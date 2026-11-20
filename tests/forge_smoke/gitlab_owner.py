"""Native GitLab Owner lifecycle with scripted providers and independent read-back."""
import json
from pathlib import Path
import time

from .injection import fault, accepted
from .runner import Failure


def fresh_fixture(run, forge, label):
    original = forge.fixture()
    branch = 'auto-review-smoke/' + run.directory.name + '/' + label
    run.command(['git', 'switch', '-c', branch, original['base']], cwd=forge.cwd)
    (forge.cwd / 'price.py').write_text('def total(price, quantity):\n    """Total cost of quantity items."""\n    return price + quantity\n')
    run.command(['git', 'commit', '-qam', 'Controlled GitLab Owner fixture'], cwd=forge.cwd)
    run.command(['git', 'push', 'origin', branch], cwd=forge.cwd)
    _, sha = run.command(['git', 'rev-parse', 'HEAD'], cwd=forge.cwd)
    run.data['objects'].append(dict(kind='gitlab', type='branch', name=branch, sha=sha.strip()))
    run.save()
    result = forge.api('/merge_requests', 'POST', dict(title='Auto-review smoke: ' + label,
                       description='Controlled Owner fixture: ' + run.directory.name,
                       source_branch=branch, target_branch=original['base']))
    fixture = dict(number=result['iid'], url=result['web_url'], branch=branch, base=original['base'])
    run.data['objects'].append(dict(kind='gitlab', type='change', **fixture))
    run.data[label + '_fixture'] = fixture
    run.save()
    endpoint = '/merge_requests/' + str(fixture['number'])
    for _ in range(15):
        raw = forge.api(endpoint)
        refs = raw.get('diff_refs') or {}
        if all(refs.get(k) for k in ('base_sha', 'start_sha', 'head_sha')):
            break
        time.sleep(1)
    else:
        raise Failure('Owner fixture diff refs never became available')
    standalone = forge.api(endpoint + '/notes', 'POST', dict(body='Owner smoke: discuss the arithmetic regression.'))
    thread = forge.api(endpoint + '/discussions', 'POST', dict(body='Fix total to multiply; verify total(3,4)==12.',
                       position=dict(refs, position_type='text', old_path='price.py', new_path='price.py', new_line=3)))
    fixture.update(comment=standalone['id'], thread=thread['id'], root=thread['notes'][0]['id'])
    run.save()
    return fixture


def _scenario(run, forge):
    if run.data['suite'] == 'live':
        raise Failure('GitLab Owner smoke uses scripted providers only')
    fixture = fresh_fixture(run, forge, 'gitlab-owner')
    endpoint = '/merge_requests/' + str(fixture['number'])
    args = [str(run.root / 'bin/pr-review'), 'owner', fixture['url'] + '/diffs?view=parallel',
            'open', '--forge', 'gitlab']
    _, output = run.command(args, cwd=forge.cwd)
    view = json.loads(output)
    session = view['session']
    def call(command, **fields):
        nonlocal view
        result = run.api(dict(command=command, session=session, revision=view['revision'], **fields), forge.cwd, timeout=1200)
        view = result.get('owner', result)
        return result
    call('owner-refresh')
    if not view['fixing']['available']:
        raise Failure('Same-project native MR cannot be fixed')
    comment = next(i for i in view['feedback'] if i['root_id'] == fixture['comment'])
    key = comment['key']
    before_fingerprint = comment['fingerprint']
    call('owner-mutate', operation='reply', item=key, body='Native Owner acknowledgment.\n/close')
    call('owner-mutate', operation='stage', reply=view['replies'][-1]['id'])
    prepared = call('owner-prepare')
    if prepared['preview']['operations'][0]['body'] != 'Native Owner acknowledgment.\n/\u200bclose':
        raise Failure('Owner preview differs from protected outgoing prose')
    with fault(run, 'gitlab-owner-reply', tool='glab', endpoint_suffix='/notes',
               body_contains='Native Owner acknowledgment.') as evidence:
        try:
            call('owner-publish', token=prepared['token'])
        except Failure:
            pass
        else:
            raise Failure('Owner accepted-write response loss was not surfaced')
    accepted(evidence)
    view = run.api(dict(command='owner-read', session=session), forge.cwd)
    call('owner-recover')
    if view['replies'][-1]['status'] != 'published':
        raise Failure('Owner did not recover its accepted native reply')
    converted = next(i for i in view['feedback'] if i['key'] == key)
    if converted['kind'] != 'thread' or converted['fingerprint'] != before_fingerprint:
        raise Failure('Comment conversion invalidated stable Owner feedback')
    target = prepared['replies'][0]['target']
    if target != converted['reply_target']:
        raise Failure('Comment conversion changed the frozen reply destination')
    thread_key = next(i['key'] for i in view['feedback'] if i['root_id'] == fixture['root'])
    choice = dict(provider='codex', model='gpt-6.1-terra', effort='high')
    call('owner-mutate', operation='settings', settings=dict(fix=choice, review=choice, verification='focused'))
    call('owner-mutate', operation='restore', items=[thread_key])
    call('owner-fix', items=[thread_key], auto_commit=True)
    batch = view['batches'][-1]
    if batch['status'] != 'ready' or not batch.get('commit_progress', {}).get('complete'):
        raise Failure('GitLab fixing did not finish verification and automatic commits')
    run.command(['python3', '-m', 'unittest'], cwd=view['worktree'])
    run.command(['git', 'push', 'origin', 'HEAD:refs/heads/' + fixture['branch']], cwd=view['worktree'])
    _, sha = run.command(['git', 'rev-parse', 'HEAD'], cwd=view['worktree'])
    for obj in run.data['objects']:
        if obj.get('name') == fixture['branch']:
            obj['sha'] = sha.strip()
    run.save()
    wait_head(run, forge, fixture, sha.strip())
    call('owner-refresh')
    call('owner-associate')
    fixed = [r for r in view['replies'] if r.get('commits')]
    if not fixed or '/-/commit/' + sha.strip() not in fixed[-1]['body']:
        raise Failure('GitLab commit association has no native fixed-in link')
    prepared_fix = call('owner-prepare')
    call('owner-publish', token=prepared_fix['token'])
    fingerprint = next(i['fingerprint'] for i in view['feedback'] if i['key'] == thread_key)
    for resolved in (True, False, True):
        call('owner-thread-state', item=thread_key, resolved=resolved)
        item = next(i for i in view['feedback'] if i['key'] == thread_key)
        if item['is_resolved'] != resolved or item['fingerprint'] != fingerprint:
            raise Failure('Thread state was not confirmed or invalidated semantic evidence')
    if not item.get('remote_completion'):
        raise Failure('Resolved verified GitLab feedback has no completion proof')
    report = call('owner-publish-report', batch=batch['id'])
    view = run.api(dict(command='owner-read', session=session), forge.cwd)
    repeated = call('owner-publish-report', batch=batch['id'])
    if report['report_url'] != repeated['report_url']:
        raise Failure('Owner execution report was duplicated')
    notes = [n for d in forge.pages(endpoint + '/discussions') for n in d['notes']]
    saved = json.loads((Path(session) / 'session.json').read_text())
    for action in saved['actions']:
        matches = [n for n in notes if action['marker'] in n['body']]
        if len(matches) != 1 or matches[0]['body'] != action['payload']['body']:
            raise Failure('Native Owner receipt differs from frozen payload or is duplicated')
    _, reopened = run.command([str(run.root / 'bin/pr-review'), 'owner', 'read', '--session', session, '--forge', 'gitlab'], cwd=forge.cwd)
    if json.loads(reopened)['session'] != session:
        raise Failure('CLI resume lost the GitLab Owner identity')
    view = json.loads(reopened)
    evidence_path = run.directory / 'gitlab-owner-evidence.json'
    evidence_path.write_text(json.dumps(dict(fixture=fixture, preview=prepared['preview'],
                              fixed_preview=prepared_fix['preview'], final=view, report=report,
                              native_note_ids=[n['id'] for n in notes]), indent=2) + '\n')
    run.data['gitlab-owner'] = dict(session=session, evidence=str(evidence_path), automated=True,
                                  interactive_gui_inspection=False)
    run.save()


def wait_head(run, forge, fixture, sha):
    """GitLab may update REST metadata before the retained MR fetch ref."""
    for _ in range(30):
        raw = forge.api('/merge_requests/' + str(fixture['number']))
        _, remote = run.command(['git', 'ls-remote', 'origin', 'refs/merge-requests/' + str(fixture['number']) + '/head'], cwd=forge.cwd)
        if (raw.get('sha') == sha and (raw.get('diff_refs') or {}).get('head_sha') == sha
                and remote.split()[:1] == [sha]):
            return
        time.sleep(1)
    raise Failure('GitLab metadata and MR fetch ref did not converge after explicit push')


def _emacs_scenario(run, forge):
    from .gitlab_emacs import emacs_arguments
    if run.data['suite'] == 'live':
        raise Failure('GitLab Owner Emacs smoke uses scripted providers only')
    fixture = fresh_fixture(run, forge, 'gitlab-owner-emacs')
    evidence_path = run.directory / 'gitlab-owner-emacs-evidence.json'
    env = dict(run.env, SMOKE_BACKEND=str(run.root / 'bin/pr-review'), SMOKE_CWD=str(forge.cwd),
               SMOKE_MR_URL=fixture['url'], SMOKE_COMMENT_ID=str(fixture['comment']),
               SMOKE_ROOT_ID=str(fixture['root']), SMOKE_SOURCE_BRANCH=fixture['branch'],
               SMOKE_EMACS_EVIDENCE=str(evidence_path))
    args = emacs_arguments(run)
    args[-1] = str(run.root / 'tests/forge_smoke/gitlab-owner-emacs.el')
    run.command(args, cwd=forge.cwd, env=env, timeout=1800)
    evidence = json.loads(evidence_path.read_text())
    view = evidence['view']
    native = forge.pages('/merge_requests/' + str(fixture['number']) + '/discussions')
    notes = [n for d in native for n in d['notes']]
    saved = json.loads((Path(view['session']) / 'session.json').read_text())
    for action in saved['actions']:
        matches = [n for n in notes if action['marker'] in n['body']]
        if len(matches) != 1 or matches[0]['body'] != action['payload']['body']:
            raise Failure('Emacs Owner publication differs from frozen native payload or is duplicated')
    thread = next(d for d in native if d['id'] == fixture['thread'])
    if not thread['notes'][0]['resolved']:
        raise Failure('Emacs Owner native thread resolution was not retained')
    _, sha = run.command(['git', 'rev-parse', 'HEAD'], cwd=view['worktree'])
    for obj in run.data['objects']:
        if obj.get('name') == fixture['branch']:
            obj['sha'] = sha.strip()
    run.data['gitlab-owner-emacs'] = dict(evidence=str(evidence_path), automated=True,
                                        interactive_gui_inspection=False)
    run.save()


def scenario(run, forge):
    try:
        return _scenario(run, forge)
    finally:
        original = run.data.get('fixtures', {}).get('gitlab', {}).get('branch')
        if original:
            run.command(['git', 'switch', original], cwd=forge.cwd)


def emacs_scenario(run, forge):
    try:
        return _emacs_scenario(run, forge)
    finally:
        original = run.data.get('fixtures', {}).get('gitlab', {}).get('branch')
        if original:
            run.command(['git', 'switch', original], cwd=forge.cwd)
