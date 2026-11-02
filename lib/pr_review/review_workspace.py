"""Artifact store, candidate snapshot and checkouts for Owner and issue engine reviews.

A non-PR review assesses an uncommitted candidate that the Owner checkpoint
already identifies by a Git tree. ``CandidateReview`` is the small artifact store
the shared engine runs against: stage records, ``save()``, artifact directories
and the bridge commit of that tree. It does not imitate a PR session.

Direct stages (the single reviewer and synthesis) read a fresh disposable
checkout of the bridge commit. The checkout makes the assessed snapshot
unambiguous; it is not a security boundary. Its tracked content is compared with
the candidate tree before and after each stage without trusting the checkout's
own index or configuration.
"""
import os
from pathlib import Path
import shutil
import tempfile

from .common import ApiError, command, now, read_json
from . import isolation, owner_git, providers, review_engine, settings
from .processes import run as process
from .storage import write_json

CHECKOUT_ISOLATION = 'candidate-checkout-v1'


def tool_version(provider):
    """The CLI version identifying the provider in stage fingerprints."""
    lines = command([provider, '--version'], timeout=90).stdout.strip().splitlines()
    return lines[0] if lines else provider


class CandidateReview:
    """The run the engine uses for an Owner or issue candidate."""

    def __init__(self, session, state, batch, adapter, directory, candidate, configuration, runtime, tools):
        self.session, self.state, self.batch = session, state, batch
        self.adapter = adapter
        self.root = self.path = Path(directory)
        self.repo = session.repo
        self.worktree = Path(state['worktree'])
        self.candidate = candidate
        self.settings, self.runtime, self.tools = configuration, runtime, tools
        self.scratch = self.path / 'tmp'
        self.active = None
        self.checkouts = {}
        self.manifest = self._manifest()

    @classmethod
    def open(cls, session, state, batch, adapter, *, tree, runtime):
        candidate = owner_git.review_commit(session, tree, batch['baseline'])
        candidate['baseline_ref'] = batch.get('baseline_ref') or owner_git.pin_commit(session, batch['baseline'])
        directory = session.path / 'review-engine' / candidate['sha'] / adapter.name
        for name in ('', 'context', 'prompts', 'reviews', 'logs', 'tmp', 'stages', 'attempts'):
            (directory / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        configuration = settings.final_review_configuration(
            owner_state_effective(batch['settings'])['final_review'])
        tools = {provider: tool_version(provider) for provider in
                 dict.fromkeys([slot['provider'] for slot in configuration['reviewer_slots']]
                               + [configuration['processing']['provider']])}
        return cls(session, state, batch, adapter, directory, candidate, configuration, runtime, tools)

    def _manifest(self):
        manifest = read_json(self.path / 'manifest.json', None)
        if not isinstance(manifest, dict) or manifest.get('format_version') != 1:
            manifest = {'format_version': 1, 'phases': {}, 'warnings': [], 'generated_files': []}
        # The bridge commit is the head and the baseline the base, so the shared
        # fingerprint scheme keeps reading ``snapshot`` as it does for a PR.
        manifest.update(adapter=self.adapter.name, candidate=self.candidate, configuration=self.settings, tools=self.tools,
                        snapshot={'head': self.candidate['sha'], 'base': self.candidate['baseline'],
                                  'merge_base': self.candidate['baseline']})
        write_json(self.path / 'manifest.json', manifest)
        return manifest

    # ---- the run protocol used by the engine ------------------------------------------
    @property
    def dual(self):
        return len(self.settings['reviewers']) == 2

    def save(self):
        write_json(self.path / 'manifest.json', self.manifest)
        self.sync_attempt()

    def sync_attempt(self):
        """Mirror stage status and artifact paths into the batch's ``review_engine`` attempt.

        An orphaned stage (a crash between saves) therefore stays visible as a running
        stage in the Owner state, where resume marks it interrupted.
        """
        attempt = getattr(self, 'attempt', None)
        if attempt is None:
            return
        stages = {}
        for stage, record in self.manifest['phases'].items():
            entry = {'status': record.get('status')}
            for key in ('stdout_log', 'stderr_log', 'generated_archive', 'artifact', 'failure_reason'):
                if record.get(key):
                    entry[key] = record[key]
            output = self.path / 'reviews' / (f'{stage}.md' if stage != 'synthesis' else 'synthesis.json')
            if record.get('status') == 'completed' and output.is_file():
                entry['output'] = str(output)
            stages[stage] = entry
        if stages != attempt.get('stages'):
            attempt['stages'] = stages
            self.session.save(self.state)

    def begin(self):
        """A non-PR review has no generation to replace."""

    def reset_publication(self):
        """A non-PR review publishes nothing."""

    def complete(self, stage, output):
        review_engine.complete(self, stage, output)

    def model(self, stage, argv, input_bytes=b''):
        return review_engine.execute_stage(self, stage, argv, input_bytes, runner=process)

    def command(self, stage):
        slot = review_engine.slot_of(self, stage)
        if slot:
            return providers.review_command(self.settings, self.runtime, stage, self.path,
                                            self.candidate['baseline_ref'],
                                            self.adapter.reviewer_instructions(self, slot))
        return self.adapter.synthesis_command(self)

    # ---- direct stages: a fresh checkout of the bridge commit ------------------------------
    def stage_dir(self, stage):
        return self.path / 'stages' / stage

    def checkout_path(self, stage):
        return self.stage_dir(stage) / 'checkout'

    def archive_stage(self, stage):
        """Keep an earlier attempt's artifacts; the live name always holds the current attempt."""
        directory = self.stage_dir(stage)
        if directory.is_symlink():
            directory.unlink()
        if not directory.exists():
            return
        shutil.rmtree(directory / 'checkout', ignore_errors=True)  # disposable; generated files were preserved
        number = 1
        while (self.path / 'attempts' / f'{stage}-{number}').exists():
            number += 1
        directory.rename(self.path / 'attempts' / f'{stage}-{number}')

    def create_checkout(self, stage):
        self.archive_stage(stage)
        directory = self.stage_dir(stage)
        directory.mkdir(parents=True, mode=0o700)
        (directory / 'scratch').mkdir(mode=0o700)
        head = self.candidate
        self.checkouts[stage] = isolation.prepare_checkout(
            self.repo.common, self.candidate_ref, head['baseline_ref'], head['sha'], head['baseline'],
            self.checkout_path(stage), self.scratch)
        return directory

    @property
    def candidate_ref(self):
        return self.candidate['ref']

    def changed_paths(self, stage):
        """Differences between the checkout's tracked content and the candidate tree.

        Neither the checkout's index nor its configuration is read: a stage can
        write to both. A temporary index is filled from the candidate tree and
        refreshed from the files, using the Owner repository's Git directory.
        Untracked and generated files are outside the comparison.
        """
        checkout = self.checkout_path(stage)
        problems = []
        git_dir = checkout / '.git'
        try:
            head = (git_dir / 'HEAD').read_text().strip() if git_dir.is_dir() and not git_dir.is_symlink() else None
        except OSError:
            head = None
        if head != self.candidate['sha']:
            problems.append('HEAD does not equal the review candidate commit')
        with tempfile.TemporaryDirectory(prefix='index-', dir=self.scratch) as temporary:
            env = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
            env.update(GIT_DIR=str(self.repo.common), GIT_WORK_TREE=str(checkout),
                       GIT_INDEX_FILE=str(Path(temporary) / 'index'), GIT_OPTIONAL_LOCKS='0',
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')

            def git(*args):
                result = process(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                  '-c', 'core.untrackedCache=false', *args], cwd=checkout, env=env)
                if result.returncode:
                    raise ApiError('review_checkout_modified', 'Cannot verify the candidate checkout: ' + result.stderr.strip())
                return result.stdout
            git('read-tree', self.candidate['tree'])
            git('add', '-u', '--', '.')
            tree = git('write-tree').strip()
            if tree != self.candidate['tree']:
                names = git('diff-tree', '-r', '--name-status', '--no-renames', self.candidate['tree'], tree)
                problems.extend(line for line in names.splitlines() if line)
                self.retain_modification(stage, tree, names)
        if problems and not (self.stage_dir(stage) / 'checkout-modified.txt').exists():
            (self.stage_dir(stage) / 'checkout-modified.txt').write_text('\n'.join(problems) + '\n')
        return problems

    def retain_modification(self, stage, tree, names):
        directory = self.stage_dir(stage)
        (directory / 'checkout-modified.txt').write_text(names)
        diff = owner_git.diff(self.session, self.candidate['tree'], tree)
        (directory / 'checkout-modified.diff').write_text(diff)

    def verify_checkout(self, stage, when):
        problems = self.changed_paths(stage)
        if problems:
            raise ApiError('review_checkout_modified',
                           f'The disposable candidate checkout for {stage} differs from the candidate '
                           f'{"before" if when == "before" else "after"} the stage; the result was discarded. '
                           'The live worktree is unchanged and no adoption is needed; resume to rerun in a fresh checkout.',
                           stage=stage, paths=problems[:20], artifacts=str(self.stage_dir(stage)))

    def close(self):
        """Remove disposable checkouts; scratch, logs, generated files and diffs remain."""
        for stage in list(self.checkouts):
            shutil.rmtree(self.checkout_path(stage), ignore_errors=True)
        self.checkouts.clear()


def owner_state_effective(value):
    from .owner_state import effective_settings
    return effective_settings(value)


# ---- the live Owner callback -------------------------------------------------------------------
def record_integrity(session, state, batch, path, message):
    batch['integrity_failure'] = str(path)
    batch.setdefault('integrity_events', []).append({'attempt': str(path), 'error': message, 'at': now()})
    session.save(state)


def verify_live(run, when, location=None):
    """Confirm the live Owner worktree and Git state still equal the reviewed candidate.

    The checks are the existing ones: ``owner_git.validate``, a new
    ``owner_git.checkpoint`` whose tree must equal both the saved checkpoint and
    the reviewed tree, ``owner_git.admin`` against ``batch['git_admin']``, an
    earlier ``integrity_failure`` and, before stages, ``owner_git.trust``. A
    violation after a stage is recorded like ``owner_providers.invoke`` does and
    leaves ``batch['checkpoint']`` unmoved, so resume requires explicit adoption.
    """
    session, state, batch = run.session, run.state, run.batch
    tree = run.candidate['tree']
    where = location or run.path
    if batch.get('integrity_failure'):
        raise ApiError('integrity_failed', 'Protected state changed; inspect and explicitly adopt before continuing.')

    def violation(message, code='integrity_failed'):
        if when == 'after':
            record_integrity(session, state, batch, where, message)
            raise ApiError('integrity_failed', message)
        raise ApiError(code, message)
    try:
        owner_git.validate(session, state)
    except ApiError as exc:
        if exc.code == 'integrity_failed':
            violation(str(exc))
        raise
    current = owner_git.checkpoint(session, state, f'review {when}')
    if current['tree'] != tree or current['tree'] != batch['checkpoint']['tree']:
        violation('The owner worktree no longer matches the reviewed candidate; inspect and adopt it before continuing.'
                  if when != 'after' else 'The review changed the live owner worktree; inspect the retained changes.',
                  code='worktree_changed')
    identity = owner_git.admin(session, state)
    if batch.get('git_admin', identity) != identity:
        violation('Protected Git state changed during review; inspect and explicitly adopt before continuing.')
    if when == 'before':
        owner_git.trust(session, state, tree)
