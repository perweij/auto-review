"""Auditable stage identity, including verified pre-migration cache records.

Version 1 is the Bash wire serialization: sorted UTF-8 JSON plus one newline.
Validator identities are semantic, mapped only from these known source hashes.
An implementation edit therefore does not silently invalidate all user reviews.
"""
import json
from .common import ROOT
from .storage import hash_bytes, hash_file, json_bytes
from .providers import cache_command, provider_for

LEGACY_VALIDATORS = {
    'synthesis': '416ef4666ee3806d7ffed03187e89bb3943f2962c5810574ea10201af7f85951',
    'contributions': 'd102fc1309fba05f22172d13090367d51f23b8052fd61878d4a1459603fe4bfb',
    'followup': 'c10f160f6d4761d4f804622c49e560cc58713a6da89ea0407e8390eb175f4576',
}
VALIDATORS = {stage: 'pr-review/' + stage + '/1' for stage in LEGACY_VALIDATORS}


def canonical(value, directory):
    if isinstance(value, str):
        return value.replace(str(directory), '<run>')
    if isinstance(value, list):
        return [canonical(v, directory) for v in value]
    if isinstance(value, dict):
        return {k: canonical(v, directory) for k, v in sorted(value.items())}
    return value


def fingerprint(inputs):
    return hash_bytes(json_bytes(inputs, compact=True, sort_keys=True))


def context_hash(directory):
    # sha256sum escapes newlines and backslashes in filenames; context names are
    # fixed by the program and never contain either.
    listing = ''.join(hash_file(p) + '  ./' + p.name + '\n' for p in sorted(directory.iterdir())
                      if p.is_file() and not p.name.endswith('.tmp'))
    return hash_bytes(listing.encode())


def prompt_hash(stage, content, directory, *, legacy=False):
    if stage == 'contributions':
        template = (ROOT / 'prompts/contributions.txt').read_bytes()
        value = json.loads(content[len(template):])
        value['discussion'] = '<discussion snapshot>'
        if not legacy:
            value = canonical(value, directory)
        return hash_bytes(template + json_bytes(value, compact=True, sort_keys=True))
    return hash_bytes(json_bytes(content.decode().replace(str(directory), '<run>')))


def completed_slot_reports(run):
    """Return slot reports only when every configured slot has completed."""
    slots = run.settings.get('reviewer_slots', [])
    phases = run.manifest.get('phases', {})
    if not slots:
        return []
    reports = [(slot, run.path / 'reviews' / (slot['id'] + '.md')) for slot in slots]
    if not all(phases.get(slot['id'], {}).get('status') == 'completed'
               and report.is_file()
               and phases[slot['id']].get('output_hash') == hash_file(report)
               for slot, report in reports):
        return []
    return reports


def inputs(run, stage, *, followup_input=None, followup_prompt=None):
    path, artifacts = run.path, {}
    slot = next((item for item in run.settings.get('reviewer_slots', []) if item['id'] == stage), None)
    if stage == 'synthesis':
        reports = completed_slot_reports(run)
        if reports:
            artifacts = dict(context=context_hash(path / 'context'),
                             reviewers={slot['id']: dict(output=hash_file(report),
                                 fingerprint=run.manifest['phases'][slot['id']].get('fingerprint'))
                                 for slot, report in reports})
        else:
            artifacts = dict(context=context_hash(path / 'context'),
                             claude=hash_file(path / 'reviews/claude-independent.md') if run.dual else '',
                             codex=hash_file(path / 'reviews/codex.md') if run.dual else '')
    if stage == 'contributions':
        artifacts = dict(synthesis=hash_file(path / 'reviews/synthesis.json'), discussion=run.manifest['discussion']['hash'])
    if stage == 'followup':
        artifacts = dict(input=hash_bytes(followup_input) if followup_input is not None else hash_file(path / 'reviews/followup-input.json'))
    if stage in VALIDATORS:
        content = followup_prompt if followup_prompt is not None else (path / f'prompts/{stage}-prompt.txt').read_bytes()
        artifacts.update(prompt=prompt_hash(stage, content, path), validator=VALIDATORS[stage],
                         schema=hash_file(ROOT / f'prompts/{stage}.schema.json'))
    identity = dict(version=1, stage=stage, head=run.manifest['snapshot']['head'],
                    merge_base=run.manifest['snapshot']['merge_base'],
                    cli_version=run.tools[provider_for(run.settings, stage)],
                    command=cache_command(run.command(stage)), artifacts=artifacts)
    if slot is not None:
        identity['slot'] = {key: slot[key] for key in ('id', 'provider', 'model', 'effort')}
        identity['base'] = run.manifest['snapshot'].get('base')
        legacy = run.manifest.get('phases', {}).get(stage, {}).get('legacy_source')
        if legacy:
            identity['legacy_source'] = legacy
            identity['isolation'] = 'legacy-original-verification'
        else:
            identity['isolation'] = 'private-snapshot-bwrap-v1'
    return canonical(identity, path)


def semantic(inputs):
    result = json.loads(json.dumps(inputs))
    stage = result.get('stage')
    artifacts = result.get('artifacts', {})
    if stage in LEGACY_VALIDATORS and artifacts.get('validator') == LEGACY_VALIDATORS[stage]:
        artifacts['validator'] = VALIDATORS[stage]
    return result


def reusable(run, stage, output, expected):
    mapping = run.manifest.get('legacy_review_artifacts')
    if mapping and stage in ('claude', 'codex'):
        # Provider-named records predate slots. A matching provider alone is not
        # enough to assign one to a new slot after configuration changes.
        matches = [(slot_id, item) for slot_id, item in mapping.items()
                   if item.get('phase') == stage and run.path / item.get('output', '') == output]
        slots = run.settings.get('reviewer_slots', [])
        if len(matches) != 1:
            return False
        slot_id, item = matches[0]
        if not any(slot.get('id') == slot_id and all(slot.get(k) == item.get(k)
                   for k in ('provider', 'model', 'effort')) for slot in slots):
            return False
    record = run.manifest.get('phases', {}).get(stage, {})
    if not output.is_file() or not output.stat().st_size or record.get('status') != 'completed' or record.get('output_hash') != hash_file(output):
        return False
    # Older versions accepted these provider diagnostics as completed reports.
    # Preserve their files, but rerun them after the environment is repaired.
    from .providers import reported_execution_failure
    if reported_execution_failure(output.read_text(errors='replace')):
        return False
    prior = record.get('inputs')
    if prior is not None:
        if fingerprint(prior) != record.get('fingerprint'):
            return False
        previous_root = str(run.path)
        if stage in ('claude', 'codex') and record.get('stdout_log'):
            from pathlib import Path
            previous_root = str(Path(record['stdout_log']).parent.parent)
        comparable = semantic(canonical(prior, previous_root))
        if stage == 'contributions' and comparable != expected:
            # Bash excluded the fetched discussion path but hashed the absolute
            # findings path. Verify its old hash from the recorded source root
            # before upgrading to a relocation-stable prompt identity.
            from pathlib import Path
            old_root = str(Path(record.get('stdout_log', str(run.path / 'logs/contributions.raw.json'))).parent.parent)
            content = (run.path / 'prompts/contributions-prompt.txt').read_bytes()
            old_content = content.replace(str(run.path).encode(), old_root.encode())
            if prompt_hash(stage, old_content, old_root, legacy=True) == prior['artifacts'].get('prompt'):
                comparable['artifacts']['prompt'] = expected['artifacts']['prompt']
        if comparable != expected:
            return False
    else:
        if any(slot['id'] == stage for slot in run.settings.get('reviewer_slots', [])):
            # Slot records have always carried inputs. A missing record cannot
            # establish their provider, model, effort or isolation identity.
            return False
        original = run.original_manifest
        snapshot = original.get('snapshot', {})
        provider = provider_for(run.settings, stage)
        if (snapshot.get('head') != expected['head'] or snapshot.get('merge_base') != expected['merge_base']
                or original.get('tools', {}).get(provider) != expected['cli_version']):
            return False
        from .common import read_json
        commands = [c['argv'] for c in read_json(run.path / 'logs/commands.json', []) if c['stage'] == stage]
        if not commands or canonical(cache_command(commands[-1]), run.path) != expected['command']:
            return False
        if stage in ('synthesis', 'contributions'):
            content = run.original_prompts.get(stage)
            if content is None or prompt_hash(stage, content, run.path) != expected['artifacts']['prompt']:
                return False
            if stage == 'synthesis':
                if expected['artifacts']['context'] != original.get('phases', {}).get('context', {}).get('output_hash'):
                    return False
                if 'reviewers' in expected['artifacts']:
                    return False  # Legacy synthesis cannot establish slot attribution.
                if run.dual and any(expected['artifacts'][p] != original.get('phases', {}).get(p, {}).get('output_hash') for p in ('claude', 'codex')):
                    return False
            else:
                ledger = read_json(run.path / 'publication/contributions.json', {})
                if (ledger.get('discussion_hash') != expected['artifacts']['discussion']
                        or ledger.get('findings_hash') != expected['artifacts']['synthesis']):
                    return False
        elif stage == 'followup':
            return False  # No pre-input follow-up format existed.
    if prior != expected:
        record.update(inputs=expected, fingerprint=fingerprint(expected))
        run.save()
        if prior is None:
            from .sessions import say
            say(f'Verified saved {stage} inputs; upgraded its cache record.')
    return True
