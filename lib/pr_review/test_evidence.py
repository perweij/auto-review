"""Bounded, provenance-bearing implementation evidence; never execution attestation.

Only declared command records and scratch logs leave the provider stream. Frozen
bundles are immutable inputs to issue synthesis, independent of model invocation.
"""
import copy
import json
import os
from pathlib import Path
import re
import shutil
import stat

from .common import ApiError
from .storage import hash_bytes, json_bytes, sync_directory, write_json

VERSION = 1
MAX_STREAM = 32 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
MAX_OUTPUT = 128 * 1024
MAX_LOG = 4 * 1024 * 1024
MAX_LOG_TOTAL = 16 * 1024 * 1024
MAX_DECL_TEXT = 128 * 1024
MAX_DECL_BYTES = 1024 * 1024
MAX_DECLARATIONS = 64
MAX_LOGS = 16
LIMITATION = ('Provider-reported execution, not independent attestation. Command-time source identity is unknown; '
              'equal before/after checkpoints do not establish which bytes a command tested. '
              'Selected output is untrusted and potentially sensitive; universal secret redaction is not claimed.')


def declarations(value):
    if not isinstance(value, list) or len(value) > MAX_DECLARATIONS:
        raise ApiError('invalid_result', 'test_evidence must contain at most 64 declarations.')
    required = {'command', 'cwd', 'log_paths', 'tested_tree', 'summary'}
    for row in value:
        if (not isinstance(row, dict) or not required <= set(row) or set(row) - required - {'tool_id', 'event_id'}
                or any(not isinstance(row[k], str) or not row[k].strip() for k in required - {'log_paths'})
                or any(len(row[k].encode()) > MAX_DECL_TEXT for k in required - {'log_paths'})
                or not isinstance(row['log_paths'], list) or len(row['log_paths']) > MAX_LOGS
                or any(not isinstance(p, str) or not p or len(p) > 4096 for p in row['log_paths'])
                or any(row.get(k) is not None and (not isinstance(row[k], str) or not row[k] or len(row[k]) > 4096)
                       for k in ('tool_id', 'event_id'))):
            raise ApiError('invalid_result', 'Test evidence needs command, cwd, log_paths, tested_tree, summary and optional tool/event IDs.')
    if len(json_bytes(value)) > MAX_DECL_BYTES:
        raise ApiError('invalid_result', 'Test evidence declarations exceed the 1 MiB metadata limit.')
    return copy.deepcopy(value)


def _relative(value):
    path = Path(value)
    if not isinstance(value, str) or not value or path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('path must be relative without traversal')
    return path


def _directory(path):
    """Open every directory component without following symlinks."""
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(root, relative, limit):
    """No symlinks or special files, including FIFOs; no unbounded reads."""
    path = _relative(relative)
    fd = _directory(root)
    file_fd = None
    try:
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('not a regular file')
        if before.st_size > limit:
            raise ValueError(f'file exceeds {limit} byte ingestion limit; not copied')
        with os.fdopen(file_fd, 'rb') as stream:
            file_fd = None
            data = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != after.st_size:
            raise ValueError('file changed during bounded read')
        return data
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(fd)


def _numeric(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _truncated(text, flags=()):
    return any(f is True for f in flags) or bool(re.search(
        r'\b(?:tokens?|characters?) truncated\b|\[output truncated\]|Warning:\s*truncated output\b', text, re.I))


def _output(value, *flags):
    text = value if isinstance(value, str) else ''
    data = text.encode()
    explicit = _truncated(text, flags)
    return {'text': data[:MAX_OUTPUT].decode(errors='replace'), 'bytes_reported': len(data),
            'reported_sha256': hash_bytes(data),
            'truncated': explicit or len(data) > MAX_OUTPUT,
            'provider_truncation': 'reported' if explicit else 'unknown',
            'capture_truncated': len(data) > MAX_OUTPUT}


def normalize(directory, provider):
    """Normalize Codex item lifecycle and Claude Bash tool/result pairs separately.

    No shell parsing and no status extraction from output prose. Missing numeric
    Claude status remains null even for a non-error tool result.
    """
    gaps = []
    records = []
    pending = {}
    # Bound both the total input and each JSON event. Large streams are a gap,
    # rather than reading their whole contents or blessing partial records.
    try:
        data = _read(directory, 'events.jsonl', MAX_STREAM)
    except (OSError, ValueError) as exc:
        return [], [f'provider stream unavailable: {exc}']
    for number, line in enumerate(data.splitlines(), 1):
        if len(line) > MAX_LINE:
            gaps.append(f'provider event line {number} exceeds ingestion limit')
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            gaps.append(f'provider event line {number} is incomplete or invalid JSON')
            continue
        if not isinstance(event, dict):
            continue
        if provider == 'codex':
            item = event.get('item')
            if (event.get('type') not in ('item.started', 'item.updated', 'item.completed')
                    or not isinstance(item, dict) or item.get('type') != 'command_execution'):
                continue
            tool_id = item.get('id') if isinstance(item.get('id'), str) else None
            key = tool_id or f'line-{number}'
            record = pending.get(key)
            if record is None:
                record = dict(provider=provider, tool_id=tool_id, event_ids=[], command=None, cwd=None,
                              completion='incomplete', exit_code=None, source_identity='unknown',
                              output=_output(None))
                pending[key] = record
                records.append(record)
            record['event_ids'].append(event.get('id') if isinstance(event.get('id'), str) else f'line:{number}')
            for field in ('command', 'cwd'):
                if isinstance(item.get(field), str):
                    record[field] = item[field]
            if event['type'] == 'item.completed':
                record['completion'] = item.get('status') if isinstance(item.get('status'), str) else 'completed'
                record['exit_code'] = _numeric(item.get('exit_code'))
            record['output'] = _output(item.get('aggregated_output'), item.get('truncated'), item.get('output_truncated'))
        elif provider == 'claude':
            message = event.get('message')
            content = message.get('content', []) if isinstance(message, dict) else []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if event.get('type') == 'assistant' and block.get('type') == 'tool_use' and block.get('name') == 'Bash':
                    inp = block.get('input') if isinstance(block.get('input'), dict) else {}
                    tool_id = block.get('id') if isinstance(block.get('id'), str) else None
                    record = dict(provider=provider, tool_id=tool_id,
                                  event_ids=[event['uuid'] if isinstance(event.get('uuid'), str) and len(event['uuid']) <= 4096 else f'line:{number}'],
                                  command=inp.get('command') if isinstance(inp.get('command'), str) else None,
                                  cwd=inp.get('cwd') if isinstance(inp.get('cwd'), str) else None,
                                  completion='incomplete', exit_code=None, source_identity='unknown', output=_output(None))
                    records.append(record)
                    if tool_id:
                        pending.setdefault(tool_id, []).append(record)
                elif event.get('type') == 'user' and block.get('type') == 'tool_result':
                    identity = block.get('tool_use_id')
                    if not isinstance(identity, str):
                        gaps.append(f'Claude result at line {number} has invalid tool identity')
                        continue
                    matches = pending.get(identity, [])
                    if not matches:
                        continue  # Other tool results are deliberately excluded.
                    if len(matches) != 1:
                        gaps.append(f'Claude result at line {number} has no unique Bash tool use')
                        continue
                    record = matches[0]
                    record['event_ids'].append(event['uuid'] if isinstance(event.get('uuid'), str) and len(event['uuid']) <= 4096 else f'line:{number}')
                    record['completion'] = 'failed' if block.get('is_error') is True else 'completed'
                    raw = event.get('tool_use_result')
                    raw = raw if isinstance(raw, dict) else {}
                    code = block.get('exit_code', raw.get('exit_code', raw.get('exitCode')))
                    record['exit_code'] = _numeric(code)
                    body = block.get('content')
                    if isinstance(body, list):
                        body = '\n'.join(b['text'] for b in body if isinstance(b, dict) and b.get('type') == 'text'
                                         and isinstance(b.get('text'), str))
                    record['output'] = _output(body, block.get('truncated'), raw.get('truncated'), raw.get('output_truncated'))
    return records, gaps


def match(declaration, records):
    matches = [r for r in records if r['command'] == declaration['command']
               and (not declaration.get('tool_id') or r['tool_id'] == declaration['tool_id'])
               and (not declaration.get('event_id') or declaration['event_id'] in r['event_ids'])]
    if len(matches) != 1:
        return None, 'ambiguous recorded command' if matches else 'no exact recorded command'
    return matches[0], None


def _artifact(root, name, data):
    from .storage import atomic_bytes
    atomic_bytes(root / name, data)
    return {'path': name, 'sha256': hash_bytes(data), 'bytes': len(data)}


def _load(root, name, limit=MAX_STREAM):
    return json.loads(_read(root, name, limit))


def _error(message):
    raise ApiError('test_evidence_integrity', message)


def _check_artifact(root, row):
    data = _read(root, row['path'], MAX_STREAM)
    if len(data) != row['bytes'] or hash_bytes(data) != row['sha256']:
        _error(f'Test evidence artifact changed: {root / row["path"]}')
    return data


def verify(reference):
    """Verify the frozen copy and selected original inputs before use or reuse."""
    try:
        directory = Path(reference['attempt_path'])
        root = directory / reference.get('bundle', 'test-evidence')
        if reference.get('bundle', 'test-evidence') not in ('test-evidence', 'test-evidence.pending'):
            _error('Invalid test evidence bundle path')
        data = _read(root, 'manifest.json', MAX_STREAM)
        if hash_bytes(data) != reference['sha256']:
            _error(f'Test evidence manifest changed: {root / "manifest.json"}')
        manifest = json.loads(data)
        if manifest['format_version'] != VERSION or manifest['controller']['attempt_path'] != str(directory):
            _error('Test evidence format or attempt identity changed')
        _check_artifact(directory, manifest['result'])
        records, gaps = normalize(directory, manifest['controller']['provider'])
        if gaps != manifest['stream_gaps']:
            _error(f'Test evidence provider stream gaps changed: {directory / "events.jsonl"}')
        for selected in manifest['commands']:
            original, gap = match(selected['declaration'], records)
            if gap or hash_bytes(json_bytes(original, sort_keys=True)) != selected['source_sha256']:
                _error(f'Selected provider command changed: {directory / "events.jsonl"}')
            _check_artifact(root, selected['output'])
        # Originally missing/ambiguous matches remain immutable gaps. They cannot
        # become passing evidence just because source artifacts later appear.
        for log in manifest['logs']:
            _check_artifact(root, log)
            source = _read(directory / 'scratch', log['source_path'], MAX_LOG)
            if hash_bytes(source) != log['sha256'] or len(source) != log['bytes']:
                _error(f'Test evidence source log changed: {directory / "scratch" / log["source_path"]}')
        return manifest
    except ApiError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _error(f'Test evidence integrity check failed: {exc}')


def capture(record):
    """Freeze once; a durable seal recovers a crash before reference publication."""
    directory = Path(record['path'])
    final, pending = directory / 'test-evidence', directory / 'test-evidence.pending'
    if record.get('test_evidence_bundle'):
        verify(record['test_evidence_bundle'])
        return record['test_evidence_bundle']
    seal_path = directory / 'test-evidence-seal.json'
    if seal_path.exists() or seal_path.is_symlink():
        try:
            seal = _load(directory, seal_path.name)
            reference = {'attempt_path': str(directory), 'bundle': 'test-evidence', 'sha256': seal['sha256']}
            if not final.exists():
                verify({**reference, 'bundle': 'test-evidence.pending'})
                os.rename(pending, final)
                sync_directory(directory)
            verify(reference)
            return reference
        except ApiError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _error(f'Cannot recover frozen test evidence: {exc}')
    if final.exists() or final.is_symlink():
        _error(f'Frozen test evidence has no seal: {final}')
    # A staging directory without a seal is never reused as completed evidence.
    if pending.is_symlink():
        _error('Symlink in test evidence staging path')
    if pending.exists():
        shutil.rmtree(pending)
    pending.mkdir(mode=0o700)
    declared = declarations(record['result']['test_evidence'])
    result = _read(directory, 'result.json', MAX_STREAM)
    if json.loads(result) != record['result']:
        _error('Saved implementation result differs from controller record')
    records, stream_gaps = normalize(directory, record['choice']['provider'])
    manifest = {'format_version': VERSION,
                'controller': {'attempt_id': directory.name, 'attempt_path': str(directory),
                               'provider': record['choice']['provider'], 'choice': record['choice'],
                               'before': record.get('before'), 'after': record.get('after'),
                               'protected_state': record.get('protected_state', 'unknown'),
                               'git_admin_before': record.get('git_admin_before'),
                               'git_admin_after': record.get('git_admin_after'),
                               'provider_completion': record.get('provider_completion', 'unknown')},
                'declarations': declared, 'commands': [], 'logs': [], 'gaps': [], 'stream_gaps': stream_gaps,
                'limitations': LIMITATION,
                'result': {'path': 'result.json', 'sha256': hash_bytes(result), 'bytes': len(result)}}
    total = 0
    for index, declaration in enumerate(declared):
        command, gap = match(declaration, records)
        if gap:
            manifest['gaps'].append({'declaration': index, 'reason': gap})
        else:
            output = command['output']
            selected = {k: v for k, v in command.items() if k != 'output'}
            selected.update(declaration=declaration, declaration_index=index,
                            source_sha256=hash_bytes(json_bytes(command, sort_keys=True)),
                            output={**_artifact(pending, f'commands/{index}.txt', output['text'].encode()),
                                    **{k: v for k, v in output.items() if k != 'text'}})
            manifest['commands'].append(selected)
            if command['cwd'] is not None and command['cwd'] != declaration['cwd']:
                manifest['gaps'].append({'declaration': index, 'reason': 'declared cwd differs from provider-reported cwd'})
        for offset, name in enumerate(declaration['log_paths']):
            try:
                data = _read(directory / 'scratch', name, min(MAX_LOG, MAX_LOG_TOTAL - total))
                total += len(data)
                manifest['logs'].append({**_artifact(pending, f'logs/{index}-{offset}.log', data),
                                         'source_path': name, 'declaration': index,
                                         'truncated': _truncated(data.decode(errors='replace')),
                                         'provider_truncation': 'reported' if _truncated(data.decode(errors='replace')) else 'unknown',
                                         'capture_truncated': False})
            except (OSError, ValueError) as exc:
                manifest['gaps'].append({'declaration': index, 'path': name, 'reason': str(exc)})
    write_json(pending / 'manifest.json', manifest)
    sha = hash_bytes(_read(pending, 'manifest.json', MAX_STREAM))
    write_json(seal_path, {'format_version': VERSION, 'sha256': sha})
    reference = {'attempt_path': str(directory), 'bundle': 'test-evidence', 'sha256': sha}
    verify({**reference, 'bundle': 'test-evidence.pending'})
    os.rename(pending, final)
    sync_directory(directory)
    return reference


def references(batch):
    return [copy.deepcopy(a['test_evidence_bundle']) for a in batch.get('attempts', [])
            if a.get('kind') == 'issue_harness' and a.get('test_evidence_version') == VERSION
            and a.get('test_evidence_bundle')]


def index(references):
    """Compact synthesis data; large selected outputs stay in artifact files."""
    result = []
    for reference in references:
        manifest = verify(reference)
        root = Path(reference['attempt_path']) / reference['bundle']
        result.append({'manifest': str(root / 'manifest.json'), 'sha256': reference['sha256'],
                       'controller': manifest['controller'], 'declarations': manifest['declarations'],
                       'commands': manifest['commands'], 'logs': manifest['logs'],
                       'gaps': manifest['gaps'], 'stream_gaps': manifest['stream_gaps'],
                       'artifact_root': str(root), 'limitations': manifest['limitations']})
    return result


def proof_current(batch):
    proof = batch.get('harness_evidence_proof')
    versioned = any(a.get('test_evidence_version') == VERSION for a in batch.get('attempts', []))
    if not versioned:
        return True
    if not isinstance(proof, dict) or proof.get('tree') != batch.get('final_tree') or proof.get('bundles') != references(batch):
        return False
    try:
        for reference in proof['bundles']:
            verify(reference)
    except ApiError:
        return False
    return True
