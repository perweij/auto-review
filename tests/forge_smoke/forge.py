"""Native fixture operations, separate from application operations under test."""
import json
from pathlib import Path
from urllib.parse import quote, urlsplit
from .runner import Failure


class Forge:
    def __init__(self, run, kind, source):
        self.run, self.kind, self.source = run, kind, Path(source).resolve()
        _, remote = run.command(['git', 'remote', 'get-url', 'origin'], cwd=self.source)
        remote = remote.strip()
        parsed = urlsplit(remote)
        host = 'github.com' if kind == 'github' else 'gitlab.com'
        if parsed.scheme != 'https' or parsed.netloc != host or parsed.username or parsed.query or parsed.fragment:
            raise Failure('Smoke targets require an HTTPS origin on ' + host)
        self.repo = parsed.path.strip('/').removesuffix('.git')
        if len(self.repo.split('/')) < 2:
            raise Failure('Invalid repository origin')
        self.remote, self.host = remote, host
        self.endpoint = ('repos/' + self.repo if kind == 'github' else 'projects/' + quote(self.repo, safe=''))
        self.cwd = run.directory / kind / 'repo'

    def api(self, suffix='', method='GET', body=None):
        args = (['gh', 'api', '--hostname', self.host] if self.kind == 'github' else ['glab', 'api', '--hostname', self.host])
        args += ['--method', method, self.endpoint + suffix]
        if body is not None:
            args += ['--input', '-']
        _, value = self.run.command(args, input_text=json.dumps(body) if body is not None else None)
        return json.loads(value) if value.strip() else None

    def preflight(self):
        tool = 'gh' if self.kind == 'github' else 'glab'
        try:
            _, raw = self.run.command([tool, 'api', '--hostname', self.host, 'user'])
            profile = json.loads(raw)
            account = {'id': profile['id'], 'login': profile.get('login') or profile['username']}
            if type(account['id']) is not int or not isinstance(account['login'], str):
                raise ValueError('Unusable account identity')
            (self.run.directory / self.run.data['commands'][-1]['stdout']).write_text(json.dumps(account) + '\n')
        except (Failure, ValueError, KeyError, TypeError) as exc:
            source = '. ../.secret_gh' if self.kind == 'github' else '. ../.secret-glab'
            raise Failure('Authenticate ' + tool + ' or run `' + source + '`, then retry. Details: ' + str(exc)) from exc
        previous = self.run.data.setdefault('accounts', {}).get(self.kind)
        if previous and previous['id'] != account['id']:
            raise Failure('Authenticated account changed since this run')
        self.run.data['accounts'][self.kind] = account
        self.run.save()
        info = self.api()
        default = info['default_branch']
        _, head = self.run.command(['git', 'ls-remote', self.remote, 'refs/heads/' + default])
        if not head.strip():
            raise Failure('Default branch unavailable')
        _, status = self.run.command(['git', 'status', '--porcelain=v1'], cwd=self.source)
        previous = self.run.data.setdefault('targets', {}).get(self.kind)
        if previous and (previous['repo'] != self.repo or previous['default_sha'] != head.split()[0] or previous['status'] != status):
            raise Failure('Original target changed since this run; inspect before resuming')
        self.run.data['targets'][self.kind] = dict(repo=self.repo, default=default,
            default_sha=head.split()[0], source=str(self.source), status=status)
        self.run.save()
        if not self.cwd.exists():
            self.cwd.parent.mkdir(exist_ok=True)
            self.run.command(['git', 'clone', '--quiet', self.remote, str(self.cwd)])
        self.run.command(['git', 'push', '--dry-run', 'origin', 'HEAD:refs/heads/auto-review-smoke-preflight'], cwd=self.cwd)

    def fixture(self):
        saved = self.run.data.setdefault('fixtures', {}).get(self.kind)
        if saved and saved.get("number"):
            return saved
        prefix = 'auto-review-smoke/' + self.run.directory.name
        base, branch = prefix + '/base', prefix + '/bug'
        git = lambda *args: self.run.command(['git', *args], cwd=self.cwd)
        git('config', 'user.name', 'Auto-review smoke')
        git('config', 'user.email', 'smoke@example.invalid')
        self.run.data['fixtures'][self.kind] = dict(base=base, branch=branch)
        self.run.save()
        code, _ = self.run.command(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/' + base], cwd=self.cwd, check=False)
        git('switch', base) if code == 0 else git('switch', '-c', base)
        (self.cwd / 'price.py').write_text('def total(price, quantity):\n    """Total cost of quantity items."""\n    return price * quantity\n')
        (self.cwd / 'test_price.py').write_text('import unittest\nfrom price import total\n\nclass PriceTest(unittest.TestCase):\n    def test_total(self):\n        self.assertEqual(total(3, 4), 12)\n')
        git('add', 'price.py', 'test_price.py')
        code, _ = self.run.command(['git', 'diff', '--cached', '--quiet'], cwd=self.cwd, check=False)
        if code:
            git('commit', '-qm', 'Smoke fixture baseline')
        git('push', 'origin', base)
        _, base_sha = git('rev-parse', base)
        self.run.data['objects'].append(dict(kind=self.kind, type='branch', name=base, sha=base_sha.strip()))
        self.run.save()
        code, _ = self.run.command(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch], cwd=self.cwd, check=False)
        git('switch', branch) if code == 0 else git('switch', '-c', branch)
        (self.cwd / 'price.py').write_text('def total(price, quantity):\n    """Total cost of quantity items."""\n    return price + quantity\n')
        code, _ = self.run.command(['git', 'diff', '--quiet'], cwd=self.cwd, check=False)
        if code:
            git('commit', '-qam', 'Introduce arithmetic regression')
        git('push', 'origin', branch)
        _, branch_sha = git('rev-parse', branch)
        self.run.data['objects'].append(dict(kind=self.kind, type='branch', name=branch, sha=branch_sha.strip()))
        self.run.save()
        body = 'Disposable auto-review functional fixture. Expected total(3, 4) is 12. Run python3 -m unittest. Run: ' + self.run.directory.name
        payload = dict(title='Auto-review smoke: arithmetic regression', body=body, head=branch, base=base) if self.kind == 'github' else dict(title='Auto-review smoke: arithmetic regression', description=body, source_branch=branch, target_branch=base)
        query = ('/pulls?state=all&head=' + quote(self.repo.split('/')[0] + ':' + branch, safe='') if self.kind == 'github' else '/merge_requests?scope=all&source_branch=' + quote(branch, safe=''))
        existing = self.api(query)
        if len(existing) > 1:
            raise Failure('Ambiguous fixture creation; inspect remote changes')
        result = existing[0] if existing else self.api('/pulls' if self.kind == 'github' else '/merge_requests', 'POST', payload)
        saved = dict(number=result['number'] if self.kind == 'github' else result['iid'],
                     url=result['html_url'] if self.kind == 'github' else result['web_url'], base=base, branch=branch)
        self.run.data['fixtures'][self.kind] = saved
        self.run.data['objects'].append(dict(kind=self.kind, type='change', **saved))
        self.run.save()
        return saved

    def cleanup(self):
        """Close exact recorded fixtures; default branches are never deleted."""
        if self.kind == 'github':
            recorded = {o['number'] for o in self.run.data['objects'] if o['type'] == 'managed-change'}
            for item in list(self.run.data['objects']):
                if item['kind'] != 'github' or item['type'] != 'issue':
                    continue
                state = self.run.api(dict(command='issue-read', issue=item['number'], repository=self.repo, cwd=str(self.cwd)), self.cwd)
                if state.get('pr') and state['pr'] not in recorded:
                    actual = self.api('/pulls/' + str(state['pr']))
                    marker = '<!-- fix-issue workflow=' + state['id'] + ' -->'
                    if marker not in (actual.get('body') or '') or actual['head']['sha'] != state.get('remote_head'):
                        raise Failure('Cleanup refused: unrecorded managed PR needs inspection')
                    self.run.data['objects'].append(dict(kind='github', type='managed-change', number=state['pr'],
                        marker=marker, branch=actual['head']['ref'], sha=actual['head']['sha']))
                    recorded.add(state['pr'])
                    self.run.save()
        owned = [o for o in self.run.data['objects'] if o['kind'] == self.kind]
        prefix = 'auto-review-smoke/' + self.run.directory.name + '/'
        for obj in reversed(owned):
            if obj.get('cleaned'):
                continue
            if obj['type'] == 'managed-change':
                suffix = '/pulls/' + str(obj['number'])
                actual = self.api(suffix)
                if obj['marker'] not in (actual.get('body') or '') or actual['head']['ref'] != obj['branch'] or actual['head']['sha'] != obj['sha']:
                    raise Failure('Cleanup refused: managed PR identity/head changed')
                self.api(suffix, 'PATCH', {'state': 'closed'})
                _, observed = self.run.command(['git', 'ls-remote', self.remote, 'refs/heads/' + obj['branch']])
                if observed.strip():
                    if observed.split()[0] != obj['sha']:
                        raise Failure('Cleanup refused: managed branch changed')
                    self.api('/git/refs/heads/' + obj['branch'], 'DELETE')
            elif obj['type'] == 'change':
                suffix = ('/pulls/' if self.kind == 'github' else '/merge_requests/') + str(obj['number'])
                actual = self.api(suffix)
                branch = actual['head']['ref'] if self.kind == 'github' else actual['source_branch']
                if branch != obj['branch'] or not branch.startswith(prefix):
                    raise Failure('Cleanup refused: fixture branch identity changed')
                self.api(suffix, 'PATCH' if self.kind == 'github' else 'PUT',
                         {'state': 'closed'} if self.kind == 'github' else {'state_event': 'close'})
            elif obj['type'] == 'issue':
                actual = self.api('/issues/' + str(obj['number']))
                if self.run.directory.name not in (actual.get('body') or ''):
                    raise Failure('Cleanup refused: issue no longer has run identity')
                self.api('/issues/' + str(obj['number']), 'PATCH', {'state': 'closed'})
            elif obj['type'] == 'branch':
                if not obj['name'].startswith(prefix):
                    raise Failure('Cleanup refused: branch is outside the run namespace')
                _, observed = self.run.command(['git', 'ls-remote', self.remote, 'refs/heads/' + obj['name']])
                if observed.strip() and observed.split()[0] != obj.get('sha'):
                    raise Failure('Cleanup refused: branch changed since the recorded test operation')
                if not observed.strip():
                    obj['cleaned'] = True
                    self.run.save()
                    continue
                if self.kind == 'github':
                    self.api('/git/refs/heads/' + obj['name'], 'DELETE')
                else:
                    self.api('/repository/branches/' + quote(obj['name'], safe=''), 'DELETE')
            else:
                raise Failure('Cleanup refused: unknown recorded object type')
            obj['cleaned'] = True
            self.run.save()

    def discussion(self, fixture):
        body = 'Smoke fixture context: use standard-library tests and preserve README. Run: ' + self.run.directory.name
        suffix = ('/issues/%s/comments' if self.kind == 'github' else '/merge_requests/%s/notes') % fixture['number']
        rows = self.pages(suffix)
        matches = [r for r in rows if r.get('body') == body]
        if len(matches) > 1:
            raise Failure('Duplicate fixture context comments')
        note = matches[0] if matches else self.api(suffix, 'POST', {'body': body})
        fixture['context_note'] = note['id']
        self.run.save()

    def pages(self, suffix):
        rows, seen = [], set()
        for page in range(1, 101):
            values = self.api(suffix + ('&' if '?' in suffix else '?') + 'per_page=100&page=' + str(page))
            if not isinstance(values, list):
                raise Failure('Expected a paginated fixture collection')
            for value in values:
                if value['id'] in seen:
                    raise Failure('Fixture collection changed during pagination; retry inspection')
                seen.add(value['id'])
                rows.append(value)
            if len(values) < 100:
                return rows
        raise Failure('Fixture collection exceeded the bounded pagination limit')

    def issue(self, title, body):
        """Recover a fixture creation before retrying a potentially accepted POST."""
        title += ' [' + self.run.directory.name + ']'
        saved = self.run.data.setdefault('issue_intents', {}).get(title)
        if saved:
            result = self.api('/issues/' + str(saved))
        else:
            candidates = [r for r in self.pages('/issues?state=all') if r['title'] == title and 'pull_request' not in r]
            if len(candidates) > 1:
                raise Failure('Ambiguous issue fixture creation; inspect remote issues')
            result = candidates[0] if candidates else self.api('/issues', 'POST', dict(title=title, body=body))
        if result.get('body') != body or result['state'] != 'open':
            raise Failure('Existing issue fixture was edited or closed; start a fresh run')
        self.run.data['issue_intents'][title] = result['number']
        if not any(o['kind'] == 'github' and o['type'] == 'issue' and o['number'] == result['number'] for o in self.run.data['objects']):
            self.run.data['objects'].append(dict(kind='github', type='issue', number=result['number'], url=result['html_url']))
        self.run.save()
        return result
