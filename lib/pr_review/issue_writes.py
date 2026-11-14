"""Shared journaled issue operations. Models never receive these capabilities."""
import copy
from .common import ApiError, command, now
from .forge_github_issue_writes import comment_marker
from .repository import normalize_remote


class Writes:
    def __init__(self, repo, state, save, guard, account_check=lambda: None):
        self.repo, self.state, self.save, self.guard = repo, state, save, guard
        self.account_check = account_check

    @property
    def adapter(self):
        from .forge import adapter, restore
        forge = self.repo.forge if hasattr(self.repo, 'forge') else adapter(restore({'repository': self.repo.identity}).forge)
        if not hasattr(forge, 'issue_writes'):
            raise ApiError('unsupported_operation', 'Issue writes are unsupported by this forge.')
        return forge.issue_writes

    def ensure_supported(self):
        if not {'comment', 'create', 'draft'} <= self.adapter.capabilities:
            raise ApiError('unsupported_operation', 'Required issue write capabilities are unavailable.')

    def api(self, endpoint, payload, method='POST', *, timeout=None):
        return self.adapter.api(self, endpoint, payload, method=method, timeout=timeout)

    def comment_text(self, body, key):
        return self.adapter.comment_text(self, body, key)

    def readback(self, key, action, *, account_checked=False):
        try:
            if not account_checked:
                self.account_check()
            return self.adapter.readback(self, key, action)
        except (ApiError, ValueError, TypeError, KeyError) as exc:
            if getattr(exc, 'code', None) in ('cancelled', 'account_changed', 'invalid_request', 'resource_limit', 'integrity_failed'):
                raise
            if action['status'] == 'pending':
                raise
            raise ApiError('uncertain', 'Cannot establish complete publication read-back; no retry sent.', exit_code=5) from exc

    def confirm(self, key, action, observation):
        found = observation['matches'][0]
        action.update(status='complete', receipt=found, evidence=self.adapter.evidence(found))
        if key == 'create-pr':
            self.state.update(pr=self.adapter.change_number(found), managed=True)
        self.save()
        return found

    def reconcile(self):
        """Recover uncertain accepted writes before deriving semantic input."""
        account_checked = False
        for key, action in self.state.get('actions', {}).items():
            if action['status'] in ('pending', 'submitting') and ('body' in action or key == 'create-pr'):
                if not account_checked:
                    self.account_check()
                    account_checked = True
                observation = self.readback(key, action, account_checked=True)
                if observation['outcome'] == 'absent' and action['status'] == 'pending':
                    continue
                if observation['outcome'] != 'accepted':
                    raise ApiError('uncertain', 'Inspect the saved write with --inspect-writes before recovery.',
                                   exit_code=5, action=key, readback=observation)
                self.confirm(key, action, observation)

    def recover(self, retry_absent=None):
        """An explicit absence decision permits a later invocation to resend."""
        actions = self.state.get('actions', {})
        if retry_absent is not None and retry_absent not in actions:
            raise ApiError('invalid_request', 'Unknown publication action.')
        selected = {retry_absent: actions[retry_absent]} if retry_absent is not None else {
            k: a for k, a in actions.items() if 'body' in a or k == 'create-pr'}
        inspected = {}
        if selected:
            self.account_check()
        for key, action in selected.items():
            observation = self.readback(key, action, account_checked=True)
            if retry_absent is not None:
                if observation['outcome'] == 'accepted':
                    self.confirm(key, action, observation)
                elif observation['outcome'] == 'absent' and action['status'] == 'submitting' and not action.get('receipt'):
                    previous = copy.deepcopy({k: v for k, v in action.items() if k != 'history'})
                    action.setdefault('history', []).append({'decision': 'retry_absent', 'at': now(),
                        'viewer': self.state['viewer'], 'previous': previous, 'readback': observation})
                    action['status'] = 'pending'
                    self.save()
                else:
                    raise ApiError('uncertain', 'Only an absent uncertain write without a receipt can be retried.',
                                   exit_code=5, action=key, readback=observation)
            inspected[key] = {**copy.deepcopy(action), 'readback': observation}
        return inspected

    def attention_body(self, body, key):
        saved = self.state.get('actions', {}).get(key)
        if saved is not None:
            # Old intents keep their exact format; new ones record the undecorated input.
            if saved.get('source_body', saved['body']) != body:
                raise ApiError('action_changed', 'Saved publication content changed.')
            return saved['body']
        if key.startswith(('questions:', 'plan:', 'blockers:', 'execution-stop:', 'approval-rejected:')):
            return '@' + self.state['viewer'] + '\n\n' + body
        return body

    def comment(self, body, key, *, approval=False):
        source_body = body
        body = self.attention_body(body, key)
        action = self.state.setdefault('actions', {}).setdefault(key, {'status': 'pending', 'body': body, 'source_body': source_body, 'approval': approval})
        if action['body'] != body:
            raise ApiError('action_changed', 'Saved publication content changed.')
        expected = self.adapter.prepare_comment(self, body, key)
        if action.get('operation', expected) != expected:
            raise ApiError('action_changed', 'Saved comment operation differs from the current destination or wording.')
        if action['status'] == 'pending':
            action.setdefault('operation', expected)
        self.save()
        observation = self.readback(key, action)
        if observation['outcome'] == 'accepted':
            return self.confirm(key, action, observation)
        if action['status'] in ('submitting', 'complete') or observation['outcome'] != 'absent':
            raise ApiError('uncertain', 'Comment outcome is uncertain or its receipt was edited/deleted; inspect GitHub.', exit_code=5)
        self.guard()
        self.account_check()
        self.adapter.validate(self, action['operation'])
        action['status'] = 'submitting'
        self.save()
        try:
            self.adapter.submit(self, action['operation'])
        except ApiError as exc:
            if exc.code in ('cancelled', 'resource_limit', 'integrity_failed', 'account_changed'):
                raise
            if exc.code == 'publication_rejected':
                action['status'] = 'pending'
                self.save()
                raise ApiError('publication_rejected', str(exc)) from exc
            raise ApiError('uncertain', 'Cannot confirm issue comment; retry to reconcile.', exit_code=5) from exc
        observation = self.readback(key, action)
        if observation['outcome'] != 'accepted':
            raise ApiError('uncertain', 'Comment not confirmed by read-back.', exit_code=5)
        return self.confirm(key, action, observation)

    def push(self, sha, expected, worktree):
        urls = self.repo.git('config', '--get-all', f'remote.{self.repo.remote}.pushurl', check=False).stdout.splitlines()
        if not urls:
            urls = self.repo.git('config', '--get-all', f'remote.{self.repo.remote}.url').stdout.splitlines()
        if not urls or any(normalize_remote(url) != self.repo.identity for url in urls):
            raise ApiError('target_changed', 'Configured push URL differs from the selected repository.')
        key = 'push:' + sha
        action = self.state.setdefault('actions', {}).setdefault(key, {'status': 'pending', 'sha': sha, 'expected': expected})
        branch = self.state['remote_branch']
        def remote():
            result = self.repo.git('ls-remote', self.repo.remote, 'refs/heads/' + branch)
            lines = result.stdout.splitlines()
            return lines[0].split()[0] if lines else None
        observed = remote()
        if observed == sha:
            action.update(status='complete', confirmed_at=now())
            self.state['remote_head'] = sha
            self.save()
            return
        if observed != expected:
            raise ApiError('unexpected_head', 'Remote branch changed; no push performed.')
        self.guard()
        self.account_check()
        action['status'] = 'submitting'
        self.save()
        # Normal Git push honors pre-push hooks. No implicit remote/ref mapping.
        result = command(['git', 'push', self.repo.remote, sha + ':refs/heads/' + branch], cwd=worktree, check=False)
        observed = remote()
        if observed == sha:
            action.update(status='complete', confirmed_at=now())
            self.state['remote_head'] = sha
            self.save()
            return
        if observed != expected:
            raise ApiError('uncertain', 'Push outcome differs from the recorded target; inspect GitHub.', exit_code=5)
        raise ApiError('push_failed', result.stderr or 'Push was rejected; inspect hooks and branch rules.')

    def plan_comment_url(self):
        return self.adapter.plan_comment_url(self)

    def create_pr(self):
        key = 'create-pr'
        action = self.state.setdefault('actions', {}).setdefault(key, {'status': 'pending'})
        self.save()
        observation = self.readback(key, action)
        if observation['outcome'] != 'accepted':
            if action['status'] in ('submitting', 'complete') or observation['outcome'] != 'absent':
                raise ApiError('uncertain', 'PR creation outcome is uncertain; inspect GitHub before retrying.', exit_code=5)
            self.guard()
            self.account_check()
            operation = self.adapter.prepare_creation(self, action)
            if action.get('payload') and action['payload'] != operation['payload']:
                action.setdefault('history', []).append({'decision': 'link_plan', 'at': now(), 'previous_payload': copy.deepcopy(action['payload'])})
            action.update(operation=operation, payload=copy.deepcopy(operation['payload']))
            self.adapter.validate(self, action['operation'])
            action['status'] = 'submitting'
            self.save()
            try:
                self.adapter.submit(self, action['operation'])
            except ApiError as exc:
                if exc.code in ('cancelled', 'resource_limit', 'integrity_failed', 'account_changed'):
                    raise
                if exc.code == 'publication_rejected':
                    action['status'] = 'pending'
                    self.save()
                    raise ApiError('bootstrap_rejected', 'GitHub rejected draft PR creation: ' + str(exc)) from exc
                raise ApiError('uncertain', 'Cannot confirm PR creation; retry to reconcile.', exit_code=5) from exc
            observation = self.readback(key, action)
        if observation['outcome'] != 'accepted':
            raise ApiError('uncertain', 'PR creation did not produce one confirmed PR.', exit_code=5)
        return self.confirm(key, action, observation)

    def confirm_draft(self, desired):
        for key, action in self.state.get('actions', {}).items():
            if key.startswith('draft:') and action.get('status') == 'submitting':
                action['status'] = 'complete' if key.startswith('draft:' + str(desired) + ':') else 'superseded'
        self.save()

    def draft(self, desired):
        self.guard()
        self.account_check()
        observed = self.adapter.draft_state(self)
        key = 'draft:' + str(desired) + ':' + self.state['remote_head']
        if observed['draft'] is desired:
            self.confirm_draft(desired)
            return
        previous = self.state.setdefault('actions', {}).get(key)
        action = {'status': 'submitting', 'desired': desired, 'history': copy.deepcopy(previous.get('history', [])) if previous else []}
        if previous:
            action['history'].append({k: v for k, v in previous.items() if k != 'history'})
        self.state['actions'][key] = action
        self.save()
        try:
            self.adapter.set_draft(self, observed['id'], desired)
        except (ApiError, ValueError, TypeError, KeyError) as exc:
            if getattr(exc, 'code', None) in ('cancelled', 'resource_limit', 'integrity_failed', 'account_changed'):
                raise
        try:
            confirmed = self.adapter.draft_state(self)['draft'] is desired
        except (ApiError, ValueError, TypeError, KeyError) as exc:
            if getattr(exc, 'code', None) in ('cancelled', 'resource_limit', 'integrity_failed', 'account_changed'):
                raise
            raise ApiError('uncertain', 'Draft transition not confirmed; retry to read back.', exit_code=5) from exc
        if not confirmed:
            raise ApiError('uncertain', 'Draft transition not confirmed; retry to read back.', exit_code=5)
        self.confirm_draft(desired)
