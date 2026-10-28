"""Structured code suggestions, isolated verification, and shared recovery rules.

Bodies stay editable prose. Only this module renders actionable Markdown, and
verification is bound to the exact prose, replacement, source, and model inputs.
"""
import copy
from contextlib import contextmanager
import difflib
import json
from pathlib import Path
import re
import tempfile
import uuid

from .common import ApiError, ROOT, atomic_json, digest, git, locked, now, read_json
from .processes import Cancelled, run as process
from . import providers, settings

VERSION = 1
RECOVERY = 'Edit and verify the suggestion, remove it to keep the finding, or suppress the finding.'
# Conservatively reject even nested/quoted fences; ordinary code fences are fine.
RAW_FENCE = re.compile(r'(?:`{3,}|~{3,})[ \t]*suggestion(?:\s|:|$)', re.I)


def lines(text):
    """Git/GitHub line numbers count LF, not Unicode separators or form feeds."""
    return [line + '\n' for line in text.split('\n')[:-1]]


def source(session, path):
    text = session.git.source(path)['text']
    if any(c in text for c in ('\r', '\0', '\ufffd')) or (text and not text.endswith('\n')):
        raise ApiError('invalid_suggestion', 'Suggestions require UTF-8 text with LF endings and a final newline.')
    mode = session.git.run(['ls-tree', session.git.head, '--', path]).split(' ', 1)[0]
    if mode not in ('100644', '100755'):
        raise ApiError('invalid_suggestion', 'Suggestions require a regular source file.')
    return text


def validate(session, comment):
    suggestion = comment.get('suggestion')
    if not isinstance(suggestion, dict):
        raise ApiError('invalid_suggestion', 'No structured suggestion is attached.')
    target = session.git.anchor(suggestion.get('target'))
    if (target['side'] != 'RIGHT' or suggestion.get('head') != session.git.head
            or session.git.anchor(comment.get('placement')) != target):
        raise ApiError('invalid_suggestion', 'Suggestion target must match this comment on the reviewed HEAD, on the RIGHT side.')
    if len(comment.get('finding_ids', [])) != 1 or comment.get('classification') != 'NEW':
        raise ApiError('invalid_suggestion', 'Suggestions require one finding in a new inline comment.')
    original, replacement = suggestion.get('original'), suggestion.get('replacement')
    if not isinstance(original, str) or not isinstance(replacement, str):
        raise ApiError('invalid_suggestion', 'Suggestion source and replacement must be strings.')
    try:
        replacement.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ApiError('invalid_suggestion', 'Replacement must be valid UTF-8 text.') from exc
    if any(c in replacement for c in ('\r', '\0', '\ufffd')) or (replacement and not replacement.endswith('\n')):
        raise ApiError('invalid_suggestion', 'Replacement must use LF endings, including a final newline, or be empty for deletion.')
    # A fixed triple-backtick fence is the interoperable GitHub representation.
    if re.search(r'(?m)^[ \t]*`{3,}', replacement):
        raise ApiError('invalid_suggestion', 'Replacement contains a Markdown fence that cannot be represented safely.')
    original_lines = lines(source(session, target['path']))
    if original != ''.join(original_lines[target['start_line'] - 1:target['line']]):
        raise ApiError('invalid_suggestion', 'Suggestion original text does not exactly match the reviewed source.')
    if original == replacement:
        raise ApiError('invalid_suggestion', 'Suggestion makes no change.')
    fence = None
    for line in comment['body'].splitlines():
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if match:
            delimiter, rest = match.groups()
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence) and not rest.strip():
                fence = None
    if (fence or '<!--' in comment['body']
            or re.search(r'(?i)<(?:script|style|pre|textarea)\b', comment['body'])
            or re.search(r'(?im)^\s*<(?:div|table)\b', comment['body'])):
        raise ApiError('invalid_suggestion', 'Close Markdown fences and remove HTML blocks before attaching a suggestion.')
    return target


def identity(session, comment):
    configuration = settings.from_manifest(session.manifest)
    provider = configuration['processing_provider']
    return digest(dict(version=VERSION, head=session.git.head, base=session.git.base,
                       finding_ids=comment.get('finding_ids'), evidence=comment.get('evidence'),
                       classification=comment.get('classification'), body=comment['body'], placement=comment['placement'],
                       suggestion={k: comment['suggestion'].get(k) for k in ('head', 'target', 'original', 'replacement')},
                       provider=provider, model=configuration[provider + '_model'], effort=configuration[provider + '_effort'],
                       tools=session.manifest.get('tools'),
                       prompt=(ROOT / 'prompts/suggestion-verify.txt').read_text(),
                       policy=(ROOT / 'prompts/synthesis-testing-focused.txt').read_text(),
                       schema=(ROOT / 'prompts/suggestion-verify.schema.json').read_text()))


def verified(session, comment):
    record = comment.get('suggestion', {}).get('verification', {})
    return record.get('status') == 'accepted' and record.get('fingerprint') == identity(session, comment)


def overlaps(left, right):
    a, b = left['suggestion']['target'], right['suggestion']['target']
    return a['path'] == b['path'] and max(a['start_line'], b['start_line']) <= min(a['line'], b['line'])


def batch_identity(session, comments):
    return digest(sorted(identity(session, c) for c in comments))


def selected_suggestions(state, comment=None, bulk=False):
    return [c for c in state['comments'] if c.get('suggestion') and
            (c['status'] == 'staged' or (bulk and c['status'] == 'unstaged') or (comment is not None and c['id'] == comment['id']))]


def blockers(session, comment, state, *, bulk=False):
    reasons = []
    if RAW_FENCE.search(comment.get('body', '')):
        reasons.append('Remove the raw suggestion fence from the comment body; only verified structured suggestions can be published.')
    if comment.get('suggestion_generation', {}).get('status') in ('pending', 'failed'):
        reasons.append('Suggestion generation is incomplete. Resume the review, or remove the suggestion to retain the ordinary finding.')
    if not comment.get('suggestion'):
        return reasons
    try:
        validate(session, comment)
        if not verified(session, comment):
            reasons.append('Suggestion requires verification. ' + RECOVERY)
        selected = selected_suggestions(state, comment, bulk)
        if any(c['id'] != comment['id'] and overlaps(c, comment) for c in selected):
            reasons.append('Suggestion overlaps another selected replacement. Remove one suggestion or suppress its finding.')
        if len(selected) > 1:
            record = state.get('suggestion_batch', {})
            if record.get('status') != 'accepted' or not {identity(session, c) for c in selected}.issubset(set(record.get('members', []))):
                reasons.append('Verify the selected suggestions together before staging or publishing.')
    except (ApiError, KeyError, TypeError, ValueError) as exc:
        reasons.append(str(exc) + ' ' + RECOVERY)
    return reasons


def render(comment):
    suggestion = comment.get('suggestion')
    if not suggestion:
        return comment['body']
    replacement = suggestion['replacement']
    return comment['body'] + '\n\n```suggestion\n' + replacement + '```\n'


def preview(comment):
    suggestion = comment.get('suggestion')
    if not suggestion:
        return None
    target = suggestion['target']
    return dict(target=target, diff=''.join(difflib.unified_diff(
        lines(suggestion['original']), lines(suggestion['replacement']),
        fromfile=target['path'] + ' (original range)', tofile=target['path'] + ' (replacement)')),
        verification=suggestion.get('verification', {}))


@contextmanager
def checkout(session, comments):
    """Private repository; no linked worktree or writable shared Git metadata."""
    with tempfile.TemporaryDirectory(prefix='pr-suggestion-') as directory:
        root = Path(directory) / 'source'
        git(['clone', '--shared', '--no-checkout', '--', session.git.common, str(root)])
        git(['checkout', '--detach', session.git.head], cwd=root)
        for comment in sorted(comments, key=lambda c: c['suggestion']['target']['start_line'], reverse=True):
            target = validate(session, comment)
            path = root / target['path']
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ApiError('invalid_suggestion', 'Replacement target is not a regular file in the isolated checkout.')
            file_lines = lines(path.read_bytes().decode('utf-8'))
            start, end = target['start_line'] - 1, target['line']
            if ''.join(file_lines[start:end]) != comment['suggestion']['original']:
                raise ApiError('invalid_suggestion', 'Suggestions overlap or do not match their isolated source.')
            path.write_bytes((''.join(file_lines[:start]) + comment['suggestion']['replacement'] + ''.join(file_lines[end:])).encode())
        yield root


def invoke(session, stage, context, cwd):
    """Use existing provider/environment policy; retain every attempt's evidence."""
    configuration = settings.from_manifest(session.manifest)
    runtime = providers.Runtime.environment()
    directory = session.path / 'suggestions' / uuid.uuid4().hex
    directory.mkdir(parents=True)
    for folder in ('reviews', 'context', 'logs'):
        (directory / folder).mkdir()
    prompt = (ROOT / f'prompts/{stage}.txt').read_text()
    if stage == 'suggestion-verify':
        prompt += '\n' + (ROOT / 'prompts/synthesis-testing-focused.txt').read_text()
    prompt += '\n' + json.dumps(context, ensure_ascii=True)
    (directory / 'prompt.txt').write_text(prompt)
    argv = providers.review_command(configuration, runtime, stage, directory, session.git.base)
    if stage == 'suggestion-verify' and configuration['processing_provider'] == 'codex':
        argv[argv.index('sandbox_mode=read-only')] = 'sandbox_mode=workspace-write'
        argv[2:2] = ['--ignore-rules', '-c', 'sandbox_workspace_write.writable_roots=[]',
                     '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=true',
                     '-c', 'sandbox_workspace_write.exclude_slash_tmp=true',
                     '-c', 'sandbox_workspace_write.network_access=false']
    env, removed = providers.environment(configuration['processing_provider'])
    from .sessions import say
    say(f'[{stage}] {configuration["processing_provider"]} / {configuration[configuration["processing_provider"] + "_model"]}; artifacts: {directory}')
    atomic_json(directory / 'invocation.json', dict(argv=argv, cwd=str(cwd), removed_environment=removed))
    result = process(argv, cwd=cwd, env=env, input_bytes=prompt.encode(), timeout=runtime.model_timeout,
                     stdout_path=directory / 'stdout.jsonl', stderr_path=directory / 'stderr.log',
                     model_events=True, stage=stage)
    if result.returncode:
        raise ApiError('suggestion_failed', f'Suggestion model failed; inspect {directory}.')
    if configuration['processing_provider'] == 'codex':
        value = read_json(directory / f'reviews/{stage}.codex.tmp')
    else:
        events = [e for e in result.events if e.get('type') == 'result']
        event = events[-1] if events else {}
        if event.get('is_error') or event.get('subtype') != 'success':
            raise ApiError('suggestion_failed', f'No successful model result; inspect {directory}.')
        try:
            value = event.get('structured_output') or json.loads(event.get('result', ''))
        except ValueError as exc:
            raise ApiError('suggestion_failed', f'Invalid model result; inspect {directory}.') from exc
    if not isinstance(value, dict):
        raise ApiError('suggestion_failed', f'No structured result; inspect {directory}.')
    atomic_json(directory / 'result.json', value)
    return value, str(directory)


def verify_result(value):
    if (set(value) != {'accepted', 'evidence', 'checks', 'inspection_only_reason'}
            or type(value['accepted']) is not bool or not isinstance(value['evidence'], str) or not value['evidence'].strip()
            or not isinstance(value['inspection_only_reason'], str) or not isinstance(value['checks'], list)
            or any(not isinstance(c, dict) or set(c) != {'command', 'result', 'passed'}
                   or not all(isinstance(c[k], str) and c[k].strip() for k in ('command', 'result'))
                   or type(c['passed']) is not bool for c in value['checks'])):
        raise ApiError('invalid_suggestion', 'Verification returned invalid evidence; verify again or remove the suggestion.')
    if value['accepted'] and (any(not c['passed'] for c in value['checks'])
                              or (not value['checks'] and not value['inspection_only_reason'].strip())):
        raise ApiError('invalid_suggestion', 'Acceptance requires passing checks or a justified inspection-only assessment.')
    if value['checks'] and value['inspection_only_reason'].strip():
        raise ApiError('invalid_suggestion', 'Verification reports both executed checks and inspection-only acceptance; verify again.')
    return value


def verify_set(session, comments):
    if any(overlaps(a, b) for i, a in enumerate(comments) for b in comments[i + 1:]):
        raise ApiError('invalid_suggestion', 'Selected suggestions overlap; remove one or suppress its finding.')
    with checkout(session, comments) as root:
        before = git(['diff', '--binary', 'HEAD', '--'], cwd=root).stdout
        value, log = invoke(session, 'suggestion-verify', dict(
            head=session.git.head, combined=len(comments) > 1,
            findings=[dict(id=c['id'], body=c['body'], evidence=c.get('evidence', []),
                           suggestion={k: v for k, v in c['suggestion'].items() if k != 'verification'}) for c in comments],
            instructions='Replacements are already applied in the current isolated checkout. Compare with HEAD. Do not change tracked files.'), root)
        if (git(['rev-parse', 'HEAD'], cwd=root).stdout.strip() != session.git.head
                or git(['diff', '--binary', 'HEAD', '--'], cwd=root).stdout != before
                or git(['diff', '--cached', '--quiet'], cwd=root, check=False).returncode):
            raise ApiError('invalid_suggestion', 'Verification changed the proposed source; acceptance discarded.')
        value = verify_result(value)
        return dict(status='accepted' if value['accepted'] else 'rejected', at=now(), log_path=log, **value)


def verify_comments(session, state, comments):
    """Caller holds PR and draft locks. Save before and after every model call."""
    for comment in comments:
        if verified(session, comment):
            continue
        record = dict(status='pending', fingerprint=identity(session, comment))
        comment['suggestion']['verification'] = record
        session.save(state)
        try:
            record.update(verify_set(session, [comment]))
            if record['status'] == 'accepted':
                comment.setdefault('suggestion_generation', {})['status'] = 'generated'
        except Cancelled:
            record.update(status='interrupted')
            raise
        except ApiError as exc:
            record.update(status='failed', evidence=str(exc))
        finally:
            session.save(state)
    if len(comments) > 1 and all(verified(session, c) for c in comments):
        fingerprint = batch_identity(session, comments)
        if state.get('suggestion_batch', {}).get('fingerprint') == fingerprint and state['suggestion_batch'].get('status') == 'accepted':
            return
        record = dict(status='pending', fingerprint=fingerprint, members=[identity(session, c) for c in comments])
        state['suggestion_batch'] = record
        session.save(state)
        try:
            record.update(verify_set(session, comments))
        except Cancelled:
            record.update(status='interrupted')
            raise
        except ApiError as exc:
            record.update(status='failed', evidence=str(exc))
        finally:
            session.save(state)


def generation_identity(session, comment):
    return digest(dict(version=VERSION, head=session.git.head, base=session.git.base,
                       body=comment['body'], findings=comment['finding_ids'], evidence=comment.get('evidence'),
                       placement=comment.get('suggestion_original_placement', comment['placement']),
                       settings=settings.from_manifest(session.manifest), tools=session.manifest.get('tools'),
                       prompt=(ROOT / 'prompts/suggestions.txt').read_text(),
                       schema=(ROOT / 'prompts/suggestions.schema.json').read_text()))


def generate(run):
    session = run.session()
    with locked(session.lock, wait=True):
        state = session.load()
        for comment in state['comments']:
            if (comment['status'] in ('published', 'suppressed') or comment.get('human_edited')
                    or comment.get('mutation_actor') == 'manual'):
                continue
            generation = comment.get('suggestion_generation', {})
            if generation.get('status') == 'removed':
                continue
            fingerprint = generation_identity(session, comment)
            if generation.get('fingerprint') == fingerprint and (comment.get('suggestion') or generation.get('status') == 'omitted'):
                continue
            if comment.get('suggestion'):
                from .commands import remember
                remember(comment)
                comment.pop('suggestion')
                comment['placement'] = comment.pop('suggestion_original_placement', comment['automatic'])
            if (comment['classification'] != 'NEW' or len(comment['finding_ids']) != 1
                    or comment['placement'].get('kind') != 'inline'):
                comment['suggestion_generation'] = dict(status='omitted', reason='Requires one finding in a new inline comment.', fingerprint=fingerprint)
                session.save(state)
                continue
            comment['suggestion_generation'] = dict(status='pending', fingerprint=fingerprint)
            session.save(state)
            try:
                # Generation is read-only and sees an untouched snapshot.
                with checkout(session, []) as root:
                    value, log = invoke(session, 'suggestions', dict(head=session.git.head, merge_base=session.git.base, comment=comment,
                        diff=session.git.diff(comment['placement']['path'])['patch']), root)
                if set(value) != {'reason', 'suggestion'} or not isinstance(value['reason'], str) or not value['reason'].strip():
                    raise ApiError('invalid_suggestion', 'Generation returned invalid suggestion data.')
                candidate = value['suggestion']
                if candidate is None:
                    comment['suggestion_generation'] = dict(status='omitted', reason=value['reason'], log_path=log)
                else:
                    if (not isinstance(candidate, dict) or set(candidate) != {'target', 'original', 'replacement'}
                            or not isinstance(candidate['original'], str) or not isinstance(candidate['replacement'], str)):
                        raise ApiError('invalid_suggestion', 'Generation returned an invalid replacement.')
                    target = session.git.anchor(candidate['target'])
                    comment['suggestion_original_placement'] = copy.deepcopy(comment['placement'])
                    comment['suggestion'] = {**candidate, 'target': target, 'head': session.git.head}
                    comment['placement'] = target
                    comment['status'] = 'unstaged'
                    state.pop('prepared', None)
                    comment['suggestion_generation'] = dict(status='generated', reason=value['reason'], log_path=log)
                    validate(session, comment)
            except Cancelled:
                raise
            except ApiError as exc:
                comment['suggestion_generation'] = dict(status='failed', reason=str(exc))
            finally:
                comment['suggestion_generation']['fingerprint'] = fingerprint
                session.save(state)
        comments = [c for c in state['comments'] if c.get('suggestion') and c['status'] not in ('suppressed', 'published')]
        verify_comments(session, state, comments)
    run.reload()


def verify_operation(session, request):
    from .operations import operation
    from .repository import Repository
    from .commands import mutable
    with operation({'kind': 'suggestion-verification', 'common': session.git.common, 'pr': str(session.manifest['pr'])}):
        with locked(session.pr_lock), locked(session.lock):
            state = session.load()
            session.check_write(request, state)
            comments = [c for c in state['comments'] if c.get('suggestion') and c['status'] in ('unstaged', 'staged')]
            if not comments:
                raise ApiError('invalid_request', 'No active suggestions to verify.')
            for comment in comments:
                mutable(comment, state)
            repo = Repository.from_manifest(session.manifest)
            repo.validate_publication(session.path, 'COMMENT')
            verify_comments(session, state, comments)
            state.pop('prepared', None)
            session.save(state)
            return {'review': session.view(state)}
