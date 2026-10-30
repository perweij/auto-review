"""Issue coordination; all implementation and review fixes execute in Owner."""
import copy
import contextlib
import sys
import json
from pathlib import Path
import re
import uuid

from .common import ApiError, atomic_json, command, digest, git, locked, now, read_json, state_root
from .repository import Repository, normalize_remote, repository_name
from . import settings, owner_state, owner_git, owner_fixes, owner_import, owner_publication
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


def input_digest(facts, state):
    # Only our receipted exact publications are excluded, never arbitrary markers.
    receipts = {a['receipt']['id']: a['receipt'] for a in state.get('actions', {}).values() if a.get('receipt') and 'body' in a}
    comments = [c for c in facts['comments'] if not (c.get('body') or '').strip().startswith('/fix-issue approve ')
                and (c.get('id') not in receipts or c.get('body') != receipts[c['id']].get('body'))]
    return digest([facts['issue']['title'], facts['issue']['body'], comments])


def approval_comment(comment, state):
    if comment.get('user', {}).get('login', '').lower() != state['viewer'].lower():
        return False
    text = (comment.get('body') or '').strip()
    for action in state.get('actions', {}).values():
        if action.get('approval') and action.get('receipt', {}).get('id') == comment.get('id') and action['receipt'].get('body') == text:
            text = action['body']
            break
    return bool(re.fullmatch(r'/fix-issue approve ' + str(state['plan']['revision']), text)) and comment.get('created_at', '') >= state['plan']['published_at']


def execution_complete(batch):
    if batch.get('status') != 'ready' or not batch.get('review', {}).get('complete') or batch.get('review', {}).get('blocking'):
        return False
    progress = batch.get('commit_progress', {})
    return progress.get('complete', False) or not batch.get('commit_plan', {}).get('units', [1])


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
        if self.state.get('phase') in ('approved', 'working') and self.state.get('approved_input'):
            semantic = copy.deepcopy(facts)
            semantic['comments'] = [c for c in facts['comments'] if not (c.get('body') or '').strip().startswith('/fix-issue approve ')]
            if input_digest(semantic, self.state) != self.state['approved_input']:
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
                if head not in state['allowed_heads']:
                    state['allowed_heads'].append(head)
                self.save()
        self.clean(path, state.get('planning_head', state.get('bootstrap_sha', state['planning_base'])))
        return path

    def plan(self):
        state, facts = self.state, self.facts
        # Approval commands are validated separately, not treated as requirements.
        semantic = copy.deepcopy(facts)
        semantic['comments'] = [c for c in facts['comments'] if not (c.get('body') or '').strip().startswith('/fix-issue approve ')]
        feedback = self.feedback_context() if state.get('pr') else []
        current = digest([input_digest(semantic, state), feedback, state['configuration'], facts['base']])
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
            context = {'issue': facts['issue'], 'comments': semantic['comments'], 'previous_plan': state.get('plan'),
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
        state['approved_input'] = input_digest(semantic, state)
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
            {'issue_body': self.facts['issue']['body'], 'answers': [c for c in self.facts['comments']
             if c.get('id') not in {a.get('receipt', {}).get('id') for a in state['actions'].values()}
             and not (c.get('body') or '').strip().startswith('/fix-issue approve ')]})
        self.save()
        return owner

    def fix(self, owner, keys):
        state = self.state
        with locked(owner.execution_lock):
            data = owner.load()
            pending = next((b for b in reversed(data['batches']) if b['items'] == keys and not execution_complete(b)), None)
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
                    self.writes.comment('Implementation needs clarification or verification:\n\n' + '\n'.join('- ' + b for b in blockers), 'blockers:' + digest(blockers))
                    state.update(phase='waiting', waiting='answers')
                    state.pop('input_digest', None)
                    self.save()
                raise ApiError('needs_attention', 'Owner verification or commit execution needs attention.', owner=str(owner.path))
            if not batch.get('commit_plan', {}).get('units'):
                latest = owner.load()
                for item in latest['feedback']:
                    if item['key'] in keys and item['kind'] == 'issue_plan':
                        item.update(triage='handled', queued=False, attention=False)
                owner.save(latest)
            commits = batch.get('commit_progress', {}).get('commits', [])
            for sha in commits:
                if sha not in state['allowed_heads']:
                    state['allowed_heads'].append(sha)
            self.save()
            head = owner.repo.git('rev-parse', 'HEAD', cwd=view['worktree']).stdout.strip()
            self.writes.push(head, state['remote_head'], view['worktree'])
        return batch

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
                                            'comments': [c['id'] for c in candidates], 'defaults': configuration})
        return imported['items']

    def feedback_context(self):
        if not self.state.get('pr'):
            return None
        number = self.state['pr']
        return [pages(self.repo, f'repos/{self.repo.name}/{endpoint}') for endpoint in
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
        self.guard()
        state['phase'] = previous_phase
        if state.get('phase') == 'complete' and state.get('finished_input') == digest([input_digest(self.facts, state), self.feedback_digest(), configuration]):
            return self.result('complete')
        if (state.get('phase') == 'ready' and not self.request.get('approve')
                and state.get('ready_input') == input_digest(self.facts, state)
                and state.get('ready_head') == self.facts.get('head')
                and state.get('ready_configuration') == digest(configuration)
                and state.get('ready_feedback') == self.feedback_digest()
                and (not state.get('approval', {}).get('receipt') or any(approval_comment(c, state) for c in self.facts['comments']))):
            if state.get('owner'):
                owner = owner_state.OwnerSession({'session': state['owner']})
                with locked(owner.execution_lock):
                    owner_git.validate(owner, owner.load())
                    owner_git.clean(owner, owner.load())
                    if owner.repo.git('rev-parse', 'HEAD', cwd=owner.load()['worktree']).stdout.strip() != state['ready_head']:
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
            items = [i['key'] for i in data['feedback'] if i.get('attention') and i.get('actionable') and not i.get('missing')]
            unfinished = next((b for b in reversed(data['batches']) if not execution_complete(b)), None)
            if unfinished:
                items = unfinished['items']
        if items:
            batch = self.fix(owner, items)
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
            if self.review():
                return self.result('needs_attention', 3, 'fix_feedback')
        # Only publish this workflow's automatic, verified replies.
        with locked(owner.execution_lock), locked(owner.repo.lock):
            data = owner.load()
            if any(a['status'] in ('submitting', 'ambiguous') for a in data['actions']):
                owner_publication.recover(owner, data)
                if any(a['status'] == 'unsent' for a in data['actions']):
                    raise ApiError('needs_attention', 'Recovered unsent replies require inspection in Owner.')
            if any(r['status'] == 'staged' and not r.get('automatic') for r in data['replies']):
                raise ApiError('needs_attention', 'Manual staged replies require explicit Owner publication.')
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
        self.writes.draft(False)
        self.writes.comment('Verified implementation and configured reviews passed. PR: ' + f'https://{self.repo.identity}/pull/{state["pr"]}', 'ready:' + state['remote_head'])
        self.guard()
        state.update(phase='ready', ready_input=input_digest(self.facts, state), ready_head=state['remote_head'],
                     ready_feedback=self.feedback_digest(), ready_configuration=digest(state['configuration']))
        self.save()
        return self.result('ready')


def dispatch(request):
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
        managed_pr = next((p for p in facts.get('pulls', []) if p['number'] == flow.state.get('pr')), None)
        if managed_pr and managed_pr.get('merged_at') and flow.state.get('managed'):
            flow.state['phase'] = 'complete'
            return flow.result('complete')
        try:
            return flow.run()
        except ApiError as exc:
            if exc.code == 'cancelled':
                raise
            flow.state['phase'] = 'blocked' if flow.state.get('waiting') != 'answers' else 'waiting'
            flow.state['error'] = {'code': exc.code, 'message': str(exc), **exc.details}
            flow.save()
            return flow.result('uncertain' if exc.code == 'uncertain' else 'partial' if exc.code == 'partial' else 'needs_attention',
                               5 if exc.code == 'uncertain' else 4 if exc.code == 'partial' else 3,
                               'recover', error=flow.state['error'])
