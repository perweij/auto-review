"""Disposable Git fixture and scripted providers for the shared review engine.

The engine runs real subprocesses, private workspaces and real Git. Only the provider
CLIs are replaced: ``CandidateReview.command`` returns small shell scripts that act
as reviewers and synthesis. Scenarios are selected through environment variables so a
test can change behavior between resumes.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import unittest
from unittest.mock import patch

import test_owner
from pr_review import acceptance, owner_git, providers, review_engine, review_workspace
from pr_review.common import ApiError, now, read_json
from pr_review.owner_state import OwnerSession
from pr_review.processes import Cancelled, run as real_process

REAL_REVIEW_OWNER = acceptance.review_owner
REAL_REVIEW_ISSUE = acceptance.review_issue
KEYS = ['thread:T1', 'thread:T2']

# A reviewer. The body records the checkout and explicitly supplied context.
SLOT_SCRIPT = r'''
stage=$1; out=${2:-}
case " ${FAIL_STAGES:-} " in *" $stage "*) exit 7 ;; esac
case " ${EDIT_STAGES:-} " in *" $stage "*) echo hacked >> source.txt ;; esac
ctx=$(cat ../context/selected_feedback.json 2>/dev/null | tr -d '"\\\n' | head -c 40)
body="stage=$stage head=$(git rev-parse HEAD 2>/dev/null | head -c 12) source=$(tr '\n' , < source.txt) new=$(tr '\n' , < new.txt 2>/dev/null) context=$ctx root=$(ls .. 2>/dev/null | tr '\n' ,) files=$(ls ../context 2>/dev/null | tr '\n' ,)"
if [ -n "$out" ]; then printf '%s\n' "$body" > "$out"; printf '%s\n' '{"type":"review.completed"}'
else printf '{"type":"result","subtype":"success","is_error":false,"result":"%s"}\n' "$body"; fi
'''

# Synthesis. It runs in a fresh checkout of the candidate on the host.
SYNTH_SCRIPT = r'''
out=$1; style=$2
cat > "${STDIN_COPY:-/dev/null}"
printf 'synthesis cwd=%s tmp=%s content=%s\n' "$(pwd)" "${TMPDIR:-}" "$(cat source.txt new.txt | tr '\n' ,)" >> "$MARKS"
env | cut -d= -f1 | sort > "${ENV_NAMES:-/dev/null}"
case "${MISBEHAVE:-}" in
  fail) exit 42 ;;
  edit) echo changed >> source.txt ;;
  assume) echo changed >> source.txt; git update-index --assume-unchanged source.txt ;;
  filter) echo changed >> source.txt; git config filter.hide.clean cat; printf 'source.txt filter=hide\n' > .gitattributes ;;
  untracked) echo generated > generated.out ;;
  live) echo live >> "$OWNER_WORKTREE/source.txt" ;;
  admin) git -C "$OWNER_WORKTREE" add new.txt ;;
  head) git commit -q --allow-empty -m moved ;;
esac
if [ "$style" = claude ]; then
  python3 -c 'import json,sys; print(json.dumps({"type":"result","subtype":"success","is_error":False,"structured_output":json.load(open(sys.argv[1]))}))' "$RESULT_FILE"
else
  if [ -s "$RESULT_FILE" ]; then cp "$RESULT_FILE" "$out"; else : > "$out"; fi
  printf '%s\n' '{"type":"turn.completed"}'
fi
'''


def finding(identifier='F1', *, blocking=False, source='both', validity='CONFIRMED', severity='Medium'):
    return {'id': identifier, 'title': f'Finding {identifier}', 'severity': severity, 'validity': validity,
            'source': source, 'evidence': f'file.py:1 evidence {identifier}', 'blocking': blocking,
            'body_md': f'Details of {identifier}'}


class ScriptedProviders:
    """Mixin: scripted providers, provider-private homes and observation helpers.

    The host class supplies ``self.base`` (an ``OwnerTest`` fixture), ``self.keys``
    and ``self.workspace_name`` and calls ``install_providers()`` from ``setUp``.
    """
    SLOTS = [{'id': 'reviewer-1', 'provider': 'claude', 'model': 'opus', 'effort': 'high'},
             {'id': 'reviewer-2', 'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}]
    SYNTHESIS = {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}

    def install_providers(self):
        self.root = Path(self.base.root)
        for name in ('provider-home/.claude', 'provider-home/.codex', 'provider-home/.config'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.marks, self.result_file = self.root / 'marks.log', self.root / 'synthesis-result.json'
        self.stdin_copy, self.env_names = self.root / 'stdin-copy.txt', self.root / 'env-names.txt'
        self.versions = {'claude': 'claude 1.0', 'codex': 'codex 1.0'}
        env = patch.dict(os.environ, {
            'HOME': str(self.root / 'provider-home'), 'CODEX_HOME': str(self.root / 'provider-home/.codex'),
            'CLAUDE_CONFIG_DIR': str(self.root / 'provider-home/.claude'),
            'XDG_CONFIG_HOME': str(self.root / 'provider-home/.config'),
            'MARKS': str(self.marks), 'RESULT_FILE': str(self.result_file), 'STDIN_COPY': str(self.stdin_copy),
            'ENV_NAMES': str(self.env_names), 'EXAMPLE_API_SECRET_TOKEN': 'must-not-reach-a-provider'})
        env.start()
        self.addCleanup(env.stop)
        patcher = patch('pr_review.review_workspace.tool_version', side_effect=lambda provider: self.versions[provider])
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, real in (('review_owner', REAL_REVIEW_OWNER), ('review_issue', REAL_REVIEW_ISSUE)):
            patcher = patch(f'pr_review.acceptance.{name}', new=real)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(review_workspace.CandidateReview, 'command', lambda run, stage: self.command(run, stage))
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, slots, synthesis, tests='focused'):
        self.batch['settings']['final_review'] = {'reviewer_slots': json.loads(json.dumps(slots)),
                                                  'synthesis': dict(synthesis), 'synthesis_tests': tests}
        self.session.save(self.state)

    def single_reviewer(self):
        """One slot matching synthesis: the direct path, with no bubblewrap."""
        self.configure([{'id': 'reviewer-1', **self.SYNTHESIS}], self.SYNTHESIS)

    def case(self, identifier='c1'):
        return {'id': identifier, 'scenario': 'Scenario', 'expected': 'Expected', 'basis': 'Basis', 'intent': 'change'}

    # ---- scripted providers ----------------------------------------------------------------------
    def command(self, run, stage):
        slot = review_engine.slot_of(run, stage)
        if slot:
            # A Codex reviewer writes its report to the output path; a Claude reviewer prints a result event.
            output = str(run.path / 'reviews' / f'{stage}.md.tmp') if providers.provider_for(run.settings, stage) == 'codex' else ''
            argv = ['/bin/sh', '-c', SLOT_SCRIPT, 'slot', stage, output]
            # Model and effort belong to the command identity, as in a real argv.
            return argv + [f'--model={slot["model"]}', f'--effort={slot["effort"]}']
        choice = run.settings['processing']
        return ['/bin/sh', '-c', SYNTH_SCRIPT, 'synthesis', str(run.stage_dir('synthesis') / 'result.json'),
                choice['provider'], f'--model={choice["model"]}', f'--effort={choice["effort"]}']

    def verdict(self, assessment='passed', findings=(), questions=(), outcomes=None, cases=None, summary='Assessment summary'):
        required = getattr(self, 'data', {}).get('verification_cases', [])
        self.result_file.write_text(json.dumps({
            'assessment': assessment, 'summary': summary, 'checks': ['ran a focused check'],
            'findings': list(findings), 'questions': list(questions),
            'outcomes': [{'item': k, 'status': 'fixed', 'evidence': 'verified'} for k in self.keys] if outcomes is None else outcomes,
            'case_results': [{'id': c['id'], 'status': 'tested', 'evidence': 'ran it'} for c in required] if cases is None else cases}))

    # ---- observations -------------------------------------------------------------------------------
    def candidate_sha(self):
        return owner_git.review_commit(self.session, self.batch['checkpoint']['tree'], self.batch['baseline'])['sha']

    def workspace(self):
        return self.session.path / 'review-engine' / self.candidate_sha() / self.workspace_name

    def commands(self):
        return read_json(self.workspace() / 'logs/commands.json', [])

    def counts(self):
        counted = {}
        for entry in self.commands():
            counted[entry['stage']] = counted.get(entry['stage'], 0) + 1
        return counted

    def manifest(self):
        return read_json(self.workspace() / 'manifest.json')

    def reran(self, action):
        """The stages that ran while ``action`` executed."""
        before = self.counts()
        action()
        after = self.counts()
        return {stage for stage in after if after[stage] != before.get(stage, 0)}

    def marks_text(self):
        return self.marks.read_text() if self.marks.exists() else ''

    def failing_process(self, stage, error):
        """A runner that raises ``error`` for ``stage`` and otherwise runs the real process."""
        def runner(argv, **kwargs):
            if kwargs.get('stage') == stage:
                raise error
            return real_process(argv, **kwargs)
        return patch('pr_review.review_workspace.process', side_effect=runner)


class EngineFixture(ScriptedProviders, unittest.TestCase):
    """Owner session and an uncommitted candidate reviewed through the Owner adapter."""
    keys = KEYS
    workspace_name = 'owner-final'

    def setUp(self):
        self.base = test_owner.OwnerTest()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.install_providers()
        self.session = OwnerSession({'session': self.base.path})
        self.state = self.session.load()
        owner_git.prepare(self.session, self.state)
        self.worktree = Path(self.state['worktree'])
        self.data = {'feedback': [], 'verification_cases': [], 'completed_tasks': [], 'scope': 'Review the combined diff.',
                     'approved_issue_plan': None}
        os.environ['OWNER_WORKTREE'] = str(self.worktree)
        self.candidate()
        self.configure(self.SLOTS, self.SYNTHESIS)
        self.verdict()

    def candidate(self, content='feature\nlocal edit\n', extra='new file\n'):
        """An uncommitted modification and an untracked file: a tree no commit contains."""
        (self.worktree / 'source.txt').write_text(content)
        (self.worktree / 'new.txt').write_text(extra)
        baseline = self.base.git('rev-parse', 'HEAD', cwd=self.worktree)
        state = self.session.load()
        self.batch = {'id': f'batch-{len(state["batches"])}', 'created_at': now(), 'baseline': baseline,
                      'remote_head': state['metadata']['headRefOid'], 'baseline_ref': owner_git.pin_commit(self.session, baseline),
                      'items': list(KEYS), 'fingerprints': {}, 'settings': json.loads(json.dumps(state['settings'])),
                      'checkpoint': owner_git.checkpoint(self.session, state, 'fixture'), 'tasks': [],
                      'git_admin': owner_git.admin(self.session, state), 'verify_only': False, 'plans': [],
                      'attempts': [], 'repairs': 0, 'status': 'reviewing', 'review_engine': 1}
        state['batches'].append(self.batch)
        self.session.save(state)
        self.state = self.session.load()
        self.batch = self.state['batches'][-1]
        self.data['feedback'] = self.session.items(self.state, KEYS)

    def adapter(self):
        return acceptance.OwnerAdapter(self.session, self.state, self.batch, KEYS, self.data)

    def review(self):
        self.state = self.session.load()
        self.batch = next(b for b in self.state['batches'] if b['id'] == self.batch['id'])
        self.data['feedback'] = self.session.items(self.state, KEYS)
        return acceptance.execute(self.session, self.state, self.batch, self.adapter(), role='final_review')
