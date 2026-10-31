"""Issue coordination; all implementation and review fixes execute in Owner."""
import contextlib
import sys
import json
from pathlib import Path
import re
import uuid

from .common import ApiError, atomic_json, command, digest, git, locked, now, read_json, state_root
from .repository import Repository, normalize_remote, repository_name
from . import settings, owner_state, owner_git, owner_fixes, owner_import, owner_publication, owner_completion
from .owner_completion import execution_complete
from .issue_eligibility import collect_eligibility_facts, evaluate_eligibility, EligibilityPolicy, pages
from .issue_planning import assess_issue
from .issue_writes import Writes


def resolve(request):
    cwd = Path(request.get('cwd', '.')).resolve()
    identity = request.get('repository')
    if not identity:
        remotes = git(['config', '--get-regexp', r'^remote\..*\.url$'], cwd=cwd).stdout
        names = {normalize_remote(line.split(None, 1)[1]) for line in remotes.splitlines()}
        names.discard('')
        if len(names) != 1:
            raise ApiError('invalid_request', 'Choose a repository with -R.')
        identity = names.pop()
    return Repository.resolve(request.get('pr') or request.get('issue'), repository_name(identity), cwd)


def semantic_comment(comment):
    """Keep discussion content and review anchors, excluding GitHub metadata."""
    result = {'id': comment.get('id'), 'user': {'login': comment.get('user', {}).get('login', '')},
              'body': comment.get('body') or ''}
    for key in ('path', 'line', 'side', 'start_line', 'start_side', 'original_line', 'original_start_line',
                'position', 'original_position', 'subject_type', 'commit_id', 'original_commit_id',
                'in_reply_to_id', 'pull_request_review_id', 'diff_hunk', 'state'):
        if key in comment:
            result[key] = comment[key]
    return result


def matches_receipt(comment, receipt):
    return (comment.get('id') == receipt.get('id') and comment.get('body') == receipt.get('body')
            and comment.get('user', {}).get('login', '').lower() == receipt.get('user', {}).get('login', '').lower())


def approval_revision(comment, state):
    """Parse only the initiator's standalone command; acceptance also checks time."""
    if comment.get('user', {}).get('login', '').lower() != state['viewer'].lower():
        return None
    text = (comment.get('body') or '').strip()
    for action in state.get('actions', {}).values():
        if action.get('approval') and action.get('receipt') and matches_receipt(comment, action['receipt']):
            text = action['body']
            break
    match = re.fullmatch(r'/fix-issue approve ([1-9][0-9]*)', text)
    return match.group(1) if match else None


def semantic_comments(comments, state):
    # Only exact receipted publications and standalone commands are excluded.
    receipts = {a['receipt']['id']: a['receipt'] for a in state.get('actions', {}).values()
                if a.get('receipt') and 'body' in a}
    return [semantic_comment(c) for c in comments
            if not (c.get('id') in receipts and matches_receipt(c, receipts[c['id']]))
            and approval_revision(c, state) is None]


def input_digest(facts, state):
    return digest([facts['issue']['title'], facts['issue']['body'], semantic_comments(facts['comments'], state)])


def approval_comment(comment, state):
    return (approval_revision(comment, state) == str(state['plan']['revision'])
            and comment.get('created_at', '') >= state['plan']['published_at'])


def required_batches(data):
    """Keep stale execution blocking until newer work verifies every item."""
    latest = {key: batch for batch in data['batches'] for key in owner_state.active_feedback_keys(batch)}
    feedback = {i['key']: i for i in data.get('feedback', []) if not i.get('missing')}
    for batch in data['batches']:
        keys = owner_state.active_feedback_keys(batch)
        if (batch.get('discussion_stale') and keys
                and execution_complete(batch, current_discussion=False)
                and all(latest[key] is not batch and execution_complete(latest[key])
                        and key in latest[key].get('verified_items', [])
                        and key in feedback
                        and latest[key].get('verified_fingerprints', {}).get(key) == feedback[key]['fingerprint']
                        and latest[key].get('verified_base')
                        and latest[key].get('verified_base') == data.get('metadata', {}).get('baseRefOid')
                        for key in keys)):
            continue
        yield batch


def check_owner_execution(owner, data):
    """Caller holds execution_lock; commit progress alone cannot authorize writes."""
    if any(not execution_complete(batch) for batch in required_batches(data)):
        raise ApiError('needs_attention', 'Owner verification or commit execution needs attention.', owner=str(owner.path))
    owner_git.validate(owner, data)
    owner_git.clean(owner, data)


def check_owner_publication(data, *, allow_staged=False):
    """Required replies survive retries; old absent attempts remain history."""
    published = {r['id'] for r in data['replies'] if r['status'] == 'published'}
    if any(a['status'] in ('submitting', 'ambiguous') for a in data['actions']):
        raise ApiError('needs_attention', 'Uncertain replies require recovery in Owner.')
    if any(a['status'] == 'unsent' and a['reply'] not in published for a in data['actions']):
        raise ApiError('needs_attention', 'Recovered unsent replies require inspection in Owner.')
    completed = {(r.get('batch'), r['source']) for r in data['replies'] if r['status'] == 'published'}
    for batch in data['batches']:
        for issue in batch.get('reply_issues', []):
            if (batch['id'], issue['item']) not in completed:
                raise ApiError('needs_attention', issue['reason'])
    for reply in data['replies']:
        if reply['status'] == 'published':
            continue
        if reply.get('needs_attention'):
            raise ApiError('needs_attention', reply['needs_attention'])
        if reply['status'] == 'staged' and not reply.get('automatic'):
            raise ApiError('needs_attention', 'Manual staged replies require explicit Owner publication.')
        if reply.get('automatic') and (reply['status'] != 'staged' or not allow_staged):
            raise ApiError('needs_attention', 'Automatic replies require publication or inspection in Owner.')


class Workflow:
    def __init__(self, request, repo, facts):
        self.request, self.repo = request, repo
        number = facts['issue']['number']
        self.path = state_root() / 'issues-v1' / digest([repo.identity, str(repo.common)])[:32] / f'issue-{number}'
        self.file = self.path / 'session.json'
        self.state = read_json(self.file)
        if not self.state:
            self.state = {'kind': 'issue', 'format_version': 1, 'sequence': 0, 'id': uuid.uuid4().hex,
                          'issue': number, 'repository': repo.identity, 'git_common_dir': str(repo.common),
                          'viewer': facts['viewer'], 'phase': 'intake', 'actions': {}, 'allowed_heads': [],
                          'title': facts['issue']['title'], 'created_at': now()}
        if self.state.get('format_version') != 1 or self.state.get('git_common_dir') != str(repo.common):
            raise ApiError('invalid_session', 'Unsupported issue state or repository identity.')
        if self.state['viewer'].lower() != facts['viewer'].lower():
            raise ApiError('wrong_user', 'Resume using the GitHub account that initiated this workflow.')
        if request.get('pr') and not self.state.get('pr'):
            self.state['pr'] = request['pr']
        self.facts = facts
        self.writes = Writes(repo, self.state, self.save, self.guard)

    def save(self):
        self.state['sequence'] += 1
        atomic_json(self.file, self.state)

    def result(self, outcome, code=0, next_action='none', **extra):
        self.state['next_action'] = next_action
        self.save()
        return {'outcome': outcome, 'exit_code': code, 'phase': self.state['phase'],
                'issue_url': f'https://{self.repo.identity}/issues/{self.state["issue"]}',
                'pr_url': f'https://{self.repo.identity}/pull/{self.state["pr"]}' if self.state.get('pr') else None,
                'session': str(self.path), 'worktree': self.state.get('worktree'), 'next_action': next_action,
                'eligibility': self.eligibility, **extra}

    def guard(self):
        target = {'issue': self.state['issue']}
        if self.state.get('pr'):
            target['pr'] = self.state['pr']
        facts = collect_eligibility_facts(target, self.repo)
        facts['workflow'] = self.state if self.file.exists() else None
        eligibility = evaluate_eligibility(facts, EligibilityPolicy(self.request.get('force', False)))
        self.eligibility = eligibility
        if eligibility['eligibility'] != 'eligible':
            raise ApiError('preflight_failed', 'Eligibility changed; inspect checks.', checks=eligibility['checks'])
        self.facts = facts
        if self.state.get('phase') in ('approved', 'working') and facts['base'] != self.state.get('assessed_base'):
            raise ApiError('stale_base', 'PR base changed since assessment; rerun to assess and review the current base.')
        if self.state.get('phase') in ('approved', 'working') and self.state.get('approved_input'):
            if input_digest(facts, self.state) != self.state['approved_input']:
                raise ApiError('stale_discussion', 'Issue requirements changed during execution; replan before publication.')
            if not self.request.get('yolo') and not any(approval_comment(c, self.state) for c in facts['comments']):
                raise ApiError('stale_approval', 'Approval was removed or changed; approve the current plan again.')
        return facts

    def clean(self, path, expected):
        if self.repo.git('status', '--porcelain', '--untracked-files=all', cwd=path).stdout or self.repo.git('rev-parse', 'HEAD', cwd=path).stdout.strip() != expected:
            raise ApiError('workspace_changed', 'Managed planning/bootstrap worktree changed; inspect retained files.')

    def workspace(self):
        state = self.state
        if not state.get('planning_worktree'):
            state['planning_worktree'] = str(self.path.parent / ('worktree-' + state['id']))
            state['planning_base'] = self.facts['head'] or self.facts['base']
            self.save()
        path = Path(state['planning_worktree'])
        if not path.exists():
            self.repo.git('worktree', 'add', '--detach', str(path), state['planning_base'])
        common = self.repo.git('rev-parse', '--path-format=absolute', '--git-common-dir', cwd=path).stdout.strip()
        if common != str(self.repo.common):
            raise ApiError('workspace_changed', 'Planning worktree belongs to another repository.')
        if state.get('bootstrap_intent') and not state.get('bootstrap_sha'):
            head = self.repo.git('rev-parse', 'HEAD', cwd=path).stdout.strip()
            parent = state['bootstrap_intent']['parent']
            if head != parent:
                if (self.repo.git('show', '-s', '--format=%P', head).stdout.strip() != parent
                        or self.repo.git('rev-parse', head + '^{tree}').stdout != self.repo.git('rev-parse', parent + '^{tree}').stdout
                        or self.repo.git('show', '-s', '--format=%B', head).stdout.strip() != state['bootstrap_intent']['message']):
                    raise ApiError('workspace_changed', 'Interrupted bootstrap commit needs inspection.')
                state['bootstrap_sha'] = head
                state['planning_head'] = head
                if head not in state['allowed_heads']:
                    state['allowed_heads'].append(head)
                self.save()
        self.clean(path, state.get('planning_head', state.get('bootstrap_sha', state['planning_base'])))
        return path

    def plan(self):
        self.writes.reconcile()
        state, facts = self.state, self.facts
        feedback = self.feedback_context() if state.get('pr') else []
        # Adopt pending blockers from sessions created before generations existed.
        if state.get('execution_blockers') and not state.get('reassessment_generation'):
            state['reassessment_generation'] = 1
            self.save()
        identity = [input_digest(facts, state), feedback, state['configuration'], facts['base']]
        if state.get('reassessment_generation'):
            identity.append(state['reassessment_generation'])
        current = digest(identity)
        changed = current != state.get('input_digest') or facts['base'] != state.get('assessed_base')
        if changed or not state.get('assessment'):
            path = self.workspace()
            desired = facts['head'] or facts['base']
            if self.repo.git('rev-parse', 'HEAD', cwd=path).stdout.strip() != desired:
                # Implementation uses Owner's separate worktree. This clean,
                # read-only planning checkout follows the current recorded head.
                if state.get('pr') or not state.get('bootstrap_intent'):
                    self.repo.git('checkout', '--detach', desired, cwd=path)
                    state['planning_head'] = desired
                    if not state.get('bootstrap_intent'):
                        state['planning_base'] = desired
                    self.save()
            context = {'issue': facts['issue'], 'comments': semantic_comments(facts['comments'], state), 'previous_plan': state.get('plan'),
                       'execution_blockers': state.get('execution_blockers', []), 'pr_feedback': feedback}
            before = self.repo.git('rev-parse', 'HEAD', cwd=path).stdout.strip()
            assessment = assess_issue(context, state['configuration'], self.path / 'assessments', path,
                                      state.get('assessment'), self.request.get('trusted_snapshot'))
            self.clean(path, before)
            if state.get('plan') and facts['base'] != state.get('assessed_base') and assessment['decision'] == 'continue':
                assessment.update(decision='plan', plan=state['plan']['text'], acceptance=state['plan']['acceptance'])
            state.update(assessment=assessment, input_digest=current, assessed_base=facts['base'])
            self.save()
        assessment = state['assessment']
        if state.get('applied_assessment') != current:
            # Replacement assessments may publish before selecting their next
            # phase. Leave execution first so its old approval cannot block them.
            state['phase'] = 'intake'
            self.save()
            decision = assessment['decision']
            if decision == 'questions':
                round_nr = state.get('question_round', 0) + 1
                body = '\n\n'.join(f'**Q{round_nr}.{i + 1}** {q}' for i, q in enumerate(assessment['questions']))
                self.writes.comment(body, 'questions:' + current)
                state.update(question_round=round_nr, phase='waiting', waiting='answers')
                state.pop('approval', None)
            elif decision == 'plan':
                revision = state.get('plan', {}).get('revision', 0) + 1
                body = assessment['plan'] + '\n\nAcceptance criteria:\n' + '\n'.join('- ' + a for a in assessment['acceptance'])
                state['plan'] = {'revision': revision, 'body': body, 'text': assessment['plan'], 'acceptance': assessment['acceptance'],
                                 'digest': digest(body), 'published_at': now()}
                state.update(phase='waiting', waiting='approval')
                state.pop('approval', None)
                self.save()
            elif decision in ('no_change', 'unsuitable'):
                self.writes.comment(assessment['summary'], decision + ':' + current)
                state.update(phase='complete' if decision == 'no_change' else 'blocked', waiting=decision)
            elif not state.get('plan'):
                raise ApiError('invalid_result', 'Cannot continue without a plan.')
            else:
                state.update(phase='waiting', waiting='approval')
            state['applied_assessment'] = current
            self.save()
        if assessment['decision'] in ('no_change', 'unsuitable'):
            # Restore terminal decisions in sessions whose phase was overwritten
            # by the former error handler. An older plan must not resume.
            decision = assessment['decision']
            state.update(phase='complete' if decision == 'no_change' else 'blocked', waiting=decision)
            self.save()
            return False
        if state.get('plan'):
            plan = state['plan']
            receipt = self.writes.comment(f'## Plan revision {plan["revision"]}\n\n{plan["body"]}\n\nApprove with `/fix-issue approve {plan["revision"]}` or `fix-issue -i {state["issue"]} --approve {plan["revision"]}`.', 'plan:' + str(plan['revision']))
            plan['published_at'] = receipt['created_at']
            self.save()
        if state['phase'] in ('complete', 'blocked'):
            return False
        if state.get('waiting') == 'answers':
            return False
        if self.request.get('approve') is not None:
            if self.request['approve'] != state['plan']['revision']:
                raise ApiError('stale_approval', 'Approve the current published revision.')
            self.writes.comment('/fix-issue approve ' + str(self.request['approve']), 'approve:' + str(self.request['approve']), approval=True)
        self.guard()
        approved = next((c for c in self.facts['comments'] if approval_comment(c, state)), None)
        if not approved and not self.request.get('yolo'):
            state.update(phase='waiting', waiting='approval')
            return False
        state['approval'] = {'revision': state['plan']['revision'], 'digest': state['plan']['digest'],
                             'receipt': approved, 'yolo': bool(not approved), 'at': now()}
        state['approved_input'] = input_digest(facts, state)
        state.update(phase='approved', waiting=None)
        self.save()
        return True

    def bootstrap(self):
        state = self.state
        if not state.get('remote_branch'):
            pr = self.facts.get('pr')
            state.update(base_branch=self.facts['base_branch'],
                         remote_branch=pr['head']['ref'] if pr else f'fix-issue/{state["issue"]}-{state["id"][:10]}',
                         remote_head=self.facts.get('head'))
            if pr:
                state.update(pr=pr['number'], managed=True, allowed_heads=[self.facts['head']])
            self.save()
        if state.get('pr') and not state.get('managed'):
            state.update(managed=True, allowed_heads=[self.facts['head']])
            self.save()
        if not state.get('pr'):
            path = self.workspace()
            if not state.get('bootstrap_sha'):
                state.setdefault('bootstrap_intent', {'parent': state['planning_base'], 'message': f'Initialize fix for issue #{state["issue"]}\n\nWorkflow: {state["id"]}'})
                self.save()
                # A prior interrupted commit is recovered by exact parent/tree/message.
                parent = state['bootstrap_intent']['parent']
                head = self.repo.git('rev-parse', 'HEAD', cwd=path).stdout.strip()
                if head == parent:
                    command(['git', 'commit', '--allow-empty', '--file', '-', '--no-edit'], cwd=path,
                            input_text=state['bootstrap_intent']['message'], timeout=120)
                    head = self.repo.git('rev-parse', 'HEAD', cwd=path).stdout.strip()
                if self.repo.git('show', '-s', '--format=%P', head).stdout.strip() != parent or self.repo.git('rev-parse', head + '^{tree}').stdout != self.repo.git('rev-parse', parent + '^{tree}').stdout:
                    raise ApiError('workspace_changed', 'Bootstrap hook or another writer changed the commit.')
                if self.repo.git('show', '-s', '--format=%B', head).stdout.strip() != state['bootstrap_intent']['message']:
                    raise ApiError('workspace_changed', 'Bootstrap message changed; inspect the commit.')
                state['bootstrap_sha'] = head
                state['planning_head'] = head
                state['allowed_heads'].append(head)
                self.save()
            self.writes.push(state['bootstrap_sha'], None, path)
            self.writes.create_pr()
        self.guard()
        opened = owner_state.open_session({'cwd': str(self.repo.cwd), 'repository': self.repo.identity, 'pr': state['pr'], 'defaults': state['configuration']})
        owner = owner_state.OwnerSession({'session': opened['session']})
        with locked(owner.execution_lock):
            data = owner.load()
            link = data.get('issue_workflow')
            if link and link != state['id']:
                raise ApiError('busy', 'Owner belongs to another issue workflow.')
            if not link and data.get('batches'):
                raise ApiError('busy', 'Existing Owner batches require manual reconciliation before attachment.')
            if data.get('worktree'):
                owner_git.validate(owner, data)
                head = owner.repo.git('rev-parse', 'HEAD', cwd=data['worktree']).stdout.strip()
                allowed = set(state['allowed_heads'])
                for batch in data['batches']:
                    allowed.update(batch.get('commit_progress', {}).get('commits', []))
                    for attempt in batch.get('commit_execution', {}).get('attempts', []):
                        if attempt.get('commit'):
                            allowed.add(attempt['commit'])
                        elif attempt.get('status') in ('preparing', 'running', 'uncertain'):
                            if owner.repo.git('show', '-s', '--format=%T %P', head).stdout.strip() == attempt['tree'] + ' ' + attempt['parent']:
                                allowed.add(head)
                if head not in allowed:
                    raise ApiError('workspace_changed', 'Owner HEAD has an unrecorded commit; inspect before continuing.')
                if not any(not execution_complete(b) for b in data['batches']):
                    owner_git.clean(owner, data)
            data['issue_workflow'] = state['id']
            if state.get('owner_settings') and (self.request.get('settings_file') or not link):
                if any(b.get('status') != 'ready' and b.get('attempts') for b in data['batches']) and data['settings'] != state['owner_settings']:
                    raise ApiError('settings_changed', 'Active Owner batch settings are frozen.')
                data['settings'] = state['owner_settings']
            owner.save(data)
        state['owner'] = str(owner.path)
        state['issue_item'] = owner_import.from_issue(owner, state['plan'], self.facts['issue'], state['id'],
            {'issue_body': self.facts['issue']['body'], 'answers': semantic_comments(self.facts['comments'], state)})
        self.save()
        return owner

    def fix(self, owner, keys):
        state = self.state
        with locked(owner.execution_lock):
            data = owner.load()
            # Finished execution retains its commits and reports. Changed
            # feedback must be assessed in a new batch from the current HEAD.
            pending = next((b for b in reversed(list(required_batches(data)))
                            if b['items'] == keys and not execution_complete(b, current_discussion=False)), None)
            request = {'items': keys, 'auto_commit': True, 'trusted_snapshot': self.request.get('trusted_snapshot')}
            if pending:
                request['batch'] = pending['id']
                request['adopt'] = bool(self.request.get('adopt'))
                if pending.get('discussion_stale'):
                    request['replan'] = True
            view = owner_fixes.execute(owner, data, request)
            batch = next(b for b in reversed(view['batches']) if b['items'] == keys)
            state['worktree'] = view['worktree']
            self.save()
            if not execution_complete(batch):
                blockers = batch.get('review', {}).get('blocking', [])
                for task in batch.get('tasks', []):
                    for report in ('implementation', 'review'):
                        for outcome in task.get(report, {}).get('outcomes', []):
                            if outcome.get('status') == 'clarify':
                                blockers.append(outcome['evidence'])
                if blockers:
                    state['execution_blockers'] = blockers
                    state['reassessment_generation'] = state.get('reassessment_generation', 0) + 1
                    self.writes.comment('Implementation needs clarification or verification:\n\n' + '\n'.join('- ' + b for b in blockers), 'blockers:' + digest(blockers))
                    state.update(phase='waiting', waiting='answers')
                    self.save()
                raise ApiError('needs_attention', 'Owner verification or commit execution needs attention.', owner=str(owner.path))
            check_owner_execution(owner, owner.load())
            # Only verified execution resolves blockers. Keep the generation so
            # clearing them cannot reapply the assessment on an unchanged retry.
            state.pop('execution_blockers', None)
            self.save()
            if not batch.get('commit_plan', {}).get('units'):
                latest = owner.load()
                for item in latest['feedback']:
                    if item['key'] in keys and item['kind'] == 'issue_plan':
                        item.update(triage='handled', queued=False, attention=False)
                owner.save(latest)
        return batch

    def implementation_head(self, owner, data):
        """Caller holds execution_lock; only the latest verified result may ship."""
        check_owner_execution(owner, data)
        head = owner.repo.git('rev-parse', 'HEAD', cwd=data['worktree']).stdout.strip()
        latest = data['batches'][-1] if data['batches'] else {}
        commits = latest.get('commit_progress', {}).get('commits', [])
        expected = commits[-1] if commits else latest.get('baseline', self.state['remote_head'])
        if head != expected:
            raise ApiError('workspace_changed', 'Owner HEAD differs from the verified implementation.')
        return head

    def push_implementation(self, owner):
        # Publication resumes even when local commit reconciliation handled all
        # feedback. Writes.push reads back accepted pushes before sending again.
        with locked(owner.execution_lock):
            data = owner.load()
            head = self.implementation_head(owner, data)
            for batch in data['batches']:
                for sha in batch.get('commit_progress', {}).get('commits', []):
                    if sha not in self.state['allowed_heads']:
                        self.state['allowed_heads'].append(sha)
            self.state['worktree'] = data['worktree']
            self.save()
            self.writes.push(head, self.state['remote_head'], data['worktree'])
            self.guard()
            if head != self.state['remote_head'] or head != self.facts.get('head'):
                raise ApiError('stale_head', 'PR head does not match the confirmed implementation; retry before review.')
            return head

    def check_reviewed_implementation(self, owner, data, reviewed_head):
        head = self.implementation_head(owner, data)
        self.guard()
        if head != reviewed_head or head != self.state['remote_head'] or head != self.facts.get('head'):
            raise ApiError('stale_head', 'Reviewed PR head differs from the verified implementation; rerun review.')

    def review(self):
        from .reviews import execute
        from .state import Session
        from .automation import manual_reason
        configuration = self.state['configuration']
        request = {'cwd': str(self.repo.cwd), 'repository': self.repo.identity, 'pr': self.state['pr'], **configuration,
                   'trusted_snapshot': self.request.get('trusted_snapshot')}
        if self.state.get('review_session'):
            request['session'] = self.state['review_session']
        with contextlib.redirect_stdout(sys.stderr):
            session = execute(request)
        self.state['review_session'] = str(session.root)
        self.save()
        if session.manifest.get('snapshot', {}).get('base') != self.state['assessed_base']:
            raise ApiError('stale_base', 'Review base differs from the assessed base; rerun to assess and review the current base.')
        if session.manifest.get('snapshot', {}).get('head') != self.state['remote_head']:
            raise ApiError('stale_head', 'Review head differs from the confirmed implementation; rerun review.')
        data = session.load()
        reason = manual_reason(data)
        if reason:
            raise ApiError('needs_attention', reason)
        candidates = [c for c in data['comments'] if c['status'] not in ('published', 'suppressed')]
        blockers = [c for c in candidates if session.blockers(c, data, bulk=True)]
        if blockers:
            raise ApiError('needs_attention', 'Review findings need judgment before automatic fixing.', comments=[c['id'] for c in blockers])
        if not candidates:
            return []
        imported = owner_import.from_review({'session': str(session.root), 'generation': session.id, 'revision': digest(data),
                                            'comments': [c['id'] for c in candidates], 'defaults': configuration,
                                            'automatic': True})
        return imported['items']

    def feedback_context(self):
        if not self.state.get('pr'):
            return None
        number = self.state['pr']
        return [[semantic_comment(c) for c in pages(self.repo, f'repos/{self.repo.name}/{endpoint}')] for endpoint in
                       (f'issues/{number}/comments', f'pulls/{number}/comments', f'pulls/{number}/reviews')]

    def feedback_digest(self):
        return digest(self.feedback_context())

    def run(self):
        state = self.state
        configuration = settings.initial_settings(self.request)
        configuration.update(state.get('configuration', {}))
        configuration.update({k: self.request[k] for k in settings.DEFAULTS if k in self.request})
        configuration = settings.validate(configuration)
        if configuration != state.get('configuration') and state.get('owner'):
            owner = owner_state.OwnerSession({'session': state['owner']})
            if any(not execution_complete(b) and b.get('attempts') for b in owner.load()['batches']):
                raise ApiError('settings_changed', 'An active batch has frozen settings.')
        state['configuration'] = configuration
        if self.request.get('settings_file'):
            state['owner_settings'] = owner_state.validate_settings(read_json(self.request['settings_file']))
        self.save()
        previous_phase = state.get('phase')
        state['phase'] = 'intake'
        try:
            self.writes.reconcile()
            self.guard()
        finally:
            state['phase'] = previous_phase
        state.pop('error', None)
        if state.get('phase') == 'complete' and state.get('finished_input') == digest([input_digest(self.facts, state), self.feedback_digest(), configuration]):
            return self.result('complete')
        if (state.get('phase') == 'ready' and not self.request.get('approve')
                and state.get('ready_input') == input_digest(self.facts, state)
                and state.get('ready_head') == self.facts.get('head')
                and state.get('ready_base') == self.facts['base']
                and state.get('ready_configuration') == digest(configuration)
                and state.get('ready_feedback') == self.feedback_digest()
                and (not state.get('approval', {}).get('receipt') or any(approval_comment(c, state) for c in self.facts['comments']))):
            if state.get('owner'):
                owner = owner_state.OwnerSession({'session': state['owner']})
                with locked(owner.execution_lock):
                    data = owner.load()
                    check_owner_execution(owner, data)
                    check_owner_publication(data)
                    if owner.repo.git('rev-parse', 'HEAD', cwd=data['worktree']).stdout.strip() != state['ready_head']:
                        raise ApiError('workspace_changed', 'Owner HEAD changed outside the workflow.')
            return self.result('noop')
        if state.get('phase') == 'blocked' and state.get('waiting') != 'unsuitable':
            state['phase'] = 'intake'
        if not self.plan():
            return self.result(state.get('waiting') or state['phase'], 0 if state['phase'] == 'complete' else 3 if state['phase'] == 'blocked' else 2,
                               'none' if state['phase'] in ('complete', 'blocked') else 'await_answers' if state.get('waiting') == 'answers' else 'await_approval')
        owner = self.bootstrap()
        self.writes.draft(True)
        state['phase'] = 'working'
        self.save()
        with locked(owner.execution_lock):
            data = owner.load()
            owner_state.refresh(owner, data)
            if data['batches']:
                reconciled = owner_completion.reconcile(owner, data)
                if reconciled['reconciliation']['status'] != 'complete':
                    raise ApiError('needs_attention', reconciled['reconciliation']['reason'])
                data = owner.load()
            items = [i['key'] for i in data['feedback'] if i.get('attention') and i.get('actionable') and not i.get('missing')]
            unfinished = next((b for b in reversed(list(required_batches(data))) if not execution_complete(b)), None)
            if unfinished:
                items = unfinished['items']
        batch = self.fix(owner, items) if items else None
        reviewed_head = self.push_implementation(owner)
        if batch:
            if items == [state['issue_item']] and not batch.get('commit_plan', {}).get('units') and not any(b.get('commit_progress', {}).get('commits') for b in owner.load()['batches']):
                self.writes.comment('Owner verified that no implementation changes are needed. The draft PR is retained for manual closure.\n\n' + batch['review']['summary'], 'no-change:' + str(state['plan']['revision']))
                self.guard()
                state.update(phase='complete', finished_input=digest([input_digest(self.facts, state), self.feedback_digest(), state['configuration']]))
                return self.result('no_change')
        # Each invocation gets at most one corrective batch; persisted intent lets
        # interrupted retries resume the same Owner batch rather than duplicate it.
        keys = self.review()
        if keys:
            self.fix(owner, keys)
            reviewed_head = self.push_implementation(owner)
            if self.review():
                return self.result('needs_attention', 3, 'fix_feedback')
        # Only publish this workflow's automatic, verified replies.
        with locked(owner.execution_lock), locked(owner.repo.lock):
            data = owner.load()
            self.check_reviewed_implementation(owner, data, reviewed_head)
            if any(a['status'] in ('submitting', 'ambiguous') for a in data['actions']):
                owner_publication.recover(owner, data)
            check_owner_publication(data, allow_staged=True)
            if any(r['status'] == 'staged' and r.get('automatic') for r in data['replies']):
                self.guard()
                prepared = owner_publication.prepare(owner, data)
                try:
                    owner_publication.publish(owner, data, {'token': prepared['token']})
                except ApiError as exc:
                    if any(a['status'] in ('submitting', 'ambiguous') for a in data['actions']):
                        raise ApiError('uncertain', str(exc), exit_code=5) from exc
                    if any(a['status'] == 'published' for a in data['actions']):
                        raise ApiError('partial', str(exc), exit_code=4) from exc
                    raise
            data = owner.load()
            self.check_reviewed_implementation(owner, data, reviewed_head)
            check_owner_publication(data)
            self.writes.draft(False)
            self.writes.comment('Verified implementation and configured reviews passed. PR: ' + f'https://{self.repo.identity}/pull/{state["pr"]}', 'ready:' + state['remote_head'])
            data = owner.load()
            self.check_reviewed_implementation(owner, data, reviewed_head)
            check_owner_publication(data)
            state.update(phase='ready', ready_input=input_digest(self.facts, state), ready_head=state['remote_head'],
                         ready_base=state['assessed_base'],
                         ready_feedback=self.feedback_digest(), ready_configuration=digest(state['configuration']))
            self.save()
        return self.result('ready')


def dispatch(request):
    if request.get('retry_absent') is not None and (
            request.get('command') != 'issue-recover' or not isinstance(request['retry_absent'], str) or not request['retry_absent']):
        raise ApiError('invalid_request', 'retry_absent requires issue-recover and an action key.')
    for key in ('force', 'yolo', 'adopt'):
        if key in request and type(request[key]) is not bool:
            raise ApiError('invalid_request', key + ' must be a Boolean.')
    for key in ('issue', 'pr', 'approve'):
        if request.get(key) is not None and (type(request[key]) is not int or request[key] < 1):
            raise ApiError('invalid_request', key + ' must be a positive integer.')
    if bool(request.get('issue')) == bool(request.get('pr')):
        raise ApiError('invalid_request', 'Choose exactly one issue or PR.')
    repo = resolve(request)
    facts = collect_eligibility_facts(request, repo)
    if not facts.get('issue'):
        return {'outcome': 'ineligible', 'exit_code': 3, 'eligibility': evaluate_eligibility(facts)}
    lock = repo.common / 'fix-issue-locks' / (digest([repo.identity, facts['issue']['number']]) + '.lock')
    with locked(lock):
        flow = Workflow(request, repo, facts)
        flow.eligibility = evaluate_eligibility({**facts, 'workflow': flow.state if flow.file.exists() else None}, EligibilityPolicy(request.get('force', False)))
        if request['command'] == 'issue-eligibility':
            return flow.eligibility
        if request['command'] == 'issue-read':
            return {**flow.state, 'session': str(flow.path), 'eligibility': flow.eligibility}
        if request['command'] == 'issue-recover':
            actions = flow.writes.recover(request.get('retry_absent'))
            return flow.result('recovered' if request.get('retry_absent') else 'inspected',
                               next_action='retry', actions=actions)
        managed_pr = next((p for p in facts.get('pulls', []) if p['number'] == flow.state.get('pr')), None)
        if managed_pr and managed_pr.get('merged_at') and flow.state.get('managed'):
            flow.state['phase'] = 'complete'
            return flow.result('complete')
        try:
            return flow.run()
        except ApiError as exc:
            if exc.code == 'cancelled':
                raise
            # Errors describe this invocation; phase records resumable progress.
            flow.state['error'] = {'code': exc.code, 'message': str(exc), **exc.details}
            flow.save()
            return flow.result('uncertain' if exc.code == 'uncertain' else 'partial' if exc.code == 'partial' else 'needs_attention',
                               5 if exc.code == 'uncertain' else 4 if exc.code == 'partial' else 3,
                               'recover', error=flow.state['error'])
