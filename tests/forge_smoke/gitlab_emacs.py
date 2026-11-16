"""Fresh controlled MR, real Emacs commands, scripted inference and native receipts."""
import json
import os
from pathlib import Path
import shutil
from .runner import Failure


def emacs_arguments(run):
    if shutil.which('emacs') is None:
        raise Failure('gitlab-emacs requires Emacs 29.1 or newer; install emacs explicitly.')
    args = ['emacs', '--batch', '--no-site-file', '-L', str(run.root / 'emacs')]
    setup = os.environ.get('AUTO_REVIEW_TEST_LOAD_FILE')
    if setup:
        args += ['-l', setup]
    else:
        dependencies = Path(os.environ.get('AUTO_REVIEW_EMACS_DEPENDENCY_ROOT', '/opt/emacs-lisp'))
        for name in ('compat', 'cond-let', 'llama', 'seq', 'with-editor', 'transient', 'magit', 'closql',
                     'emacsql', 'ghub', 'treepy', 'markdown-mode', 'yaml', 'forge'):
            directory = dependencies / name
            if (directory / 'lisp').is_dir():
                directory /= 'lisp'
            if directory.is_dir():
                args += ['-L', str(directory)]
    return args + ['-l', str(run.root / 'emacs/tests/bootstrap.el'),
                   '-l', str(run.root / 'tests/forge_smoke/gitlab-emacs.el')]


def verify(evidence, notes):
    def contains_position(observed, expected):
        # Native responses also carry server-derived fields (including a type
        # for unchanged range endpoints). Every frozen field must match.
        if isinstance(expected, dict):
            return isinstance(observed, dict) and all(contains_position(observed.get(k), v) for k, v in expected.items())
        return observed == expected
    confirmations = evidence['confirmations']
    if len(confirmations) != 2 or confirmations[0]['accepted'] or not confirmations[1]['accepted']:
        raise Failure('Emacs must decline one preview, then explicitly confirm another')
    prepared = confirmations[1]['prepared']
    rows = prepared['preview']['operations']
    ops = [op for action in prepared['review']['prepared']['actions'] for op in action['operations']
           if op['status'] != 'published' and not op.get('receipt_verified')]
    if len(rows) != 5 or len(rows) != len(ops):
        raise Failure('Expected five frozen operations in the displayed preview')
    if prepared['counts'] != dict(inline=4, overall=1, replies=0):
        raise Failure('Confirmation counts do not match remaining writes')
    if '4 inline' not in confirmations[1]['prompt'] or '1 overall' not in confirmations[1]['prompt']:
        raise Failure('The displayed confirmation counts differ from remaining writes')
    for row, op in zip(rows, ops):
        if row['operation_id'] != op['id'] or row['body'] + '\n\n' + op['marker'] != op['payload']['body']:
            raise Failure('Preview differs from the frozen payload beyond the declared marker omission')
        if row['body'] not in confirmations[1]['text']:
            raise Failure('Confirmation did not display the exact projected prose')
        if row['destination'] != op['destination'] or row['destination']['url'] not in confirmations[1]['text']:
            raise Failure('Displayed publication destination differs from the frozen operation')
        if row['position'] != op['payload'].get('position'):
            raise Failure('Preview lost the frozen native position')
        if row['position'] and ('Position: ' + json.dumps(row['position'], ensure_ascii=False, separators=(',', ':'))) not in confirmations[1]['text']:
            raise Failure('Confirmation did not display the exact frozen position')
        matches = [n for n in notes if op['marker'] in n['body']]
        if len(matches) != 1 or matches[0]['body'] != op['payload']['body']:
            raise Failure('Native note differs from frozen payload, is missing, or was duplicated')
        if row.get('position'):
            if not contains_position(matches[0].get('position'), row['position']):
                raise Failure('Native note position differs from frozen position')
    inline = [row['position'] for row in rows if row['kind'] == 'inline']
    if not any(p.get('old_line') == 3 and p.get('new_line') is None for p in inline):
        raise Failure('Missing removed-line position')
    if not any(p.get('old_line') == p.get('new_line') == 2 for p in inline):
        raise Failure('Missing paired context-line position')
    if not any(p.get('line_range') for p in inline):
        raise Failure('Missing native multiline position')
    if not any('/\u200bclose' in row['body'] for row in rows):
        raise Failure('Slash directive was not neutralized in displayed prose')
    originals = evidence['published_review']['comments']
    if not any('**Edited finding**\n/close' in c['body'] for c in originals):
        raise Failure('Dashboard/editor source wording changed')
    def calls(usage):
        return {(r['role'], r['provider'], r['model'], r['effort']): r['calls']
                for r in usage.get('roles', []) if not r['role'].startswith('auth-probe/')}
    baseline = calls(evidence['usage_after_run'])
    if (not baseline or baseline != calls(evidence['published_review']['usage'])
            or baseline != calls(evidence['usage_after_resume'])
            or baseline != calls(evidence['final_review']['usage'])):
        raise Failure('Opening, editing, publishing or resuming performed implicit model work')
    if not Path(evidence['checkout']).is_dir() or not Path(evidence['report_path']).is_file():
        raise Failure('Checkout or local execution report is unavailable')


def scenario(run, forge):
    args = emacs_arguments(run)
    if run.data['suite'] == 'live':
        raise Failure('gitlab-emacs uses scripted providers only; no paid smoke run is needed.')
    original = forge.fixture()
    fixture = run.data.get('gitlab_emacs_fixture')
    if not fixture:
        branch = 'auto-review-smoke/' + run.directory.name + '/emacs'
        run.command(['git', 'switch', '-c', branch, original['base']], cwd=forge.cwd)
        (forge.cwd / 'price.py').write_text('def total(price, quantity):\n    """Total cost of quantity items."""\n    return price + quantity\n')
        run.command(['git', 'commit', '-qam', 'Controlled Emacs review fixture'], cwd=forge.cwd)
        run.command(['git', 'push', 'origin', branch], cwd=forge.cwd)
        _, sha = run.command(['git', 'rev-parse', 'HEAD'], cwd=forge.cwd)
        run.data['objects'].append(dict(kind='gitlab', type='branch', name=branch, sha=sha.strip()))
        run.save()
        result = forge.api('/merge_requests', 'POST', dict(title='Auto-review smoke: GitLab Emacs',
                           description='Controlled Emacs fixture: ' + run.directory.name,
                           source_branch=branch, target_branch=original['base']))
        fixture = dict(number=result['iid'], url=result['web_url'], branch=branch, base=original['base'])
        run.data['gitlab_emacs_fixture'] = fixture
        run.data['objects'].append(dict(kind='gitlab', type='change', **fixture))
        run.save()
    endpoint = '/merge_requests/' + str(fixture['number'])
    before = forge.api(endpoint)
    if any(not n.get('system') for n in forge.pages(endpoint + '/notes')):
        raise Failure('gitlab-emacs requires a fresh MR; use a new smoke run for a new recording.')
    path = run.directory / 'gitlab-emacs-evidence.json'
    env = dict(run.env, SMOKE_CWD=str(forge.cwd), SMOKE_BACKEND=str(run.root / 'bin/pr-review'),
               SMOKE_MR_URL=fixture['url'], SMOKE_EMACS_EVIDENCE=str(path))
    try:
        run.command(args, cwd=forge.cwd, env=env, timeout=1800)
        evidence = json.loads(path.read_text())
        discussions = forge.pages(endpoint + '/discussions')
        notes = [n for d in discussions for n in d['notes']]
        verify(evidence, notes)
        after = forge.api(endpoint)
        if any(before.get(k) != after.get(k) for k in ('state', 'sha', 'title', 'description', 'source_branch', 'target_branch')):
            raise Failure('GitLab Emacs publication changed MR state or source')
        fixture.update(session=evidence['final_review']['session'], evidence=str(path),
                       automated=True, interactive_gui_inspection=False)
        run.save()
    finally:
        run.command(['git', 'switch', original['branch']], cwd=forge.cwd)
