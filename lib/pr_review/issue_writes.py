"""Journaled GitHub operations. Models never receive these capabilities."""
import copy
import json
import re
from .common import ApiError, command, digest, now
from .issue_eligibility import pages
from .repository import normalize_remote


class Writes:
    def __init__(self, repo, state, save, guard):
        self.repo, self.state, self.save, self.guard = repo, state, save, guard

    def api(self, endpoint, payload, method='POST'):
        return json.loads(command(['gh', 'api', '--hostname', self.repo.host, '-X', method, endpoint, '--input', '-'],
                                 cwd=self.repo.cwd, input_text=json.dumps(payload)).stdout)

    def comment_text(self, body, key):
        return body + '\n\n' + f'<!-- fix-issue workflow={self.state["id"]} action={digest(key)[:24]} -->'

    def readback(self, key, action):
        """Inspect one saved intent without sending or changing its journal."""
        receipt = action.get('receipt', {})
        if key == 'create-pr':
            marker = '<!-- fix-issue workflow=' + self.state['id'] + ' -->'
            candidates = pages(self.repo, f'repos/{self.repo.name}/pulls?state=all&head='
                               + self.repo.name.split('/')[0] + ':' + self.state['remote_branch'])
            found = [p for p in candidates if marker in (p.get('body') or '')
                     and p['base']['ref'] == self.state['base_branch']
                     and p['head']['ref'] == self.state['remote_branch']
                     and (p['head'].get('repo') or {}).get('full_name', '').lower() == self.repo.name
                     and p['user']['login'].lower() == self.state['viewer'].lower()
                     and p.get('body') == action.get('payload', {}).get('body', p.get('body'))]
        elif 'body' in action:
            text = self.comment_text(action['body'], key)
            marker = text.rsplit('\n\n', 1)[1]
            comments = pages(self.repo, f'repos/{self.repo.name}/issues/{self.state["issue"]}/comments')
            candidates = [c for c in comments if marker in (c.get('body') or '') or c.get('id') == receipt.get('id')]
            found = [c for c in candidates if c.get('body') == text
                     and c.get('user', {}).get('login', '').lower() == self.state['viewer'].lower()]
        else:
            raise ApiError('invalid_request', 'Only comment and PR-creation intents support this recovery operation.')
        if len(found) == len(candidates) == 1 and (not receipt or found[0].get('id') == receipt.get('id')):
            outcome = 'accepted'
        elif candidates or receipt or action['status'] == 'complete':
            outcome = 'ambiguous' if len(candidates) > 1 else 'changed'
        else:
            outcome = 'absent'
        return {'outcome': outcome, 'candidates': candidates, 'matches': found}

    def confirm(self, key, action, observation):
        found = observation['matches'][0]
        action.update(status='complete', receipt=found)
        if key == 'create-pr':
            self.state.update(pr=found['number'], managed=True)
        self.save()
        return found

    def reconcile(self):
        """Recover uncertain accepted writes before deriving semantic input."""
        for key, action in self.state.get('actions', {}).items():
            if action['status'] in ('pending', 'submitting') and ('body' in action or key == 'create-pr'):
                observation = self.readback(key, action)
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
        for key, action in selected.items():
            observation = self.readback(key, action)
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

    def comment(self, body, key, *, approval=False):
        action = self.state.setdefault('actions', {}).setdefault(key, {'status': 'pending', 'body': body, 'approval': approval})
        if action['body'] != body:
            raise ApiError('action_changed', 'Saved publication content changed.')
        text = self.comment_text(body, key)
        endpoint = f'repos/{self.repo.name}/issues/{self.state["issue"]}/comments'
        self.save()
        observation = self.readback(key, action)
        if observation['outcome'] == 'accepted':
            return self.confirm(key, action, observation)
        if action['status'] in ('submitting', 'complete') or observation['outcome'] != 'absent':
            raise ApiError('uncertain', 'Comment outcome is uncertain or its receipt was edited/deleted; inspect GitHub.', exit_code=5)
        self.guard()
        action['status'] = 'submitting'
        self.save()
        try:
            self.api(endpoint, {'body': text})
            observation = self.readback(key, action)
        except ApiError as exc:
            if re.search(r'HTTP (401|403|404|422)\b', str(exc)):
                action['status'] = 'pending'
                self.save()
                raise ApiError('publication_rejected', str(exc)) from exc
            raise ApiError('uncertain', 'Cannot confirm issue comment; retry to reconcile.', exit_code=5) from exc
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
        key = 'plan:' + str(self.state['plan']['revision'])
        action = self.state.get('actions', {}).get(key, {})
        receipt = action.get('receipt', {})
        comment_id = receipt.get('id')
        if (action.get('status') != 'complete' or type(comment_id) is not int or comment_id <= 0
                or 'body' not in action or receipt.get('body') != self.comment_text(action['body'], key)
                or receipt.get('user', {}).get('login', '').lower() != self.state['viewer'].lower()):
            raise ApiError('facts_incomplete', 'A confirmed current plan comment is required before PR creation.')
        # Build from trusted repository identity and a numeric receipt, never a
        # model-written URL or prose that could add closing directives.
        return f'https://{self.repo.identity}/issues/{self.state["issue"]}#issuecomment-{comment_id}'

    def create_pr(self):
        key = 'create-pr'
        marker = '<!-- fix-issue workflow=' + self.state['id'] + ' -->'
        action = self.state.setdefault('actions', {}).setdefault(key, {'status': 'pending'})
        endpoint = f'repos/{self.repo.name}/pulls'
        self.save()
        observation = self.readback(key, action)
        if observation['outcome'] != 'accepted':
            if action['status'] in ('submitting', 'complete') or observation['outcome'] != 'absent':
                raise ApiError('uncertain', 'PR creation outcome is uncertain; inspect GitHub before retrying.', exit_code=5)
            body = f'Closes #{self.state["issue"]}\n\nPlan: {self.plan_comment_url()}\n\n{marker}'
            self.guard()
            if 'payload' in action and action['payload']['body'] != body:
                # Only an absent, pending intent may change. Keep uncertain and
                # accepted payloads intact for exact read-back reconciliation.
                action.setdefault('history', []).append({'decision': 'link_plan', 'at': now(),
                    'previous_payload': copy.deepcopy(action['payload'])})
                action['payload']['body'] = body
            action.setdefault('payload', {'title': self.state['title'], 'head': self.state['remote_branch'],
                                         'base': self.state['base_branch'], 'body': body, 'draft': True})
            action['status'] = 'submitting'
            self.save()
            try:
                self.api(endpoint, action['payload'])
                observation = self.readback(key, action)
            except ApiError as exc:
                if re.search(r'HTTP (401|403|404|422)\b', str(exc)):
                    action['status'] = 'pending'
                    self.save()
                    raise ApiError('bootstrap_rejected', 'GitHub rejected draft PR creation: ' + str(exc)) from exc
                raise ApiError('uncertain', 'Cannot confirm PR creation; retry to reconcile.', exit_code=5) from exc
        if observation['outcome'] != 'accepted':
            raise ApiError('uncertain', 'PR creation did not produce one confirmed PR.', exit_code=5)
        return self.confirm(key, action, observation)

    def draft(self, desired):
        self.guard()
        data = self.api(f'graphql', {'query': 'query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){id isDraft state}}}',
                                    'variables': {'owner': self.repo.name.split('/')[0], 'name': self.repo.name.split('/')[1], 'n': self.state['pr']}})
        try:
            pr = data['data']['repository']['pullRequest']
            if data.get('errors') or pr['state'] != 'OPEN':
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError('facts_incomplete', 'Cannot verify draft status.') from exc
        if pr['isDraft'] == desired:
            return
        key = 'draft:' + str(desired) + ':' + self.state['remote_head']
        self.state.setdefault('actions', {})[key] = {'status': 'submitting'}
        self.save()
        verb = 'convertPullRequestToDraft' if desired else 'markPullRequestReadyForReview'
        query = 'mutation($id:ID!){' + verb + '(input:{pullRequestId:$id}){pullRequest{isDraft}}}'
        try:
            result = self.api('graphql', {'query': query, 'variables': {'id': pr['id']}})
            if result.get('errors') or result['data'][verb]['pullRequest']['isDraft'] != desired:
                raise ValueError()
        except (ApiError, KeyError, TypeError, ValueError) as exc:
            raise ApiError('uncertain', 'Draft transition not confirmed; retry to read back.', exit_code=5) from exc
        self.state['actions'][key]['status'] = 'complete'
        self.save()
