#!/usr/bin/env python3
"""Opt-in live forge functional tests. Artifacts and remote fixtures are retained."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time

from forge_smoke.runner import Run, Failure
from forge_smoke.forge import Forge
from forge_smoke.scenarios import publication, issue, owner, integrity, publication_recovery, edited_preview, stale_head, foreign_issue_commit, issue_plan_recovery, interrupt_and_lock, approval_changes, changed_plan_receipt, emacs_checkouts, owner_reply_recovery, gitlab_advance, accounting, automatic_cli

ROOT = Path(__file__).resolve().parents[1]


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--suite', choices=('smoke', 'extended', 'live', 'all'), default='smoke')
    result.add_argument('--github-repo', type=Path, default=ROOT.parent / 'temp-eval-auto-review-github')
    result.add_argument('--gitlab-repo', type=Path, default=ROOT.parent / 'temp-eval-auto-review-gitlab')
    group = result.add_mutually_exclusive_group()
    group.add_argument('--resume', type=Path)
    group.add_argument('--cleanup', type=Path)
    result.add_argument('--scenario', choices=('preflight', 'review', 'issue', 'approval-recovery'))
    return result


def scripted(run):
    fixture = run.directory / 'models'
    fixture.mkdir(exist_ok=True)
    (fixture / 'bin').mkdir(exist_ok=True)
    (fixture / 'delegates').mkdir(exist_ok=True)
    for tool in ('codex', 'claude'):
        path = fixture / 'bin' / tool
        if not path.exists():
            path.symlink_to(ROOT / 'tests/forge_smoke/provider.py')
        delegate = fixture / 'delegates' / tool
        if not delegate.exists():
            delegate.symlink_to(ROOT / 'tests/helpers/mock-cli')
    for name in ('mock-bridge-request', 'mock-bridge-response'):
        if not (fixture / name).exists():
            os.mkfifo(fixture / name, 0o600)
    finding = dict(id='F1', title='Total adds instead of multiplying', severity='Medium', validity='CONFIRMED',
                   evidence='price.py:3 returns price + quantity; total(3,4) is 7 instead of 12.', blocking=False,
                   body_md='Multiply price by quantity: total(3, 4) must be 12, but returns 7.',
                   primary_location=dict(path='price.py', side='RIGHT', line=3, start_line=3), locations=[])
    (fixture / 'findings.json').write_text(json.dumps([finding]))
    run.env.update(FIXTURE=str(fixture), PATH=str(fixture / 'bin') + os.pathsep + run.env['PATH'])
    return subprocess.Popen([sys.executable, str(ROOT / 'tests/helpers/mock-review-bridge.py'), str(fixture)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def review(run, forge):
    fixture = forge.fixture()
    forge.discussion(fixture)
    remote_before = forge.api(('/pulls/' if forge.kind == 'github' else '/merge_requests/') + str(fixture['number']))
    args = [str(ROOT / 'bin/pr-review'), fixture['url'], '--forge', forge.kind, '--no-publish', '--economical']
    _, output = run.command(args, cwd=forge.cwd, timeout=1200)
    paths = [line.split('Checkout: ', 1)[1].strip() for line in output.splitlines() if 'Checkout: ' in line]
    if not paths or not Path(paths[-1]).is_absolute():
        raise Failure('Missing absolute Checkout log line')
    _, actual = run.command(['git', 'rev-parse', 'HEAD'], cwd=paths[-1])
    _, remote = run.command(['git', 'ls-remote', 'origin', 'refs/heads/' + fixture['branch']], cwd=forge.cwd)
    if actual.strip() != remote.split()[0]:
        raise Failure('Review checkout SHA differs from fixture head')
    run.data['fixtures'][forge.kind]['checkout'] = paths[-1]
    artifacts = [line.split('Artifacts: ', 1)[1].strip() for line in output.splitlines() if 'Artifacts: ' in line]
    if artifacts:
        run.data['fixtures'][forge.kind]['session'] = artifacts[-1]
    run.save()
    def usage_counts():
        view = run.api(dict(command='read', forge=forge.kind, session=fixture['session']), forge.cwd)
        fixture['usage'] = view.get('usage', {})
        return {(r['role'], r['provider'], r['model'], r['effort']): r['calls'] for r in view.get('usage', {}).get('roles', []) if not r['role'].startswith('auth-probe/')}
    counts_before = usage_counts()
    stages = run.directory / 'models/stages'
    before = stages.read_text() if stages.exists() else None
    run.command(args, cwd=forge.cwd, timeout=1200)
    if usage_counts() != counts_before:
        raise Failure('Resume charged new non-probe model calls')
    if before is not None and stages.read_text() != before:
        raise Failure('Resume launched new review stages')
    if forge.kind == 'gitlab':
        calls = [json.loads(line) for line in (run.directory / 'forge-calls.jsonl').read_text().splitlines()]
        if any(c['tool'] == 'glab' and c['actor'] == 'application' and c['kind'] not in ('read', 'metadata') for c in calls):
            raise Failure('GitLab review performed a mutation or unclassified forge operation')
        for command in ('owner-open', 'issue-run', 'prepare'):
            try:
                run.api(dict(command=command, forge='gitlab', cwd=str(forge.cwd), pr=fixture['number']), forge.cwd)
            except Failure as exc:
                if 'unsupported_operation' not in str(exc):
                    raise
            else:
                raise Failure('Unsupported GitLab operation was accepted: ' + command)
        remote_after = forge.api('/merge_requests/' + str(fixture['number']))
        if any(remote_before.get(k) != remote_after.get(k) for k in ('sha', 'state', 'title', 'description')):
            raise Failure('GitLab MR changed during read-only review')


def main():
    os.umask(0o077)
    options = parser().parse_args()
    required = ['git', 'gh', 'glab', 'python3']
    if not options.cleanup:
        required += ['claude', 'codex'] if options.suite == 'live' else ['bash', 'jq']
        if options.suite == 'all':
            required += ['claude', 'codex']
        if options.suite == 'extended':
            required.append('emacs')
    missing = [tool for tool in required if shutil.which(tool) is None]
    if missing:
        raise Failure('Install required tools before running smoke tests: ' + ', '.join(missing))
    if options.cleanup:
        run = Run(ROOT, options.cleanup, resume=True, verify_snapshot=False)
        try:
            for kind in ('github', 'gitlab'):
                source = run.data['targets'][kind]['source']
                forge = Forge(run, kind, source)
                if forge.repo != run.data['targets'][kind]['repo']:
                    raise Failure('Cleanup target remote identity changed')
                forge.preflight()
                forge.cleanup()
        finally:
            run.report()
        return 0
    if options.suite == 'all':
        if options.resume:
            raise Failure('Resume an individual retained run, not the combined command.')
        print('Smoke: scripted checks, then economical real-model workflows (uses quota).', flush=True)
        for suite in ('smoke', 'live'):
            args = [sys.executable, str(Path(__file__).resolve()), '--suite', suite, '--github-repo', str(options.github_repo), '--gitlab-repo', str(options.gitlab_repo)]
            if options.scenario:
                args += ['--scenario', options.scenario]
            code = subprocess.run(args).returncode
            if code:
                return code
        return 0
    run = Run(ROOT, options.resume, resume=bool(options.resume))
    if options.resume and run.data.get('suite') != options.suite:
        raise Failure('Resume with --suite ' + run.data['suite'] + '; model modes cannot change within a run.')
    run.deadline = time.monotonic() + (60 if options.suite == 'extended' else 30) * 60
    run.trace_forges()
    print('Run artifacts:', run.directory, flush=True)
    run.data['suite'] = options.suite
    run.save()
    if options.suite == 'live':
        run.deadline = time.monotonic() + 90 * 60
        sys.path.insert(0, str(ROOT / 'lib'))
        from pr_review import cli_settings
        from pr_review.issue_cli import parser as issue_parser
        request = vars(issue_parser().parse_args(['-i', '1', '--economical', '--planning', 'codex-1', '--fix', 'codex-1', '--planning-review', 'claude-1']))
        cli_settings.prepare(request, issue=True)
        resolved = cli_settings.resolve(request, issue=True)
        choices = [*resolved['reviewer_slots'], resolved['processing'], resolved['planning'], resolved['fix'], resolved['planning_review']]
        allowed = {('claude', 'haiku', 'high'), ('codex', 'gpt-6.1-terra', 'high')}
        if any((c['provider'], c['model'], c['effort']) not in allowed for c in choices):
            raise Failure('Paid smoke model settings are outside the economical allowlist')
        run.data['resolved_settings'] = resolved
        run.save()
    bridge = None
    passed = True
    try:
        forges = [Forge(run, kind, run.data['targets'][kind]['source'] if options.resume else getattr(options, kind + '_repo')) for kind in ('github', 'gitlab')]
        for forge in forges:
            passed = run.scenario(forge.kind + '-preflight', forge.preflight) and passed
        if not passed or options.scenario == 'preflight':
            return 0 if passed else 1
        if options.suite != 'live':
            bridge = scripted(run)
        if options.scenario == 'approval-recovery':
            if options.suite == 'live':
                raise Failure('Approval recovery uses scripted models only')
            for mode in ('after', 'before'):
                passed = run.scenario('issue-plan-recovery-' + mode, lambda mode=mode: issue_plan_recovery(run, forges[0], mode)) and passed
            passed = run.scenario('approval-changes', lambda: approval_changes(run, forges[0])) and passed
            for mode in ('before', 'after'):
                passed = run.scenario('plan-receipt-' + mode, lambda mode=mode: changed_plan_receipt(run, forges[0], mode)) and passed
            passed = run.scenario('integrity', lambda: integrity(run, forges)) and passed
            return 0 if passed else 1
        if options.scenario == 'issue':
            passed = run.scenario('github-issue', lambda: issue(run, forges[0]))
            passed = run.scenario('integrity', lambda: integrity(run, forges)) and passed
            return 0 if passed else 1
        for forge in forges:
            passed = run.scenario(forge.kind + '-review', lambda forge=forge: review(run, forge)) and passed
        if options.scenario == 'review':
            passed = run.scenario('integrity', lambda: integrity(run, forges)) and passed
            return 0 if passed else 1
        if options.suite == 'live':
            passed = run.scenario('github-issue', lambda: issue(run, forges[0])) and passed
            passed = run.scenario('integrity', lambda: integrity(run, forges)) and passed
            return 0 if passed else 1
        github = forges[0]
        if any(r['name'] == 'github-review' and r['status'] == 'passed' for r in run.data['results']):
            passed = run.scenario('github-publication', lambda: publication(run, github)) and passed
        if any(r['name'] == 'github-publication' and r['status'] == 'passed' for r in run.data['results']):
            passed = run.scenario('publication-recovery', lambda: publication_recovery(run, github)) and passed
            passed = run.scenario('github-owner', lambda: owner(run, github)) and passed
        if options.suite == 'extended':
            passed = run.scenario('gitlab-advance', lambda: gitlab_advance(run, forges[1])) and passed
            passed = run.scenario('owner-reply-recovery', lambda: owner_reply_recovery(run, github)) and passed
            passed = run.scenario('edited-preview', lambda: edited_preview(run, github)) and passed
        passed = run.scenario('stale-head', lambda: stale_head(run, github)) and passed
        if options.suite == 'extended':
            passed = run.scenario('interruption-and-concurrency', lambda: interrupt_and_lock(run, github)) and passed
        for mode in ('after', 'before'):
            passed = run.scenario('issue-plan-recovery-' + mode, lambda mode=mode: issue_plan_recovery(run, github, mode)) and passed
        if options.suite == 'extended':
            passed = run.scenario('approval-changes', lambda: approval_changes(run, github)) and passed
            for mode in ('before', 'after'):
                passed = run.scenario('plan-receipt-' + mode, lambda mode=mode: changed_plan_receipt(run, github, mode)) and passed
        passed = run.scenario('github-issue', lambda: issue(run, github)) and passed
        if options.suite == 'extended' and any(r['name'] == 'github-issue' and r['status'] == 'passed' for r in run.data['results']):
            passed = run.scenario('accounting', lambda: accounting(run, forges)) and passed
            passed = run.scenario('emacs-checkouts', lambda: emacs_checkouts(run, github)) and passed
        if any(r['name'] == 'github-issue' and r['status'] == 'passed' for r in run.data['results']):
            passed = run.scenario('foreign-issue-commit', lambda: foreign_issue_commit(run, github)) and passed
        passed = run.scenario('automatic-cli-json', lambda: automatic_cli(run, github)) and passed
        passed = run.scenario('integrity', lambda: integrity(run, forges)) and passed
        return 0 if passed else 1
    finally:
        if bridge:
            bridge.terminate()
            bridge.wait(timeout=5)
        run.report()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Failure, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
