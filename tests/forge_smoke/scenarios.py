"""Functional assertions through the public CLI and protocol."""
import json
from .runner import Failure, Blocked


def context(run, forge):
    fixture = run.data['fixtures'][forge.kind]
    sessions = run.api(dict(command='discover', cwd=str(forge.cwd), pr=fixture['number']), forge.cwd)['sessions']
    if len(sessions) != 1:
        raise Failure('Expected one retained review session')
    return dict(session=sessions[0]['session'])


def publication(run, forge):
    ctx = context(run, forge)
    def read():
        return run.api(dict(command='read', **ctx), forge.cwd)
    view = read()
    number = run.data['fixtures'][forge.kind]['number']
    existing = [c for c in forge.pages('/pulls/%s/comments' % number) if 'Multiply price by quantity' in c['body']]
    if existing:
        if len(existing) != 1:
            raise Failure('Existing publication is duplicated')
        recovered = run.api(dict(command='resolve', generation=view['generation'], revision=view['revision'], **ctx), forge.cwd)['review']
        if not any(c['status'] == 'published' and c.get('receipt') and 'Multiply price by quantity' in c['body'] for c in recovered['comments']):
            raise Failure('Existing remote comment has no recoverable local receipt')
        if existing[0]['path'] != 'price.py' or existing[0]['line'] != 3:
            raise Failure('Existing publication has wrong placement')
        run.data['fixtures'][forge.kind]['published_comment'] = existing[0]['id']
        run.save()
        return
    for comment in view['comments']:
        if comment['status'] != 'unstaged':
            continue
        result = run.api(dict(command='mutate', operation='stage', comment=comment['id'],
                             generation=view['generation'], revision=view['revision'], **ctx), forge.cwd)
        view = result['review']
    preview = run.api(dict(command='prepare', generation=view['generation'], revision=view['revision'], **ctx), forge.cwd)
    if not preview['ready']:
        raise Failure('Publication preview not ready: ' + str(preview))
    view = preview['review']
    result = run.api(dict(command='publish', generation=view['generation'], revision=view['revision'],
                         confirmation=preview['confirmation'], **ctx), forge.cwd)
    if not result.get('complete'):
        raise Failure('Publication batch did not complete')
    number = run.data['fixtures'][forge.kind]['number']
    comments = forge.pages('/pulls/%s/comments' % number)
    matches = [c for c in comments if 'Multiply price by quantity' in c['body']]
    if len(matches) != 1:
        raise Failure('Expected exactly one inline arithmetic comment, got ' + str(len(matches)))
    if matches[0]['path'] != 'price.py' or matches[0]['line'] != 3:
        raise Failure('Wrong inline placement')
    run.data['fixtures'][forge.kind]['published_comment'] = matches[0]['id']
    run.save()


def issue(run, forge):
    fixture = run.data.setdefault('issue_fixture', {})
    if not fixture:
        created = forge.issue('Smoke: add multiplication module',
            'Add smoke_total.py with total(price, quantity) returning their product. Add test_smoke_total.py using unittest: total(3,4)==12 and total(3,0)==0. Preserve README and other existing files. No dependencies. Run: ' + run.directory.name)
        fixture.update(number=created['number'], url=created['html_url'])
        run.save()
    args = [str(run.root / 'bin/fix-issue'), '-i', str(fixture['number']), '-R', forge.repo,
            '--economical', '--planning', 'codex-1', '--fix', 'codex-1', '--planning-review', 'claude-1',
            '--max-runtime', '60m', '--max-repair-cycles', '1', '--json']
    _, output = run.command(args, cwd=forge.cwd, timeout=3600, check=False)
    result = json.loads(output)
    fixture['initial'] = result
    run.save()
    already_complete = result.get('exit_code') == 0 and bool(result.get('pr_url'))
    if result.get('outcome') != 'approval' and not already_complete:
        raise Failure('Expected approval boundary: ' + str(result.get('error') or result.get('outcome')))
    # Ask the public read service for the exact plan revision; do not infer it.
    state = run.api(dict(command='issue-read', issue=fixture['number'], repository=forge.repo, cwd=str(forge.cwd)), forge.cwd)
    fixture['read'] = state
    fixture['workflow_id'] = state['id']
    run.save()
    plan = state.get('plan') or state.get('workflow', {}).get('plan')
    if not plan:
        raise Failure('Public issue-read did not expose a plan revision')
    if not already_complete:
        if run.data['suite'] == 'extended':
            traces = [run.directory / 'models' / name for name in ('stages', 'issue-stages.jsonl')]
            before = {str(p): p.read_bytes() for p in traces if p.exists()}
            args += ['--codex-1-effort', 'medium', '--claude-1-effort', 'medium']
            _, changed = run.command(args, cwd=forge.cwd, timeout=600, check=False)
            if json.loads(changed).get('outcome') != 'approval':
                raise Failure('Settings change left the approval boundary')
            updated = run.api(dict(command='issue-read', issue=fixture['number'], repository=forge.repo, cwd=str(forge.cwd)), forge.cwd)
            if updated['plan']['revision'] != plan['revision'] or updated['plan']['body'] != plan['body']:
                raise Failure('Settings change unnecessarily regenerated the plan')
            if any(p.read_bytes() != before[str(p)] for p in traces if str(p) in before):
                raise Failure('Settings change launched models at the approval stop')
            fixture['settings_boundary_checked'] = True
            run.save()
        if run.data['suite'] == 'extended':
            from .injection import fault, accepted
            with fault(run, 'create-pr', endpoint='repos/' + forge.repo + '/pulls') as evidence:
                _, output = run.command(args + ['--approve', str(plan['revision'])], cwd=forge.cwd, timeout=3600, check=False)
            accepted(evidence)
            interrupted = json.loads(output)
            if interrupted.get('exit_code') != 5:
                raise Failure('PR creation did not retain an uncertain intent')
            _, inspected = run.command(args + ['--inspect-writes'], cwd=forge.cwd)
            if json.loads(inspected)['actions']['create-pr']['readback']['outcome'] != 'accepted':
                raise Failure('Created PR could not be read back')
            with fault(run, 'ready-transition', endpoint='graphql', body_contains='markPullRequestReadyForReview', fail_readback=True) as evidence:
                _, output = run.command(args, cwd=forge.cwd, timeout=3600, check=False)
            accepted(evidence, readback=True)
            if json.loads(output).get('exit_code') == 0:
                raise Failure('Ready transition unexpectedly succeeded with failed readback')
        _, output = run.command(args + ['--approve', str(plan['revision'])], cwd=forge.cwd, timeout=3600, check=False)
        result = json.loads(output)
    elif run.data['suite'] == 'extended':
        from .injection import accepted
        if not fixture.get('settings_boundary_checked'):
            raise Failure('Missing settings-boundary evidence; start a fresh extended run')
        accepted(run.directory / 'create-pr-fault.json')
        accepted(run.directory / 'ready-transition-fault.json', readback=True)
    fixture['completed'] = result
    run.save()
    if result.get('pr_url'):
        pr_number = int(result['pr_url'].rsplit('/', 1)[-1])
        pr = forge.api('/pulls/' + str(pr_number))
        marker = '<!-- fix-issue workflow=' + fixture['workflow_id'] + ' -->'
        if marker not in (pr.get('body') or ''):
            raise Failure('Generated PR does not carry the recorded workflow identity')
        if not any(o['type'] == 'managed-change' and o['number'] == pr_number for o in run.data['objects']):
            run.data['objects'].append(dict(kind='github', type='managed-change', number=pr_number,
                marker=marker, branch=pr['head']['ref'], sha=pr['head']['sha']))
        run.save()
    if result.get('exit_code') != 0:
        raise Failure('Issue workflow did not complete: ' + str(result.get('error') or result.get('outcome')))
    import re
    url = result.get('pr_url') or ''
    match = re.fullmatch(r'https://github.com/' + re.escape(forge.repo) + r'/pull/(\d+)', url)
    if not match:
        raise Failure('Completed issue has no canonical PR URL')
    number = int(match.group(1))
    remote = forge.api('/pulls/' + str(number))
    if remote['draft'] or remote['state'] != 'open':
        raise Failure('Issue PR did not become ready')
    checkout = run.directory / 'issue-oracle'
    if not checkout.exists():
        run.command(['git', 'clone', '--quiet', '--branch', remote['head']['ref'], '--single-branch', forge.remote, str(checkout)])
    _, head = run.command(['git', 'rev-parse', 'HEAD'], cwd=checkout)
    if head.strip() != remote['head']['sha']:
        raise Failure('Independent checkout has wrong PR head')
    run.command(['python3', '-m', 'unittest', 'test_smoke_total'], cwd=checkout)
    files = forge.api('/pulls/%s/files' % number)
    if {f['filename'] for f in files} != {'smoke_total.py', 'test_smoke_total.py'}:
        raise Failure('Issue implementation changed unexpected files')
    link = re.search(r'https://github.com/' + re.escape(forge.repo) + r'/issues/\d+#issuecomment-(\d+)', remote['body'] or '')
    if not link:
        raise Failure('PR lacks a plan-comment link')
    comment = forge.api('/issues/comments/' + link.group(1))
    if plan['body'] not in comment['body']:
        raise Failure('PR plan link does not contain the approved plan')
    traces = [run.directory / 'models' / name for name in ('stages', 'issue-stages.jsonl')]
    before = {str(p): p.read_bytes() for p in traces if p.exists()}
    _, output = run.command(args, cwd=forge.cwd, timeout=3600, check=False)
    if json.loads(output).get('exit_code') != 0:
        raise Failure('Completed issue failed on resume')
    if any(p.read_bytes() != before[str(p)] for p in traces if str(p) in before):
        raise Failure('Completed issue launched new model stages')



def owner(run, forge):
    number = run.data['fixtures']['github']['number']
    view = run.api(dict(command='owner-open', cwd=str(forge.cwd), repository=forge.repo, pr=number), forge.cwd)
    def operation(command, **fields):
        nonlocal view
        result = run.api(dict(command=command, session=view['session'], revision=view['revision'], **fields), forge.cwd)
        view = result.get('owner', result)
        return result
    operation('owner-refresh')
    feedback = [item for item in view['feedback'] if item['kind'] == 'thread']
    if not feedback:
        raise Failure('Published inline finding not visible to Owner')
    item = feedback[0]['key']
    operation('owner-mutate', operation='reply', item=item, body='Smoke acknowledgment: multiplication regression reproduced.')
    reply = view['replies'][-1]['id']
    operation('owner-mutate', operation='stage', reply=reply)
    prepared = operation('owner-prepare')
    operation('owner-publish', token=prepared['token'])
    comments = forge.pages('/pulls/%s/comments' % number)
    if len([c for c in comments if 'Smoke acknowledgment: multiplication regression reproduced.' in c['body']]) != 1:
        raise Failure('Expected one published Owner reply')
    for resolved in (True, False):
        operation('owner-thread-state', item=item, resolved=resolved)
    if run.data['suite'] == 'extended':
        body = 'Smoke external native reply ' + run.directory.name
        root_id = run.data['fixtures']['github']['published_comment']
        forge.api('/pulls/%s/comments/%s/replies' % (number, root_id), 'POST', {'body': body})
        operation('owner-refresh')
        if body not in json.dumps(view['feedback']):
            raise Failure('Owner refresh omitted a native external reply')
        query = 'mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}'
        run.command(['gh', 'api', '--hostname', 'github.com', 'graphql', '--input', '-'], input_text=json.dumps({'query': query, 'variables': {'id': item.split(':', 1)[1]}}))
        operation('owner-refresh')
        if not next(i for i in view['feedback'] if i['key'] == item)['is_resolved']:
            raise Failure('Owner refresh omitted native thread resolution')
        operation('owner-thread-state', item=item, resolved=False)
    run.data['owner_session'] = view['session']
    run.save()


def integrity(run, forges):
    for forge in forges:
        target = run.data['targets'][forge.kind]
        _, head = run.command(['git', 'ls-remote', forge.remote, 'refs/heads/' + target['default']])
        _, status = run.command(['git', 'status', '--porcelain=v1'], cwd=forge.source)
        if head.split()[0] != target['default_sha'] or status != target['status']:
            raise Failure('Original checkout/default branch changed for ' + forge.kind)


def publication_recovery(run, forge):
    import os
    import shutil
    from .runner import atomic_json
    ctx = context(run, forge)
    view = run.api(dict(command='read', **ctx), forge.cwd)
    body = 'Smoke lost-response recovery ' + run.directory.name
    def request(command, **fields):
        return run.api(dict(command=command, generation=view['generation'], revision=view['revision'], **ctx, **fields), forge.cwd)
    result = request('mutate', operation='create', body=body, placement={'kind': 'review'})
    view = result['review']
    identity = result['changed'][0]
    view = request('mutate', operation='stage', comment=identity)['review']
    preview = request('prepare')
    if not preview['ready']:
        raise Failure('Recovery fixture not ready')
    view = preview['review']
    config = run.directory / 'fault.json'
    number = run.data['fixtures']['github']['number']
    atomic_json(config, dict(executable=shutil.which('gh'), method='POST', endpoint='repos/%s/pulls/%s/reviews' % (forge.repo, number), mode='after'))
    wrapper = run.directory / 'fault-bin'
    wrapper.mkdir(exist_ok=True)
    (wrapper / 'gh').symlink_to(run.root / 'tests/forge_smoke/fault.py')
    old_path = run.env['PATH']
    run.env.update(PATH=str(wrapper)+os.pathsep+old_path, SMOKE_FAULT_FILE=str(config))
    try:
        try:
            request('publish', confirmation=preview['confirmation'])
        except Failure:
            pass  # Both explicit uncertainty and partial-result responses are valid.
    finally:
        run.env['PATH'] = old_path
        run.env.pop('SMOKE_FAULT_FILE', None)
    evidence = json.loads(config.read_text())
    if not evidence.get('used') or evidence.get('forwarded_exit') != 0 or not evidence.get('response_bytes'):
        raise Failure('Fault did not hide an accepted remote write')
    view = run.api(dict(command='read', **ctx), forge.cwd)
    recovered = request('resolve')['review']
    comment = next(c for c in recovered['comments'] if c['id'] == identity)
    if comment['status'] != 'published' or not comment.get('receipt'):
        raise Failure('Recovery did not restore the publication receipt')
    reviews = forge.pages('/pulls/%s/reviews' % number)
    if len([r for r in reviews if body in r['body']]) != 1:
        raise Failure('Lost-response recovery did not produce exactly one review')


class Review:
    def __init__(self, run, forge):
        self.run, self.forge, self.ctx = run, forge, context(run, forge)
        self.read()
        self.call('refresh')

    def read(self):
        self.view = self.run.api(dict(command='read', **self.ctx), self.forge.cwd)
        return self.view

    def call(self, command, **fields):
        result = self.run.api(dict(command=command, generation=self.view['generation'],
            revision=self.view['revision'], **self.ctx, **fields), self.forge.cwd)
        if 'review' in result:
            self.view = result['review']
        return result

    def draft(self, body):
        result = self.call('mutate', operation='create', body=body, placement={'kind': 'review'})
        identity = result['changed'][0]
        self.call('mutate', operation='stage', comment=identity)
        return identity


def edited_preview(run, forge):
    review = Review(run, forge)
    body = 'Smoke edited preview ' + run.directory.name
    identity = review.draft(body + ' original')
    prepared = review.call('prepare')
    if not prepared['ready']:
        raise Failure('Edited-preview fixture not ready')
    review.call('mutate', operation='edit', comment=identity, body=body + ' edited')
    try:
        review.call('publish', confirmation=prepared['confirmation'])
    except Failure as exc:
        if 'confirmation_expired' not in str(exc):
            raise
    else:
        raise Failure('Edited draft accepted an obsolete preview')
    review.call('mutate', operation='stage', comment=identity)
    prepared = review.call('prepare')
    review.call('publish', confirmation=prepared['confirmation'])
    rows = forge.pages('/pulls/%s/reviews' % run.data['fixtures']['github']['number'])
    matches = [row for row in rows if body in row['body']]
    if len(matches) != 1 or body + ' edited' not in matches[0]['body']:
        raise Failure('Remote body does not match the freshly confirmed edit')


def stale_head(run, forge):
    review = Review(run, forge)
    body = 'Smoke stale-head refusal ' + run.directory.name
    review.draft(body)
    prepared = review.call('prepare')
    if not prepared['ready']:
        raise Failure('Stale-head fixture not ready')
    run.command(['git', 'commit', '--allow-empty', '-qm', 'Advance smoke review head'], cwd=forge.cwd)
    run.command(['git', 'push', 'origin', run.data['fixtures']['github']['branch']], cwd=forge.cwd)
    _, head = run.command(['git', 'rev-parse', 'HEAD'], cwd=forge.cwd)
    for obj in run.data['objects']:
        if obj['kind'] == 'github' and obj['type'] == 'branch' and obj['name'] == run.data['fixtures']['github']['branch']:
            obj['sha'] = head.strip()
    run.save()
    try:
        result = review.call('publish', confirmation=prepared['confirmation'])
    except Failure as exc:
        if not any(word in str(exc) for word in ('head', 'snapshot', 'source', 'stale')):
            raise
    else:
        if result.get('complete'):
            raise Failure('Stale source was accepted for publication')
    rows = forge.pages('/pulls/%s/reviews' % run.data['fixtures']['github']['number'])
    if any(body in row['body'] for row in rows):
        raise Failure('Stale preview wrote a remote review')


def foreign_issue_commit(run, forge):
    fixture = run.data['issue_fixture']
    result = fixture.get('completed') or {}
    if result.get('exit_code') != 0:
        raise Failure('Foreign-commit scenario requires a completed issue fixture')
    number = int(result['pr_url'].rsplit('/', 1)[-1])
    remote = forge.api('/pulls/' + str(number))
    checkout = run.directory / 'foreign-commit'
    run.command(['git', 'clone', '--quiet', '--branch', remote['head']['ref'], '--single-branch', forge.remote, str(checkout)])
    for key, value in [('user.name', 'Smoke outsider'), ('user.email', 'outsider@example.invalid')]:
        run.command(['git', 'config', key, value], cwd=checkout)
    run.command(['git', 'commit', '--allow-empty', '-qm', 'Unexpected external smoke commit'], cwd=checkout)
    run.command(['git', 'push', 'origin', remote['head']['ref']], cwd=checkout)
    _, head = run.command(['git', 'rev-parse', 'HEAD'], cwd=checkout)
    args = [str(run.root / 'bin/fix-issue'), '-i', str(fixture['number']), '-R', forge.repo, '--json']
    code, output = run.command(args, cwd=forge.cwd, check=False)
    value = json.loads(output)
    if code == 0 or 'unexpected_head' not in json.dumps(value):
        raise Failure('Foreign issue commit was not rejected with unexpected_head')
    if forge.api('/pulls/' + str(number))['head']['sha'] != head.strip():
        raise Failure('Foreign commit was overwritten')
    for obj in run.data['objects']:
        if obj['type'] == 'managed-change' and obj['number'] == number:
            obj['sha'] = head.strip()
    run.save()


def issue_plan_recovery(run, forge, mode):
    import os
    import shutil
    from .runner import atomic_json
    created = forge.issue('Smoke plan recovery: ' + mode,
        'Add smoke_total.py with total(price, quantity) multiplying its arguments. Add standard-library unittest coverage in test_smoke_total.py for (3,4)==12 and (3,0)==0. Preserve README. Run: ' + run.directory.name)
    number = created['number']
    run.save()
    args = [str(run.root / 'bin/fix-issue'), '-i', str(number), '-R', forge.repo, '--economical',
            '--planning', 'codex-1', '--fix', 'codex-1', '--planning-review', 'claude-1', '--json']
    config = run.directory / ('issue-fault-' + mode + '.json')
    atomic_json(config, dict(executable=shutil.which('gh'), method='POST',
        endpoint='repos/%s/issues/%s/comments' % (forge.repo, number), mode=mode))
    wrapper = run.directory / 'fault-bin'
    wrapper.mkdir(exist_ok=True)
    if not (wrapper / 'gh').exists():
        (wrapper / 'gh').symlink_to(run.root / 'tests/forge_smoke/fault.py')
    old_path = run.env['PATH']
    run.env.update(PATH=str(wrapper)+os.pathsep+old_path, SMOKE_FAULT_FILE=str(config))
    try:
        code, output = run.command(args, cwd=forge.cwd, timeout=600, check=False)
    finally:
        run.env['PATH'] = old_path
        run.env.pop('SMOKE_FAULT_FILE', None)
    evidence = json.loads(config.read_text())
    if not evidence.get('used') or (mode != 'before' and evidence.get('forwarded_exit') != 0):
        raise Failure('Issue fault was not injected at the intended write')
    if code != 5:
        raise Failure('Lost plan response did not preserve uncertainty: ' + output[-1000:])
    _, inspected = run.command(args + ['--inspect-writes'], cwd=forge.cwd)
    actions = json.loads(inspected)['actions']
    action = actions.get('plan:1')
    expected = 'absent' if mode == 'before' else 'accepted'
    if not action or action['readback']['outcome'] != expected:
        raise Failure('Wrong independent plan-write readback: ' + str(actions))
    if mode == 'before':
        run.command(args + ['--retry-absent', 'plan:1'], cwd=forge.cwd)
    _, output = run.command(args, cwd=forge.cwd, timeout=600, check=False)
    if json.loads(output).get('outcome') != 'approval':
        raise Failure('Recovered plan did not reach its approval boundary')
    rows = forge.pages('/issues/%s/comments' % number)
    plans = [row for row in rows if 'action=plan:1' in row['body'] or 'key=plan:1' in row['body']]
    if len(plans) != 1:
        # Native comment body remains authoritative; marker spelling is not assumed.
        plans = [row for row in rows if '## Outcome' in row['body'] and '## Verification' in row['body']]
    if len(plans) != 1:
        raise Failure('Plan recovery did not produce exactly one published plan')
    run.data.setdefault('plan_recoveries', {})[mode] = dict(number=number, args=args)
    run.save()


def interrupt_and_lock(run, forge):
    import os
    import signal
    import subprocess
    import time
    fixture = run.data['fixtures']['github']
    marker = run.directory / 'models/model-started'
    marker.unlink(missing_ok=True)
    args = [str(run.root / 'bin/pr-review'), fixture['url'], '--economical', '--no-publish']
    env = dict(run.env, MOCK_SLEEP_STAGE='codex', MOCK_SLEEP_SECONDS='60', SMOKE_ACTOR='application')
    with (run.directory / 'interrupted-review.log').open('w') as log:
        process = subprocess.Popen(args, cwd=forge.cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        try:
            deadline = time.monotonic() + 90
            while not marker.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise Failure('Review never reached the deterministic interruption point')
                time.sleep(0.1)
            code, output = run.command(args, cwd=forge.cwd, check=False, timeout=30)
            last = run.data['commands'][-1]
            error = (run.directory / last['stderr']).read_text()
            if code == 0 or not any(word in (output+error).lower() for word in ('busy', 'another', 'running', 'lock')):
                raise Failure('Concurrent invocation did not stop at the execution lock')
            os.killpg(process.pid, signal.SIGINT)
            if process.wait(timeout=20) != 130:
                raise Failure('Interrupted review did not exit 130')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
    stages = run.directory / 'models/stages'
    before = stages.read_text().splitlines().count('claude')
    run.command(args, cwd=forge.cwd, timeout=300)
    if stages.read_text().splitlines().count('claude') != before:
        raise Failure('Resume discarded the completed independent reviewer')


def approval_changes(run, forge):
    fixture = run.data['plan_recoveries']['after']
    number, args = fixture['number'], fixture['args']
    existing = forge.pages('/issues/%s/comments' % number)
    if not any(r['body'].strip() == '/fix-issue approve 9999' for r in existing):
        forge.api('/issues/%s/comments' % number, 'POST', {'body': '/fix-issue approve 9999'})
    for _ in range(2):
        _, output = run.command(args, cwd=forge.cwd, check=False, timeout=600)
        value = json.loads(output)
        if value.get('eligibility', {}).get('eligibility') == 'indeterminate':
            reasons = [c['message'] for c in value['eligibility']['checks'] if c['outcome'] == 'unknown']
            raise Blocked('Forge facts unavailable: ' + '; '.join(reasons))
        if value.get('outcome') != 'approval':
            raise Failure('Wrong approval escaped the approval boundary')
    rows = forge.pages('/issues/%s/comments' % number)
    if len([r for r in rows if 'Approval for revision 9999 cannot be accepted:' in r['body']]) != 1:
        raise Failure('Wrong-revision approval was not rejected exactly once')
    existing = forge.pages('/issues/%s/comments' % number)
    approved = next((r for r in existing if r['body'].strip() == '/fix-issue approve 1'), None)
    if approved is None:
        approved = forge.api('/issues/%s/comments' % number, 'POST', {'body': '/fix-issue approve 1'})
    original = forge.api('/issues/' + str(number))
    extra = 'Also cover negative quantities in the tests.'
    if extra not in original['body']:
        forge.api('/issues/' + str(number), 'PATCH', {'body': original['body'] + '\n' + extra})
    _, output = run.command(args, cwd=forge.cwd, check=False, timeout=600)
    if json.loads(output).get('outcome') != 'approval':
        raise Failure('Changed requirements reused the old approval')
    state = run.api(dict(command='issue-read', issue=number, repository=forge.repo, cwd=str(forge.cwd)), forge.cwd)
    if state['plan']['revision'] <= 1 or state.get('approval'):
        raise Failure('Changed issue did not invalidate the old approval')
    reactions = forge.pages('/issues/comments/%s/reactions' % approved['id'])
    if not any(r['content'] == 'eyes' for r in reactions):
        raise Failure('Native approval command was not acknowledged')


def changed_plan_receipt(run, forge, mode):
    fixture = run.data['plan_recoveries'][mode]
    number, args = fixture['number'], fixture['args']
    before_state = run.api(dict(command='issue-read', issue=number, repository=forge.repo, cwd=str(forge.cwd)), forge.cwd)
    revision = before_state['plan']['revision']
    _, inspected = run.command(args + ['--inspect-writes'], cwd=forge.cwd)
    action = json.loads(inspected)['actions']['plan:' + str(revision)]
    receipt = action.get('receipt') or {}
    rows = forge.pages('/issues/%s/comments' % number)
    plan = next((r for r in rows if r['id'] == receipt.get('id')), None)
    if mode == 'before':
        if plan is None:
            raise Failure('Plan receipt is missing before edit test')
        if not plan['body'].endswith('External edit.'):
            forge.api('/issues/comments/' + str(plan['id']), 'PATCH', {'body': plan['body'] + '\nExternal edit.'})
    elif plan is not None:
        forge.api('/issues/comments/' + str(plan['id']), 'DELETE')
    count = len(forge.pages('/issues/%s/comments' % number))
    code, output = run.command(args, cwd=forge.cwd, check=False, timeout=600)
    result = json.loads(output)
    expected_count = count
    if mode == 'before' and result.get('outcome') == 'approval':
        state = run.api(dict(command='issue-read', issue=number, repository=forge.repo, cwd=str(forge.cwd)), forge.cwd)
        if state['plan']['revision'] != revision + 1 or state.get('approval') or state.get('pr'):
            raise Failure('Edited plan bypassed fresh revision approval')
        expected_count += 1
    elif code != 5 or 'uncertain' not in output:
        raise Failure('Changed plan receipt did not stop safely')
    _, inspected = run.command(args + ['--inspect-writes'], cwd=forge.cwd)
    action = json.loads(inspected)['actions']['plan:' + str(revision)]
    # A deleted previously receipted write is changed, not safely absent.
    expected_outcome = 'changed'
    if action['readback']['outcome'] != expected_outcome:
        raise Failure('Changed/deleted plan receipt was incorrectly accepted')
    if len(forge.pages('/issues/%s/comments' % number)) != expected_count:
        raise Failure('Changed plan receipt caused duplicate or unexpected comments')
    if mode == 'before':
        forge.api('/issues/' + str(number), 'PATCH', {'state': 'closed'})
        code, output = run.command(args, cwd=forge.cwd, check=False, timeout=600)
        if code == 0 or 'issue_closed' not in output:
            raise Failure('Closed issue did not stop through eligibility')


def emacs_checkouts(run, forge):
    import os
    from pathlib import Path
    ctx = context(run, forge)
    history = run.api(dict(command='history', **ctx), forge.cwd)['generations']
    if len(history) < 2:
        raise Failure('Historical checkout test requires two completed generations')
    cases = []
    historical_available = 0
    for entry in history:
        target = dict(toolkit='review', generation=entry['generation'], **ctx)
        checkout = run.api(dict(command='checkout', **target), forge.cwd)
        if not checkout['available']:
            if entry['current'] or not checkout.get('message'):
                raise Failure('Current checkout unavailable or missing explanation')
            cases.append(dict(missing=True, **target))
            run.data.setdefault('unmapped_history', []).append(entry['generation'])
            continue
        if not entry['current']:
            historical_available += 1
        _, head = run.command(['git', 'rev-parse', 'HEAD'], cwd=checkout['path'])
        if head.strip() != entry['head'] or 'isolated-review-' in checkout['path']:
            raise Failure('Historical checkout does not contain its generation head')
        cases.append(dict(path=checkout['path'], **target))
    if not historical_available:
        raise Failure('No retained historical checkout was available to verify')
    cases.append(dict(toolkit='review', session=None, missing=True))
    issue_state = run.api(dict(command='issue-read', issue=run.data['issue_fixture']['number'], repository=forge.repo,
                              cwd=str(forge.cwd)), forge.cwd)
    owner_session = issue_state['owner']
    owner = run.api(dict(command='checkout', toolkit='owner', session=owner_session), forge.cwd)
    if not owner['available']:
        raise Failure('Completed issue Owner checkout unavailable')
    cases.append(dict(toolkit='owner', session=owner_session, path=owner['path']))
    args = ['emacs', '--batch', '--no-site-file', '-L', str(run.root / 'emacs')]
    setup = os.environ.get('AUTO_REVIEW_TEST_LOAD_FILE')
    if setup:
        args += ['-l', setup]
    else:
        dependencies = Path(os.environ.get('AUTO_REVIEW_EMACS_DEPENDENCY_ROOT', '/opt/emacs-lisp'))
        for name in ('compat', 'cond-let', 'llama', 'seq', 'with-editor', 'transient', 'magit', 'closql', 'emacsql', 'ghub', 'treepy', 'markdown-mode', 'yaml', 'forge'):
            directory = dependencies / name
            if (directory / 'lisp').is_dir():
                directory /= 'lisp'
            if directory.is_dir():
                args += ['-L', str(directory)]
    args += ['-l', str(run.root / 'emacs/tests/bootstrap.el'), '-l', str(run.root / 'tests/forge_smoke/emacs-checkout.el')]
    env = dict(run.env, SMOKE_BACKEND=str(run.root / 'bin/pr-review'), SMOKE_EMACS_CASES=json.dumps(cases))
    run.command(args, cwd=forge.cwd, env=env)
    selected = next(case for case in cases if case.get('path'))
    missing = Path(selected['path'])
    hidden = missing.with_name(missing.name + '-temporarily-unavailable')
    missing.rename(hidden)
    try:
        env['SMOKE_EMACS_CASES'] = json.dumps([{**selected, 'missing': True}])
        run.command(args, cwd=forge.cwd, env=env)
        if missing.exists():
            raise Failure('Missing-checkout action recreated the worktree')
    finally:
        hidden.rename(missing)


def owner_reply_recovery(run, forge):
    from .injection import fault, accepted
    session = run.data['owner_session']
    view = run.api(dict(command='owner-read', session=session), forge.cwd)
    def call(command, **fields):
        nonlocal view
        result = run.api(dict(command=command, session=session, revision=view['revision'], **fields), forge.cwd)
        view = result.get('owner', result)
        return result
    call('owner-refresh')
    item = next(i['key'] for i in view['feedback'] if i['kind'] == 'thread')
    body = 'Smoke lost Owner response ' + run.directory.name
    call('owner-mutate', operation='reply', item=item, body=body)
    identity = view['replies'][-1]['id']
    call('owner-mutate', operation='stage', reply=identity)
    preview = call('owner-prepare')
    with fault(run, 'owner-reply', endpoint_suffix='/replies', body_contains=body) as evidence:
        try:
            call('owner-publish', token=preview['token'])
        except Failure:
            pass
    accepted(evidence)
    view = run.api(dict(command='owner-read', session=session), forge.cwd)
    call('owner-recover')
    recovered = next(r for r in view['replies'] if r['id'] == identity)
    if recovered['status'] != 'published' or not recovered.get('receipt'):
        raise Failure('Owner recovery did not restore the reply receipt')
    rows = forge.pages('/pulls/%s/comments' % run.data['fixtures']['github']['number'])
    if len([r for r in rows if body in r['body']]) != 1:
        raise Failure('Owner reply was duplicated')


def gitlab_advance(run, forge):
    import time
    fixture = run.data['fixtures']['gitlab']
    endpoint = '/merge_requests/' + str(fixture['number'])
    for _ in range(10):
        mr = forge.api(endpoint)
        refs = mr.get('diff_refs')
        if refs and all(refs.get(k) for k in ('base_sha', 'head_sha', 'start_sha')):
            break
        time.sleep(1)
    else:
        raise Failure('GitLab diff refs did not become available for a positioned discussion')
    position = dict(refs, position_type='text', old_path='price.py', new_path='price.py', new_line=3)
    thread = forge.api(endpoint + '/discussions', 'POST', dict(body='Smoke positioned discussion ' + run.directory.name, position=position))
    (forge.cwd / 'price.py').write_text('def total(price, quantity):\n    """Total cost of quantity items."""\n    return price + quantity + 0\n')
    run.command(['git', 'commit', '-qam', 'Advance positioned GitLab fixture'], cwd=forge.cwd)
    run.command(['git', 'push', 'origin', fixture['branch']], cwd=forge.cwd)
    _, sha = run.command(['git', 'rev-parse', 'HEAD'], cwd=forge.cwd)
    for obj in run.data['objects']:
        if obj['kind'] == 'gitlab' and obj['type'] == 'branch' and obj['name'] == fixture['branch']:
            obj['sha'] = sha.strip()
    run.save()
    run.command([str(run.root / 'bin/pr-review'), fixture['url'], '--forge', 'gitlab', '--no-publish', '--economical'], cwd=forge.cwd, timeout=600)
    view = run.api(dict(command='read', forge='gitlab', session=fixture['session']), forge.cwd)
    if view['snapshot']['head'] != sha.strip():
        raise Failure('GitLab second push did not select the new source SHA')
    discussion = next((i for i in view['discussion']['items'] if i['key'] == 'thread:' + thread['id']), None)
    if not discussion or not discussion['is_outdated']:
        raise Failure('Old GitLab position was not retained as an outdated thread')


def accounting(run, forges):
    summaries = []
    for forge in forges:
        fixture = run.data['fixtures'][forge.kind]
        view = run.api(dict(command='read', forge=forge.kind, session=fixture['session']), forge.cwd)
        summaries.append((forge.kind, view['usage']))
    summaries.append(('issue', run.data['issue_fixture']['completed']['usage']))
    for label, summary in summaries:
        if summary['storage_incomplete'] or not summary['calls']:
            raise Failure('Missing accounting records for ' + label)
        for role in summary['roles']:
            if role['token_coverage'] == 'unavailable' and role['usage'] is not None:
                raise Failure('Unavailable usage was represented numerically')
            if role['usage'] is not None and role['usage']['total_tokens'] < 0:
                raise Failure('Negative reported token count')
    run.data['usage'] = dict(summaries)
    run.save()


def automatic_cli(run, forge):
    directory = run.directory / 'automatic'
    directory.mkdir(exist_ok=True)
    checkout = directory / 'repo'
    if not checkout.exists():
        run.command(['git', 'clone', '--quiet', forge.remote, str(checkout)])
    env = dict(run.env, XDG_STATE_HOME=str(directory / 'state'))
    args = [str(run.root / 'bin/pr-review'), run.data['fixtures']['github']['url'], '--economical', '--auto-publish', '--json']
    number = run.data['fixtures']['github']['number']
    _, output = run.command(args, cwd=checkout, env=env, timeout=600)
    result = json.loads(output)
    if result.get('outcome') not in ('published', 'noop') or result.get('exit_code') != 0:
        raise Failure('Automatic CLI did not publish the controlled fixture')
    count = len(forge.pages('/pulls/%s/comments' % number))
    _, output = run.command(args, cwd=checkout, env=env, timeout=600)
    repeated = json.loads(output)
    if repeated.get('outcome') != 'noop' or len(forge.pages('/pulls/%s/comments' % number)) != count:
        raise Failure('Repeated automatic CLI duplicated publication')
    run.data['automatic'] = result
    run.save()
