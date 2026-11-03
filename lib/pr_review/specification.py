"""One canonical Markdown specification document per issue-plan revision.

The document is the only authoritative statement of the approved requirements.
The approval comment, the Owner copy, the implementation harness and the
acceptance review all use its exact bytes. This module owns:

* rendering, durable storage and byte-exact verification of the document, and
* the mechanical completeness checks applied to planner output.

Mechanical checks establish that the response is well formed and that the
standard's elements are present. They cannot establish that its decisions are
sound; that remains the user's approval and the independent acceptance review.
"""
import re
from pathlib import Path

from .common import ApiError, digest
from .storage import atomic_bytes, hash_bytes

# GitHub rejects issue comments longer than this many characters.
COMMENT_LIMIT = 65536
# Conservative bound for planner output: leaves room for the revision heading,
# approval instructions and the hidden workflow marker added at publication.
DOCUMENT_LIMIT = COMMENT_LIMIT - 1024

REQUIRED_SECTIONS = ('Outcome', 'Scope', 'Approach', 'Acceptance scenarios', 'Verification')
CONDITIONAL_SECTIONS = ('Interfaces and defaults', 'Errors and compatibility', 'Design decisions',
                        'Assumptions and resolved choices')
KINDS = ('product', 'technical')
STATUSES = ('resolved', 'delegated', 'unresolved')
PLACEHOLDERS = {'n/a', 'na', 'none', 'tbd', 'todo', 'to be determined', 'not applicable', '-', '...', 'etc', 'etc.'}


def render(text, acceptance):
    """The canonical document: the specification followed by its acceptance criteria."""
    return text + '\n\nAcceptance criteria:\n' + '\n'.join('- ' + a for a in acceptance)


def encode(document):
    try:
        return document.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise ApiError('invalid_result', 'The specification is not valid UTF-8 text.') from exc


def fingerprint(document):
    """Approval digest. Legacy plans already store ``digest(body)``; keep one definition."""
    return digest(document)


def description(document):
    """Locator-free facts about a document, safe to compare across copies."""
    data = encode(document)
    return {'digest': fingerprint(document), 'sha256': hash_bytes(data), 'bytes': len(data)}


def check_size(document, *, wrapper=''):
    """Never truncate: an oversized specification must be split by replanning."""
    length = len(document) + len(wrapper)
    if length > COMMENT_LIMIT:
        raise ApiError('specification_too_large',
                       f'The specification is {length} characters; a GitHub issue comment holds at most {COMMENT_LIMIT}. '
                       'It is never truncated or summarized for approval. Narrow the issue scope or split it into '
                       'smaller issues, then run fix-issue again.')


WORKFLOW_GUIDANCE = ' Restore the approved revision from its plan comment on the issue, or approve a new revision.'
COPY_GUIDANCE = (' This is a transport copy: restore the approved bytes, or remove a modified copy so that the next '
                 'fix-issue run copies it again from the verified workflow document.')


def read(path, expected, *, what='Specification', guidance=WORKFLOW_GUIDANCE):
    """Return the verified document text; the file must still hold the approved bytes."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ApiError('specification_missing', f'{what} file is missing: {path}.' + guidance, path=str(path)) from exc
    except OSError as exc:
        raise ApiError('specification_missing', f'{what} file cannot be read: {path}: {exc}.' + guidance, path=str(path)) from exc
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ApiError('specification_modified', f'{what} file is not valid UTF-8: {path}.' + guidance, path=str(path)) from exc
    if fingerprint(text) != expected['digest'] or ('sha256' in expected and hash_bytes(data) != expected['sha256']):
        raise ApiError('specification_modified',
                       f'{what} file no longer matches the approved digest {expected["digest"][:12]}: {path}.' + guidance,
                       path=str(path))
    return text


def write(path, document, *, what='Specification', guidance=WORKFLOW_GUIDANCE):
    """Store ``document`` once; an intact existing copy is retained untouched."""
    path = Path(path)
    expected = description(document)
    try:
        if read(path, expected, what=what, guidance=guidance) == document:
            return expected
    except ApiError as exc:
        if exc.code not in ('specification_missing', 'specification_modified'):
            raise
    atomic_bytes(path, encode(document))
    path.chmod(0o400)
    read(path, expected, what=what, guidance=guidance)
    return expected


def transport(source, destination, expected, *, what='Specification copy'):
    """Byte-preserving copy; both ends are verified against the approved digest.

    A missing copy is recreated from the verified source. An existing copy that
    no longer holds the approved bytes is never overwritten: it stops execution.
    """
    read(source, expected, what='Specification')
    data = Path(source).read_bytes()
    destination = Path(destination)
    if destination.exists():
        read(destination, expected, what=what, guidance=COPY_GUIDANCE)
        return destination
    atomic_bytes(destination, data)
    destination.chmod(0o400)
    read(destination, expected, what=what, guidance=COPY_GUIDANCE)
    return destination


def locator(revision, path, expected, **extra):
    """Where one revision's document lives, with the facts needed to verify it."""
    return {'revision': revision, 'path': str(path), **expected, **extra}


def issue_item(state):
    return next((i for i in state['feedback'] if i['kind'] == 'issue_plan' and not i.get('missing')), None)


def handoff(session, state):
    """Verified approved document for harness execution and acceptance review.

    Owner items imported before canonical documents carry no locator: their exact
    approved ``body`` is materialized once, without strengthening or rewriting it.
    """
    item = issue_item(state)
    if item is None:
        raise ApiError('invalid_state', 'Issue execution requires its approved specification.')
    source, plan = item['source'], item['source']['plan']
    expected = description(plan['body'])
    if expected['digest'] != plan.get('digest', expected['digest']):
        raise ApiError('specification_modified', 'The saved approved plan no longer matches its approved digest; '
                       'approve a new revision.')
    where = source.get('specification')
    if where is None:
        path = Path(session.path) / 'specifications' / f'{source["workflow"]}-r{plan["revision"]}.md'
        write(path, plan['body'], what='Approved specification', guidance=COPY_GUIDANCE)
        where = source['specification'] = locator(plan['revision'], path, expected, origin='saved-plan')
        session.save(state)
    if where['digest'] != expected['digest'] or where['revision'] != plan['revision']:
        raise ApiError('specification_modified', 'The Owner specification copy belongs to a different revision or digest; '
                       'approve a new revision.')
    text = read(where['path'], where, what='Approved specification', guidance=COPY_GUIDANCE)
    return {'plan': plan, 'locator': where, 'text': text, 'path': where['path']}


def provenance(item):
    """An issue-plan item without its specification text: provenance, not requirements."""
    if item.get('kind') != 'issue_plan':
        return item
    plan = item['source']['plan']
    source = {k: v for k, v in item['source'].items() if k not in ('plan', 'specification', 'context')}
    source['plan'] = {k: plan[k] for k in ('revision', 'digest', 'published_at') if k in plan}
    return {k: v for k, v in item.items() if k not in ('comments', 'source', 'versions')} | {'source': source}


# ---- planner-response checks ---------------------------------------------------------------------------
def sections(text):
    """Level-two Markdown sections outside fenced code: [(title, body)]."""
    result, fence, current = [], None, None
    for line in text.splitlines():
        stripped = line.strip()
        marker = re.match(r'(`{3,}|~{3,})', stripped)
        if marker:
            if fence is None:
                fence = marker.group(1)[0]
            elif stripped.startswith(fence * 3):
                fence = None
        heading = None if fence or marker else re.fullmatch(r'##\s+(.+?)\s*#*', line)
        if heading:
            current = [heading.group(1).strip(), []]
            result.append(current)
        elif current is not None:
            current[1].append(line)
    return [(title, '\n'.join(body).strip()) for title, body in result]


def _filler(body):
    return not body or body.strip(' .*_`').lower() in PLACEHOLDERS


def check_document(text, decisions):
    """Raise ``invalid_result`` unless the specification meets the mechanical standard."""
    found = {}
    for title, body in sections(text):
        key = title.lower()
        if key in found:
            raise ApiError('invalid_result', f'Specification repeats the section "{title}".')
        if _filler(body):
            raise ApiError('invalid_result', f'Specification section "{title}" is empty or filler; omit sections that do not apply.')
        found[key] = body
    missing = [s for s in REQUIRED_SECTIONS if s.lower() not in found]
    if missing:
        raise ApiError('invalid_result', 'Specification lacks required section(s): ' + ', '.join(missing) + '.')
    needed = []
    if any(d['kind'] == 'product' and d['status'] == 'resolved' for d in decisions):
        needed.append('Assumptions and resolved choices')
    if any(d['kind'] == 'technical' and d['status'] == 'resolved' for d in decisions):
        needed.append('Design decisions')
    absent = [s for s in needed if s.lower() not in found]
    if absent:
        raise ApiError('invalid_result', 'Resolved decisions must be recorded in section(s): ' + ', '.join(absent) + '.')
    # Heuristics against the shapes the standard forbids; they are not a quality judgement.
    if re.search(r'^\s*[-*]\s+\[[ xX]\]', text, re.M) or re.search(r'^#{1,6}\s*(step|task|phase)\s*\d+\b', text, re.M | re.I):
        raise ApiError('invalid_result', 'Specification must state outcomes, not a checklist or ledger of tasks.')
    if re.search(r'^(@@ -\d+|diff --git |\+\+\+ b/|--- a/)', text, re.M):
        raise ApiError('invalid_result', 'Specification must not prescribe edits as diffs; the harness chooses the edits.')


def check_decisions(value):
    decisions = value.get('decisions')
    if not isinstance(decisions, list):
        raise ApiError('invalid_result', 'Invalid decisions')
    for item in decisions:
        if (not isinstance(item, dict) or set(item) != {'topic', 'kind', 'status', 'detail'}
                or any(not isinstance(item[k], str) or not item[k].strip() for k in ('topic', 'detail'))
                or item['kind'] not in KINDS or item['status'] not in STATUSES):
            raise ApiError('invalid_result', 'Invalid decision entry.')
        if item['kind'] == 'technical' and item['status'] == 'unresolved':
            raise ApiError('invalid_result', 'Technical choices belong to the harness: resolve or delegate them, do not ask.')
        if item['kind'] == 'product' and item['status'] == 'delegated':
            raise ApiError('invalid_result', 'Product choices cannot be delegated to the harness; resolve or ask.')
    return decisions


def check_inspected(value, cwd):
    inspected = value.get('inspected')
    if not isinstance(inspected, list):
        raise ApiError('invalid_result', 'Invalid inspected')
    root = Path(cwd).resolve()
    for item in inspected:
        if (not isinstance(item, dict) or set(item) != {'path', 'finding'}
                or any(not isinstance(item[k], str) or not item[k].strip() for k in item)):
            raise ApiError('invalid_result', 'Invalid inspected entry.')
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts or not (root / relative).resolve().is_relative_to(root) \
                or not (root / relative).exists():
            raise ApiError('invalid_result', f'Inspected path does not exist in the repository: {item["path"]}')
    return inspected


def check_assessment(value, cwd):
    """Reject malformed, incomplete or self-contradictory assessments (mechanical only)."""
    decision = value['decision']
    questions, acceptance = value['questions'], value['acceptance']
    plan = value.get('plan', '')
    if not isinstance(plan, str):
        raise ApiError('invalid_result', 'Invalid plan')
    value.setdefault('inspected', [])
    value.setdefault('decisions', [])
    decisions = check_decisions(value)
    inspected = check_inspected(value, cwd)
    unresolved = [d for d in decisions if d['status'] == 'unresolved']
    if decision == 'plan':
        if not plan.strip() or not acceptance:
            raise ApiError('invalid_result', 'Empty questions or plan.')
        if questions or unresolved:
            raise ApiError('invalid_result', 'A plan cannot also ask questions or leave a product choice unresolved; '
                           'return questions until the product choices are settled.')
        if not inspected:
            raise ApiError('invalid_result', 'A plan must cite the existing repository behavior it inspected.')
        check_document(plan, decisions)
        document = render(plan, acceptance)
        encode(document)
        if len(document) > DOCUMENT_LIMIT:
            raise ApiError('invalid_result', f'The specification exceeds {DOCUMENT_LIMIT} characters; narrow its scope.')
    elif decision == 'questions':
        if not questions:
            raise ApiError('invalid_result', 'Empty questions or plan.')
        if plan.strip() or acceptance:
            raise ApiError('invalid_result', 'Questions cannot be combined with a plan or acceptance criteria.')
        if not unresolved:
            raise ApiError('invalid_result', 'Questions must correspond to an unresolved product decision.')
        if not inspected:
            raise ApiError('invalid_result', 'Questions must cite the repository inspection that could not answer them.')
    else:
        if questions or unresolved or plan.strip() and decision != 'continue' or acceptance and decision != 'continue':
            raise ApiError('invalid_result', f'A {decision} result cannot contain questions or a plan.')
    return value
