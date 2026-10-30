"""Journaled GitHub operations. Models never receive these capabilities."""
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

    def comment(self, body, key, *, approval=False):
        action = self.state.setdefault('actions', {}).setdefault(key, {'status': 'pending', 'body': body, 'approval': approval})
        if action['body'] != body:
            raise ApiError('action_changed', 'Saved publication content changed.')
        marker = f'<!-- fix-issue workflow={self.state["id"]} action={digest(key)[:24]} -->'
        text = body + '\n\n' + marker
        endpoint = f'repos/{self.repo.name}/issues/{self.state["issue"]}/comments'
        self.save()
        def find():
            return [c for c in pages(self.repo, endpoint) if c.get('body') == text
                    and c.get('user', {}).get('login', '').lower() == self.state['viewer'].lower()]
        found = find()
        if found:
            action.update(status='complete', receipt=found[0])
            self.save()
            return found[0]
        if action['status'] in ('submitting', 'complete'):
            raise ApiError('uncertain', 'Comment outcome is uncertain or its receipt was edited/deleted; inspect GitHub.', exit_code=5)
        self.guard()
        action['status'] = 'submitting'
        self.save()
        try:
            self.api(endpoint, {'body': text})
            found = find()
        except ApiError as exc:
            if re.search(r'HTTP (401|403|404|422)\b', str(exc)):
                action['status'] = 'pending'
                self.save()
                raise ApiError('publication_rejected', str(exc)) from exc
            raise ApiError('uncertain', 'Cannot confirm issue comment; retry to reconcile.', exit_code=5) from exc
        if not found:
            raise ApiError('uncertain', 'Comment not confirmed by read-back.', exit_code=5)
        action.update(status='complete', receipt=found[0])
        self.save()
        return found[0]

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

    def create_pr(self):
        key = 'create-pr'
        marker = '<!-- fix-issue workflow=' + self.state['id'] + ' -->'
        action = self.state.setdefault('actions', {}).setdefault(key, {'status': 'pending'})
        endpoint = f'repos/{self.repo.name}/pulls'
        def find():
            return [p for p in pages(self.repo, endpoint + '?state=all&head=' + self.repo.name.split('/')[0] + ':' + self.state['remote_branch'])
                    if marker in (p.get('body') or '') and p['base']['ref'] == self.state['base_branch']
                    and p['head']['ref'] == self.state['remote_branch'] and (p['head'].get('repo') or {}).get('full_name', '').lower() == self.repo.name
                    and p['user']['login'].lower() == self.state['viewer'].lower()]
        self.save()
        found = find()
        if not found:
            if action['status'] == 'submitting':
                raise ApiError('uncertain', 'PR creation outcome is uncertain; inspect GitHub before retrying.', exit_code=5)
            self.guard()
            action['status'] = 'submitting'
            self.save()
            try:
                self.api(endpoint, {'title': self.state['title'], 'head': self.state['remote_branch'], 'base': self.state['base_branch'],
                                    'body': f'Closes #{self.state["issue"]}\n\n{self.state["plan"]["body"]}\n\n{marker}', 'draft': True})
                found = find()
            except ApiError as exc:
                if re.search(r'HTTP (401|403|404|422)\b', str(exc)):
                    action['status'] = 'pending'
                    self.save()
                    raise ApiError('bootstrap_rejected', 'GitHub rejected draft PR creation: ' + str(exc)) from exc
                raise ApiError('uncertain', 'Cannot confirm PR creation; retry to reconcile.', exit_code=5) from exc
        if len(found) != 1:
            raise ApiError('uncertain', 'PR creation did not produce one confirmed PR.', exit_code=5)
        action.update(status='complete', receipt=found[0])
        self.state.update(pr=found[0]['number'], managed=True)
        self.save()
        return found[0]

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
