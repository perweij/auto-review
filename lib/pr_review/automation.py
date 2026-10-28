"""Explicit unattended reviewer orchestration; draft and publisher rules stay shared.

Run records live outside generation draft ledgers. The context capability is
internal: JSON requests cannot impersonate automatic mutations or enable guards.
"""
from contextvars import ContextVar
import re

from .common import ApiError, atomic_json, digest, locked, now, read_json, state_root
from . import publication
from .state import Session, generation

_active = ContextVar('automatic_review', default=None)
OUTSTANDING = {'draft', 'failed', 'submitting', 'ambiguous'}
EXIT_CODES = {'published': 0, 'noop': 0, 'failed': 1, 'needs_attention': 3,
              'partial': 4, 'uncertain': 5, 'interrupted': 130}


def actions(state):
    return [a for b in state.get('batches', []) for a in b['actions']]


def manual_reason(state, *, completed_history=False):
    if state.get('verdict_requested') or state.get('verdict', 'COMMENT') != 'COMMENT':
        return 'A verdict was selected manually. Finish this review in Emacs.'
    # Fully handled history does not permanently prohibit a new generation.
    unsettled = [c for c in state['comments'] if c['status'] not in ('published', 'suppressed')]
    if completed_history and not unsettled:
        return None
    if not state.get('mutation_tracking'):
        return 'Saved draft authorship is unknown. Finish this saved review in Emacs.'
    if state.get('manual_intervention') or state.get('documents') or any(
            c.get('mutation_actor') == 'manual' or c.get('human_edited') or c.get('override')
            or c.get('versions') or c.get('classification') == 'HUMAN' for c in state['comments']):
        return 'This review contains manual decisions. Finish it in Emacs.'
    return None


def guard_path(root, path):
    """Caller holds the PR lock and this generation's draft lock."""
    manifest = read_json(path / 'manifest.json', {})
    raw = read_json(path / 'publication/drafts.json')
    # A candidate can hold an unchanged copy of the preceding ledger. Check
    # the originating current generation instead of adopting that copy.
    if raw and raw.get('generation') != generation(path, manifest):
        return
    if not raw and not (path / 'publication/contributions.json').exists():
        return
    session = Session({'session': str(root), 'generation': generation(path, manifest)})
    state = session.load()
    if any(a['status'] in OUTSTANDING for a in actions(state)):
        raise ApiError('automatic_handoff', 'Publication has an unfinished batch. Finish it in Emacs.')
    reason = manual_reason(state, completed_history=True)
    if reason:
        raise ApiError('automatic_handoff', reason)


def check_existing(root):
    """Called inside the analysis PR lock, before any replacement work."""
    if not _active.get() or not root:
        return
    session = Session({'session': str(root)})
    for path in dict.fromkeys([session.current, session.pointer('.pending-review')]):
        if path:
            with locked(path / 'publication/.drafts.lock'):
                guard_path(session.root, path)


def guard_replacement(run):
    if _active.get():
        guard_path(run.root, run.current)


def observe(run):
    controller = _active.get()
    if controller:
        controller.bind(run)


def manual_completion(run):
    """Remember the automatic history superseded by successful manual analysis.

    A manual retry can finish the very same generation that paused automatically.
    Keep this boundary in the completed manifest, leaving saved outcomes intact.
    The caller saves the manifest while holding the PR lock.
    """
    if not _active.get():
        run.manifest['superseded_automatic_runs'] = sorted(
            p.stem for p in (run.root / 'automatic-runs').glob('*.json'))


def summary(session):
    files = list((session.root / 'automatic-runs').glob('*.json'))
    if session.path != session.current:
        files = [p for p in files if read_json(p, {}).get('generation') == session.id]
    if not files:
        return None
    record = read_json(max(files, key=lambda p: p.stat().st_mtime_ns), {})
    if session.path == session.current and session.manifest.get('status') == 'complete':
        pending = session.pointer('.pending-review')
        pending_id = generation(pending, read_json(pending / 'manifest.json', {})) if pending else None
        # Older sessions have no explicit completion boundary. A record for an
        # unfinished automatic replacement still belongs to the current view.
        if (record.get('operation_id') in session.manifest.get('superseded_automatic_runs', [])
                or record.get('generation') not in (session.id, pending_id)):
            return {**record, 'superseded': True, 'superseded_by': session.id}
    if record.get('outcome') == 'running':
        from .operations import process_start
        op = read_json(state_root() / 'operations' / (record['operation_id'] + '.json'), {})
        if (op.get('status') != 'running' or not op.get('pid')
                or process_start(op['pid']) != op.get('start')):
            record = {**record, 'outcome': 'interrupted', 'exit_code': 130,
                      'reason': 'Automatic review was interrupted. Resume analysis with R r; check publication recovery if submission had started.'}
    return record


def review_log(session, request):
    identity = request.get('operation_id', '')
    if not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{32}', identity):
        raise ApiError('invalid_request', 'Select a saved automatic-review operation.')
    record = read_json(session.root / 'automatic-runs' / (identity + '.json'), {})
    if record.get('session') != str(session.root) or record.get('operation_id') != identity:
        raise ApiError('invalid_request', 'This operation does not belong to this review.')
    path = state_root() / 'operations' / (identity + '.log')
    if not path.is_file():
        raise ApiError('missing_log', 'The saved operation log is no longer available.')
    return {'operation_id': identity, 'text': path.read_text(errors='replace')}


class AutomaticRun:
    def __init__(self, operation):
        self.operation = operation
        self.store = None
        self.root = None
        self.state = None
        self.state_generation = None
        self.record = dict(operation_id=operation['operation_id'], log_path=operation['log_path'],
                           started_at=now(), outcome='running', affected=[], receipts=[])

    def bind(self, store):
        identity = (store.root, generation(store.path, store.manifest))
        if identity != self.state_generation:
            self.state = None
            self.state_generation = identity
        self.store, self.root = store, store.root
        self.operation.update(session=str(self.root), common=store.manifest['git_common_dir'],
                              pr=str(store.manifest['pr']))
        atomic_json(state_root() / 'operations' / (self.record['operation_id'] + '.json'), self.operation)
        self.save()

    def save(self):
        if self.store:
            self.record.update(session=str(self.root), generation=generation(self.store.path, self.store.manifest))
        if self.root:
            atomic_json(self.root / 'automatic-runs' / (self.record['operation_id'] + '.json'), self.record)

    def finish(self, outcome, reason, state=None, **fields):
        receipts = []
        if state:
            receipts = [c.get('receipt', {}).get('html_url') for c in state['comments'] if c.get('receipt')]
            receipts += [a['result'].get('html_url') for a in actions(state) if a.get('result')]
        self.record.update(outcome=outcome, exit_code=fields.pop('exit_code', EXIT_CODES[outcome]), reason=reason,
                           finished_at=now(), receipts=sorted({u for u in receipts if u}),
                           guidance='Open this PR in Emacs. Inspect attention items; R r resumes analysis, P opens publication and recovery, L shows the saved log.', **fields)
        self.save()
        self.operation.update(exit_code=self.record['exit_code'],
                              status='cancelled' if outcome == 'interrupted' else 'stopped')
        from .events import emit
        emit('progress', message=reason)
        return dict(self.record)

    def interrupt(self, exit_code):
        # A signal observed while leaving the operation cannot undo a result
        # that was already confirmed and saved.
        if self.record['outcome'] != 'running':
            return dict(self.record)
        state = self.state
        remaining = [a for a in actions(state) if a['status'] in OUTSTANDING] if state else []
        outcome = 'interrupted'
        reason = 'Automatic review interrupted; saved work and publication intents are retained.'
        if any(a['status'] in ('submitting', 'ambiguous') for a in remaining):
            outcome = 'uncertain'
            reason = 'Automatic review interrupted during publication; submission is uncertain. Inspect publication recovery in Emacs before retrying.'
        elif remaining and (any(a['status'] == 'published' for a in actions(state))
                            or any(c.get('receipt') for c in state['comments'])):
            outcome = 'partial'
            reason = 'Automatic review interrupted after confirmed publication; finish the unsent remainder in Emacs.'
        affected = [{'id': a['id'], 'status': a['status'], 'comment_ids': a.get('comment_ids', [])}
                    for a in remaining]
        return self.finish(outcome, reason, state, affected=affected,
                           exit_code=exit_code if outcome == 'interrupted' else EXIT_CODES[outcome])

    def recover(self, session, state, reason):
        """One read-back pass; never prepare or resend here."""
        self.state = state
        try:
            publication.recover(session, state)
        except ApiError as exc:
            from .processes import Cancelled
            if isinstance(exc, Cancelled):
                raise
            return self.finish('uncertain', str(exc), state)
        remaining = [a for a in actions(state) if a['status'] in OUTSTANDING]
        if any(a['status'] in ('submitting', 'ambiguous') for a in remaining):
            return self.finish('uncertain', reason, state)
        if remaining:
            return self.finish('partial' if any(a['status'] == 'published' for a in actions(state))
                               else 'needs_attention', reason, state)
        return None

    def before_analysis(self, root):
        session = Session({'session': str(root)})
        self.bind(session)
        with locked(session.pr_lock):
            session = Session({'session': str(root)})
            self.bind(session)
            for path in dict.fromkeys([session.current, session.pointer('.pending-review')]):
                if not path:
                    continue
                manifest = read_json(path / 'manifest.json', {})
                raw = read_json(path / 'publication/drafts.json')
                if raw and raw.get('generation') != generation(path, manifest):
                    continue
                with locked(path / 'publication/.drafts.lock'):
                    candidate = Session({'session': str(root), 'generation': generation(path, manifest)})
                    self.bind(candidate)
                    self.state = state = candidate.load()
                    if any(a['status'] in OUTSTANDING for a in actions(state)):
                        result = self.recover(candidate, state, 'Earlier publication has an unsent remainder. Finish it in Emacs.')
                        if result:
                            return result
                    guard_path(session.root, path)
        return None

    def publish(self, session):
        self.bind(session)
        with locked(session.pr_lock), locked(session.lock):
            session = Session({'session': str(session.root), 'generation': session.id})
            self.state = state = session.load()
            if (session.path != session.current or session.pointer('.pending-review')
                    or session.manifest.get('status') != 'complete'
                    or session.manifest.get('analysis_kind', 'full') != 'full'
                    or not session.manifest.get('discussion', {}).get('complete')):
                return self.finish('needs_attention', 'Analysis is incomplete. Saved work is retained; resume with R r.', state)
            session.check_write({'generation': session.id, 'revision': digest(state)}, state)
            if any(a['status'] in OUTSTANDING for a in actions(state)):
                result = self.recover(session, state, 'Publication has an unfinished batch. Finish it in Emacs.')
                if result:
                    return result
            reason = manual_reason(state)
            if reason:
                return self.finish('needs_attention', reason, state)
            candidates = [c for c in state['comments'] if c['status'] != 'published'
                          and not (c['status'] == 'suppressed' and c['classification'] == 'DUPLICATE')]
            blocked = [{'id': c['id'], 'reasons': session.blockers(c, state, bulk=True)} for c in candidates]
            blocked = [b for b in blocked if b['reasons']]
            if blocked:
                return self.finish('needs_attention', f"Automatic publication stopped: {len(blocked)} finding(s) need attention. No comments were published by this invocation. Analysis is saved.",
                                   state, affected=blocked)
            if not candidates:
                # prepare's 'nothing staged' answer alone cannot prove a no-op.
                prior_discussion = state.get('discussion_hash')
                affected = publication.refresh(session, state)
                if affected or state.get('discussion_hash') != prior_discussion:
                    return self.finish('needs_attention', 'Discussion changed. Inspect the saved review in Emacs.', state)
                try:
                    publication.repository_operation(session, 'validate', 'COMMENT')
                except ApiError as exc:
                    from .processes import Cancelled
                    if isinstance(exc, Cancelled):
                        raise
                    return self.finish('needs_attention', str(exc), state)
                return self.finish('noop', 'Analysis completed; no new comments to publish.', state)
            from .commands import mutate
            ids = [c['id'] for c in candidates]
            staged = mutate(session, state, {'operation': 'stage', 'comments': ids, 'bulk': True}, actor='automatic')
            if staged['skipped']:
                return self.finish('needs_attention', 'Draft eligibility changed; inspect the review in Emacs.', state, affected=staged['skipped'])
            prepared = publication.prepare(session, state)
            if not prepared['ready']:
                return self.finish('needs_attention', prepared['reason'], state)
            if set(ids) != {c['id'] for c in state['comments'] if c['status'] == 'staged'}:
                return self.finish('needs_attention', 'The prepared comment selection changed. Inspect it in Emacs.', state)
            try:
                result = publication.publish(session, state, {'confirmation': prepared['confirmation']})
            except ApiError as exc:
                from .processes import Cancelled
                if isinstance(exc, Cancelled):
                    raise
                result = {'complete': False, 'reason': str(exc)}
            if result['complete']:
                return self.finish('published', 'Publication confirmed by GitHub.', state)
            recovered = self.recover(session, state, result['reason'])
            if recovered:
                return recovered
            if all(c['status'] == 'published' for c in state['comments'] if c['id'] in ids):
                return self.finish('published', 'Publication confirmed by receipt recovery.', state)
            return self.finish('needs_attention', result['reason'], state)


def _execute(request, controller):
    from . import models, reviews, settings
    from .repository import Repository
    from .sessions import locate
    from .processes import Cancelled
    operation = controller.operation
    # Scope the internal capability to this invocation, including failures.
    token = _active.set(controller)
    try:
        repo = Repository.resolve(request['pr'], request.get('repository'), request.get('cwd'))
        operation.update(common=str(repo.common), pr=str(repo.pr))
        atomic_json(state_root() / 'operations' / (operation['operation_id'] + '.json'), operation)
        with locked(repo.lock):
            root, _ = locate(repo, request.get('session'))
        if root:
            result = controller.before_analysis(root)
            if result:
                return result
        configured = settings.resolve({**request, 'session': str(root) if root else None})['settings']
        catalogs = {p: models.discover({'provider': p}) for p in configured['reviewers']}
        models.validate_selection(configured, catalogs)
        session = reviews.execute({**request, **configured, 'catalogs': catalogs})
        return controller.publish(session)
    except (ApiError, OSError, ValueError, KeyboardInterrupt, EOFError) as exc:
        if isinstance(exc, Cancelled) or isinstance(exc, (KeyboardInterrupt, EOFError)):
            return controller.interrupt(exc.details.get('exit_code', 130) if isinstance(exc, Cancelled) else 130)
        code = exc.code if isinstance(exc, ApiError) else 'execution_failed'
        attention = code in ('automatic_handoff', 'confirmation_required', 'stale_revision', 'historical',
                             'not_ready', 'incomplete_review', 'incomplete_discussion', 'integrity_failed', 'snapshot_changed',
                             'missing_generation')
        fields = {'error_code': code}
        if isinstance(exc, ApiError) and exc.details.get('snapshot'):
            fields['trusted_snapshot'] = exc.details['snapshot']
            fields['agent_config_paths'] = exc.details.get('paths', [])
        return controller.finish('needs_attention' if attention else 'failed', str(exc), **fields)
    finally:
        _active.reset(token)


def run(request):
    from . import operations
    from .processes import Cancelled
    controller = None
    try:
        with operations.operation({'kind': 'automatic-review'}) as operation:
            controller = AutomaticRun(operation)
            return _execute(request, controller)
    except Cancelled as exc:
        # The outer cancellation scope checks again on exit. Preserve a JSON
        # result even when cancellation arrives during the final local writes.
        if controller is None:
            raise
        result = controller.interrupt(exc.details['exit_code'])
        # operation() has already saved the signal's status and exit code.
        # Keep its record consistent with the retained publication outcome.
        operation.update(exit_code=result['exit_code'],
                         status='cancelled' if result['outcome'] == 'interrupted' else 'stopped')
        atomic_json(state_root() / 'operations' / (operation['operation_id'] + '.json'), operation)
        return result
