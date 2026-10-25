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


def inputs(run, stage, *, followup_input=None, followup_prompt=None):
    path, artifacts = run.path, {}
    if stage == 'synthesis':
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
    return canonical(dict(version=1, stage=stage, head=run.manifest['snapshot']['head'],
                          merge_base=run.manifest['snapshot']['merge_base'],
                          cli_version=run.tools[provider_for(run.settings, stage)],
                          command=cache_command(run.command(stage)), artifacts=artifacts), path)


def semantic(inputs):
    result = json.loads(json.dumps(inputs))
    stage = result.get('stage')
    artifacts = result.get('artifacts', {})
    if stage in LEGACY_VALIDATORS and artifacts.get('validator') == LEGACY_VALIDATORS[stage]:
        artifacts['validator'] = VALIDATORS[stage]
    return result


def reusable(run, stage, output, expected):
    record = run.manifest.get('phases', {}).get(stage, {})
    if not output.is_file() or not output.stat().st_size or record.get('status') != 'completed' or record.get('output_hash') != hash_file(output):
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
