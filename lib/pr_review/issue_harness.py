"""Thin approved-specification execution over persistent provider CLI sessions.

The harness owns implementation, testing, debugging and decomposition. This
module owns source identity, session attribution and handoff to the existing
verified commit/publication transaction. Acceptance review of a completed local
candidate belongs to the shared review engine (see ``acceptance``).
"""
import copy
import json
import uuid
from pathlib import Path

from .common import ApiError, digest, now
from .events import emit
from . import acceptance, specification as canonical, owner_git, owner_providers, owner_commit_plan, owner_auto_commit
from .providers import Runtime, environment
from .processes import run
from .storage import write_json, atomic_bytes
from .structured import response

SCHEMA = owner_providers.obj({
    'status': {'type': 'string', 'enum': ['ready', 'working', 'changes_requested', 'needs_input', 'blocked_external']},
    'summary': owner_providers.TEXT,
    'checks': owner_providers.STRINGS,
    'findings': owner_providers.STRINGS,
    'questions': owner_providers.STRINGS,
})


def validate(value):
    if (not isinstance(value, dict) or value.get('status') not in SCHEMA['properties']['status']['enum']
            or not isinstance(value.get('summary'), str) or not value['summary'].strip()
            or any(not isinstance(value.get(k), list) or any(not isinstance(v, str) or not v.strip() for v in value[k])
                   for k in ('checks', 'findings', 'questions'))):
        raise ApiError('invalid_result', 'Harness did not return a usable completion report.')
    if value['status'] == 'ready' and (value['findings'] or value['questions']):
        raise ApiError('invalid_result', 'A ready candidate cannot contain blocking findings or questions.')
    if value['status'] == 'needs_input' and not value['questions']:
        raise ApiError('invalid_result', 'A product question requires question text.')
    if value['status'] == 'changes_requested' and not value['findings']:
        raise ApiError('invalid_result', 'Changes requested requires actionable findings.')
    return value


def arguments(choice, directory, schema, conversation):
    args = owner_providers.argv(choice, Runtime.environment(), 'fix', directory, schema)
    if choice['provider'] == 'codex':
        args.remove('--ephemeral')
        if conversation.get('session_id'):
            args.insert(2, 'resume')
            args.insert(-1, conversation['session_id'])
    else:
        args.remove('--no-session-persistence')
        if conversation.get('started'):
            args += ['--resume', conversation['session_id']]
        else:
            args += ['--session-id', conversation['session_id']]
    return args


def handoff(session, state, batch=None):
    """The verified approved document, kept outside the worktree and Git directory."""
    document = canonical.handoff(session, state)
    roots = [Path(state['worktree']).resolve(), Path(session.repo.common).resolve()]
    if any(Path(document['path']).resolve().is_relative_to(root) for root in roots):
        raise ApiError('invalid_session', 'The approved specification must be outside the worktree and Git directory.')
    return document


def invoke(session, state, batch, specification, feedback):
    """One implementation turn of the persistent ``fix`` session."""
    choice = batch['settings']['fix']
    key = digest([state['issue_workflow'], specification, choice])
    conversations = state.setdefault('issue_harness_sessions', {})
    conversation = conversations.setdefault(key, {'choice': copy.deepcopy(choice)})
    if choice['provider'] == 'claude':
        conversation.setdefault('session_id', str(uuid.uuid4()))
    directory = session.path / 'attempts' / uuid.uuid4().hex
    if any(directory.resolve().is_relative_to(Path(root).resolve())
           for root in (state['worktree'], session.repo.common)):
        raise ApiError('invalid_session', 'Harness artifacts must be outside the worktree and Git directory.')
    directory.mkdir(parents=True)
    (directory / 'scratch').mkdir()
    schema = directory / 'schema.json'
    write_json(schema, copy.deepcopy(SCHEMA))
    before = owner_git.checkpoint(session, state, 'harness before')
    if before['tree'] != batch['checkpoint']['tree']:
        raise ApiError('worktree_changed', 'Source changed between harness turns; inspect before continuing.')
    owner_git.trust(session, state, before['tree'])
    admin = owner_git.admin(session, state)
    if batch.get('integrity_failure') or batch.get('git_admin', admin) != admin:
        raise ApiError('integrity_failed', 'Protected Git state changed; inspect before continuing.')
    prompt = (Path(__file__).resolve().parents[2] / 'prompts' / 'issue-implement.txt').read_text()
    # Re-verify the approved document immediately before each turn (including resumption).
    document = handoff(session, state, batch)
    prompt, _ = owner_providers.context_prompt(prompt + '\n', {
        'approved_specification': {'format': 'markdown', 'file': document['path'], 'revision': document['plan']['revision'],
                                   'sha256': document['locator']['sha256'], 'bytes': document['locator']['bytes']},
        'feedback': feedback, 'worktree': state['worktree'],
        'selected_feedback': [canonical.provenance(i) for i in session.items(state, batch['items'])],
        'issue_context': next((i['source'].get('context', {}) for i in state['feedback']
                               if i['kind'] == 'issue_plan' and not i.get('missing')), {}),
        'scratch': str(directory / 'scratch'), 'baseline': batch['baseline'],
        'prior_checks': batch.get('harness_candidate', {}).get('checks', []),
        'previous_attempts': [a['path'] for a in batch.get('attempts', [])[-4:]]}, directory)
    atomic_bytes(directory / 'prompt.txt', prompt.encode())
    args = arguments(choice, directory, schema, conversation)
    record = {'role': 'fix', 'kind': 'issue_harness', 'choice': copy.deepcopy(choice),
              'path': str(directory), 'argv': args, 'before': before, 'git_admin_before': admin,
              'status': 'running', 'started_at': now()}
    batch.setdefault('attempts', []).append(record)
    session.save(state)
    def event_line(stream, line):
        if stream != 'stdout':
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        identity = (event.get('thread_id') if event.get('type') == 'thread.started' else
                    event.get('session_id') if event.get('type') == 'system' and event.get('subtype') == 'init' else None)
        if identity:
            # Persist the exact session immediately, including when interrupted.
            if conversation.get('session_id') and conversation['session_id'] != identity:
                raise ApiError('invalid_state', 'Provider resumed a different implementation session.')
            conversation.update(session_id=identity, started=True)
            session.save(state)
    env, _ = environment(choice['provider'])
    env.update(TMPDIR=str(directory / 'scratch'), PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0')
    turn = sum(a.get('kind') == 'issue_harness' and a.get('role') == 'fix' for a in batch['attempts'])
    emit('progress', message=f"Issue implementation turn {turn}: {choice['provider']} / {choice['model']} ({choice['effort']})")
    try:
        result = run(args, cwd=state['worktree'], env=env, input_bytes=prompt.encode(),
                     timeout=Runtime.environment().model_timeout, stdout_path=directory / 'events.jsonl',
                     stderr_path=directory / 'stderr.log', model_events=True, on_line=event_line,
                     stage='issue-implementation')
        after = owner_git.checkpoint(session, state, 'harness after')
        record['after'] = after
        batch['checkpoint'] = after
        if owner_git.admin(session, state) != admin:
            batch['integrity_failure'] = str(directory)
            raise ApiError('integrity_failed', 'Harness changed protected source or Git state.')
        # The provider could write anywhere; the approved document must be what it was handed.
        handoff(session, state, batch)
        try:
            value = validate(response(choice, result, directory))
            if not conversation.get('session_id'):
                raise ApiError('model_failed', 'Provider did not identify its implementation session; inspect the saved events.')
        except ApiError as exc:
            if exc.code == 'invalid_result' or (exc.code == 'invalid_artifact' and isinstance(exc.__cause__, ValueError)) or (exc.code == 'model_failed' and
                    str(exc) in ('Provider returned invalid structured output.', 'Provider returned no structured object.')):
                raise ApiError('invalid_result', str(exc)) from exc
            raise
        write_json(directory / 'result.json', value)
        record.update(status='complete', result=value)
        conversation['started'] = True
        return value
    except BaseException as exc:
        record.update(status='failed', error=str(exc))
        raise
    finally:
        if 'after' not in record:
            # The subprocess runner has reaped its child group. Capture retained
            # source even after cancellation, without launching more model work.
            from .processes import _cancellation
            from .issue_resources import _current
            cancellation_token = _cancellation.set(None)
            resource_token = _current.set(None)
            try:
                after = owner_git.checkpoint(session, state, 'interrupted harness')
                record['after'] = after
                if owner_git.admin(session, state) == admin:
                    batch['checkpoint'] = after
                else:
                    batch['integrity_failure'] = str(directory)
            except BaseException as checkpoint_error:
                record['checkpoint_error'] = str(checkpoint_error)
            finally:
                _current.reset(resource_token)
                _cancellation.reset(cancellation_token)
        record['finished_at'] = now()
        session.save(state)


def report(session, state, batch, specification, feedback):
    """One format correction; source work stays in the implementation session."""
    try:
        return invoke(session, state, batch, specification, feedback)
    except ApiError as exc:
        if exc.code != 'invalid_result':
            raise
        emit('progress', message='Correcting harness completion report format')
        return invoke(session, state, batch, specification,
                      list(feedback) + ['Correct the completion report to the supplied schema: ' + str(exc)])


def require_supported(batch):
    if (batch.get('execution_kind') != 'issue_harness'
            or any(t.get('status') != 'complete' for t in batch.get('tasks', []))):
        raise ApiError('unsupported_session',
                       'This unfinished issue session uses the retired task pipeline. '
                       'Automatic migration is no longer supported; preserve its worktree '
                       'and artifacts for manual recovery.')
    require_engine(batch)


def require_engine(batch):
    """Unfinished batches that began before the shared review engine are not migrated.

    New batches record ``review_engine: 1``. An untouched batch adopts the marker;
    a ready batch can still commit and publish because its guards are unchanged.
    """
    if batch.get('review_engine') == 1 or batch.get('status') == 'ready':
        return
    if batch.get('attempts'):
        raise ApiError('unsupported_session',
                       'This unfinished session started before the shared review engine. '
                       'Automatic migration is not supported; its worktree and artifacts are kept '
                       'for manual recovery.')
    batch['review_engine'] = 1


def resume(batch, request, snapshot, identity):
    """Shared guards checked HEAD, index and Git identity; retain the saved phase."""
    if request.get('skip') or request.get('replan') or request.get('verify_only'):
        raise ApiError('invalid_request', 'Harness execution has no task skip/replan/verify controls; resume the issue normally.')
    changed = snapshot['tree'] != batch['checkpoint']['tree']
    if changed and not request.get('adopt'):
        raise ApiError('worktree_changed', 'Inspect partial/manual changes, then explicitly adopt and reverify.')
    if changed or batch.get('integrity_failure') or batch.get('git_admin', identity) != identity:
        batch['harness_phase'] = 'implement'
    if request.get('adopt'):
        batch.update(checkpoint=snapshot, git_admin=identity)
        batch.pop('integrity_failure', None)
        for attempt in batch['attempts']:
            if attempt.get('status') in ('failed', 'interrupted'):
                attempt.setdefault('adopted_at', now())
    # Source changes invalidate the candidate awaiting review.
    if (batch.get('harness_phase') == 'review' and
            batch.get('harness_candidate_tree') != snapshot['tree']):
        batch['harness_phase'] = 'implement'


def execution_failure(batch):
    """Public stops are product questions or commit failures, not task policy."""
    if batch.get('harness_questions'):
        return {'stage': 'implementation', 'summary': 'An unresolved product choice needs your answer.',
                'blocking': batch['harness_questions'], 'next_action': 'await_answers'}
    execution = batch.get('commit_execution', {})
    reason = execution.get('error') or batch.get('error') or 'Commit execution needs attention.'
    return {'stage': 'commit', 'summary': reason, 'blocking': [reason], 'next_action': 'inspect_owner'}


def execute(session, state, batch, remote):
    """Use the existing batch only as a publication/commit compatibility record."""
    approved = next((i['source']['plan'] for i in state['feedback']
                     if i['kind'] == 'issue_plan' and not i.get('missing')), None)
    if not approved:
        raise ApiError('invalid_state', 'Issue execution requires its approved specification.')
    require_supported(batch)
    # Fresh start or resumption: the exact approved document must still be intact.
    handoff(session, state)
    if batch.pop('harness_questions', None):
        batch['harness_phase'] = 'implement'
    batch.pop('error', None)
    batch['status'] = 'running'
    if batch.get('discussion_stale'):
        batch.update(harness_phase='implement', discussion_stale=False)
        for field in ('harness_feedback', 'harness_unchanged', 'harness_repeated_rejection', 'harness_rejected_tree'):
            batch.pop(field, None)
        batch['fingerprints'] = {i['key']: i['fingerprint'] for i in session.items(state, batch['items'])}
    feedback = batch.get('harness_feedback', []) or [
        {'title': i.get('title'), 'comments': i.get('comments', [])}
        for i in session.items(state, batch['items']) if i['kind'] != 'issue_plan']
    implementation_feedback = feedback
    from .owner_fixes import check_fresh
    while True:
        check_fresh(session, state, batch)
        session.save(state)
        if batch.get('harness_phase') != 'review':
            before = batch['checkpoint']['tree']
            candidate = report(session, state, batch, approved, implementation_feedback)
            batch['harness_candidate'] = candidate
            batch['harness_candidate_tree'] = batch['checkpoint']['tree']
            if candidate['status'] == 'needs_input':
                batch.update(status='paused', harness_questions=candidate['questions'])
                session.save(state)
                return session.view(state)
            if candidate['status'] == 'blocked_external':
                raise ApiError('blocked_external', candidate['summary'])
            if candidate['status'] != 'ready':
                # Continue the same harness conversation. Do not review or replay
                # test commands for an implementation that says it is unfinished.
                unchanged = batch.get('harness_unchanged', 0) + 1 if before == batch['checkpoint']['tree'] else 0
                batch['harness_unchanged'] = unchanged
                if unchanged >= 3:
                    raise ApiError('automation_failed', 'Implementation repeatedly returned without a candidate or source progress.', summary=candidate['summary'])
                implementation_feedback = feedback + ['Continue the approved implementation to a complete candidate; own testing and debugging.']
                continue
            batch['harness_unchanged'] = 0
            batch['harness_phase'] = 'review'
            session.save(state)
        result, review = acceptance.review_issue(session, state, batch, approved)
        if review is None:
            # An incomplete review writes no proof. The phase stays 'review', so
            # resume reruns only the stages that lack valid evidence.
            raise ApiError('review_incomplete', 'Acceptance review could not finish: ' + result.value['summary'],
                           summary=result.value['summary'], artifact=str(result.artifact),
                           verification_blockers=result.value.get('verification_blockers', []),
                           next_action='verify_environment' if result.value.get('verification_blockers') else 'retry')
        batch['harness_review'] = review
        batch['harness_review_tree'] = batch['checkpoint']['tree']
        if review['status'] == 'needs_input':
            batch.update(status='paused', harness_questions=review['questions'])
            session.save(state)
            return session.view(state)
        if review['status'] != 'ready':
            repeated = batch.get('harness_rejected_tree') == batch['checkpoint']['tree']
            batch['harness_repeated_rejection'] = batch.get('harness_repeated_rejection', 0) + 1 if repeated else 1
            batch['harness_rejected_tree'] = batch['checkpoint']['tree']
            if batch['harness_repeated_rejection'] >= 3:
                raise ApiError('automation_failed', 'The same unchanged candidate was repeatedly rejected.', summary='; '.join(review['findings']))
            # Only blocking findings return to the implementation session.
            feedback = review['findings'] or [review['summary']]
            implementation_feedback = feedback
            batch.update(harness_feedback=feedback, harness_phase='implement')
            continue
        check_fresh(session, state, batch)
        tree = batch['checkpoint']['tree']
        outcomes = review['outcomes']
        proof = {'summary': review['summary'], 'checks': review['checks'], 'outcomes': outcomes,
                 'complete': True, 'blocking': []}
        # A completed boundary is a commit record, never an execution task.
        task = next((t for t in batch['tasks'] if t['id'] == 'approved-specification'), None)
        before = batch.get('harness_before', batch['checkpoint'])
        covered = {k for t in batch['tasks'] for k in t['items']}
        if task is None and (before['tree'] != tree or not set(batch['items']) <= covered):
            task = {'id': 'approved-specification', 'items': batch['items'], 'depends_on': [],
                    'before': before,
                    'rationale': 'Implement the approved specification', 'repairs': 0,
                    'expected': [{'item': k, 'outcome': 'Approved scope satisfied'} for k in batch['items']]}
            batch['tasks'].append(task)
        if task is not None:
            task.update(status='complete', after=batch['checkpoint'], review=proof)
        batch.update(status='ready', review=proof, review_tree=tree, final_tree=tree,
                     completed_at=now(), verified_items=batch['items'],
                     verified_fingerprints={i['key']: i['fingerprint'] for i in session.items(state, batch['items'])},
                     verified_base=state['metadata']['baseRefOid'])
        owner_commit_plan.update(session, batch, batch['baseline'])
        session.save(state)
        return owner_auto_commit.execute(session, state, batch, remote) if batch.get('auto_commit') else session.view(state)
