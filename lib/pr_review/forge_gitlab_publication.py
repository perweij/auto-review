"""GitLab COMMENT reviews as journaled timeline notes and diff discussions."""
import copy
import hashlib
import re
import uuid

from .common import ApiError, command
from .forge_gitlab_notes import canonical_body, context, post


def render_body(body):
    """Keep literal code; neutralize slash directives before exact preview."""
    body = canonical_body(body)
    protected, fence, html, start, offset = [], None, None, 0, 0
    lines = body.splitlines(keepends=True)
    for line in lines:
        if html:
            end, literal = html
            if (re.search(end, line, re.I) if end else not line.strip()):
                if literal:
                    protected.append((start, offset + len(line)))
                html = None
            offset += len(line)
            continue
        if fence is None:
            tag = re.match(r'^ {0,3}<(pre|script|style|textarea)(?=[ \t>]|$)', line, re.I)
            text = re.sub(r'^ {0,3}', '', line)
            if tag:
                html = (r'</' + tag[1] + r'\s*>', True)
            elif text.startswith('<!--'):
                html = ('-->', True)
            elif text.startswith('<?'):
                html = (r'\?>', True)
            elif text.startswith('<![CDATA['):
                html = (r'\]\]>', True)
            elif re.match(r'<![A-Z]', text):
                html = ('>', True)
            elif re.match(r'^ {0,3}</?[A-Za-z][A-Za-z0-9-]*(?=[ \t/>]|$)', line):
                # Raw HTML may contain literal fences. Suppress fence recognition
                # until a blank line; ambiguous HTML is not a directive exemption.
                html = (None, False)
            if html:
                start = offset
                end, literal = html
                if end and re.search(end, line, re.I):
                    if literal:
                        protected.append((start, offset + len(line)))
                    html = None
                offset += len(line)
                continue
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)', line)
        if match:
            run, rest = match.groups()
            if fence is None and not (run[0] == '`' and '`' in rest):
                fence, start = run, offset
            elif fence and run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                protected.append((start, offset + len(line)))
                fence = None
        offset += len(line)
    if fence or (html and html[1]):
        protected.append((start, len(body)))
    def inside(offset):
        return any(start <= offset < end for start, end in protected)
    def code_spans(text, offset):
        runs = list(re.finditer(r'`+', text))
        i = 0
        while i < len(runs):
            closing = next((j for j in range(i+1, len(runs)) if len(runs[j][0]) == len(runs[i][0])), None)
            if closing is None:
                i += 1  # Unmatched ticks are prose, not a code exemption.
            else:
                protected.append((offset + runs[i].start(), offset + runs[closing].end()))
                i = closing + 1
    # Inline code cannot cross paragraphs or block boundaries. Conservatively
    # break at Markdown extension constructs as well as CommonMark block starts.
    paragraph, beginning, offset = [], 0, 0
    boundary = re.compile(r'^(?: {4}|\t| {0,3}(?:[#><|!\[$\\]|[-+*](?:[ \t]|$)|[0-9]+[.)][ \t]|[=-]+[ \t]*$|`{3,}|~{3,}))')
    for line in lines:
        if not line.strip() or inside(offset) or boundary.match(line):
            code_spans(''.join(paragraph), beginning)
            paragraph = []
        else:
            if not paragraph:
                beginning = offset
            paragraph.append(line)
        offset += len(line)
    code_spans(''.join(paragraph), beginning)
    result, offset = [], 0
    for line in lines:
        if re.match(r'^/\S', line) and not inside(offset):
            line = '/\u200b' + line[1:]
            result.append(line)
            offset += len(line) - 1
        else:
            result.append(line)
            offset += len(line)
    return ''.join(result)


def position(session, anchor, refs):
    """Derive native positions, including context pairs and range cursor codes."""
    anchor = session.git.anchor(anchor)
    entry = session.git.diff(anchor['path'])
    key = 'old' if anchor['side'] == 'LEFT' else 'new'
    endpoints = {}
    for hunk in entry['hunks']:
        match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)', hunk['header'])
        old, new = int(match[1]), int(match[2])
        for row in hunk['rows']:
            if row[key] in (anchor['start_line'], anchor['line']) and (key != 'old' or row['kind'] == '-'):
                endpoints[row[key]] = dict(old_line=row['old'], new_line=row['new'],
                    type='old' if row['kind'] == '-' else 'new' if row['kind'] == '+' else None,
                    line_code=hashlib.sha1(entry['path'].encode()).hexdigest() + f'_{old}_{new}')
            old += row['kind'] != '+'
            new += row['kind'] != '-'
    end = endpoints[anchor['line']]
    value = dict(refs, position_type='text', old_path=entry['old_path'], new_path=entry['path'])
    value.update({k: end[k] for k in ('old_line', 'new_line') if end[k] is not None})
    if anchor['start_line'] != anchor['line']:
        value['line_range'] = {name: {k: v for k, v in endpoints[number].items() if v is not None}
                               for name, number in (('start', anchor['start_line']), ('end', anchor['line']))}
    return value


def root_note(receipt):
    if isinstance(receipt, dict) and isinstance(receipt.get('notes'), list) and receipt['notes']:
        return receipt['notes'][0]
    return receipt


class GitLabPublication:
    verdicts = frozenset(('COMMENT',))
    placements = frozenset(('review', 'inline'))
    suggestions = False
    requires_absence_proof = True

    def __init__(self, forge):
        self.forge = forge

    def prepare_owner_reply(self, session, reply, viewer, identity):
        from .common import digest
        repo, actor, destination, _ = context(self.forge, session, command)
        if actor['username'].lower() != viewer.lower():
            raise ApiError('account_changed', 'GitLab account changed before reply preparation.')
        target = reply['target']
        item = session.items(session.load(), [reply['source']])[0]
        if target != item.get('reply_target') or not item.get('can_reply'):
            raise ApiError('permission_denied', 'The native GitLab reply destination is unavailable.')
        marker = f'<!-- pr-owner session={digest(str(session.path))[:24]} action={identity} -->'
        return dict(version=1, forge='gitlab', id=identity, reply=reply['id'], source=reply['source'],
                    kind='reply', target=copy.deepcopy(target), viewer=viewer, marker=marker,
                    destination=destination, actor=actor, body=reply['body'], status='draft',
                    creates_thread=item['kind'] == 'comment',
                    payload={'body': canonical_body(render_body(reply['body']) + '\n\n' + marker)})

    @staticmethod
    def owner_body(operation):
        return operation['body']

    @staticmethod
    def owner_receipt(receipt):
        note = root_note(receipt)
        return dict(id=note.get('id'), author=(note.get('author') or {}).get('username', ''),
                    body=note.get('body'), url=receipt.get('html_url'))

    @staticmethod
    def owner_preview(operations):
        rows = []
        for op in operations:
            body, suffix = op['payload']['body'], '\n\n' + op['marker']
            if not body.endswith(suffix):
                raise ApiError('invalid_state', 'Prepared reply is missing its exact tracking suffix.')
            rows.append(dict(operation_id=op['id'], kind='reply',
                             destination={**copy.deepcopy(op['destination']), **copy.deepcopy(op['target'])},
                             body=body[:-len(suffix)], marker_omitted=True,
                             creates_thread=op.get('creates_thread', False)))
        return dict(operations=rows)

    def owner_bound(self, session, op):
        if op.get('version') != 1 or op.get('forge') != 'gitlab' or op.get('kind') != 'reply':
            raise ApiError('unsupported_operation', 'Unsupported saved GitLab Owner operation.')
        repo, actor, destination, raw = context(self.forge, session, command)
        if actor != op['actor'] or destination != op['destination']:
            raise ApiError('account_changed', 'Saved reply account or MR identity changed; no write was sent.')
        return repo, raw, f"projects/{destination['project_id']}/merge_requests/{destination['iid']}"

    def lookup_owner(self, session, operations):
        # One complete native listing, even when several actions need recovery.
        if not operations:
            return {}
        repo, _, endpoint = self.owner_bound(session, operations[0])
        for op in operations[1:]:
            if (op.get('forge'), op.get('version'), op.get('kind'), op.get('actor'), op.get('destination')) != (
                    'gitlab', 1, 'reply', operations[0]['actor'], operations[0]['destination']):
                raise ApiError('invalid_state', 'Owner journal contains conflicting destinations or accounts.')
        discussions = self.forge.pages(repo, endpoint + '/discussions')
        values = []
        for discussion in discussions:
            if (not isinstance(discussion.get('id'), str) or not isinstance(discussion.get('notes'), list)
                    or not discussion['notes'] or any(not self.valid_note(n) for n in discussion['notes'])):
                raise ApiError('incomplete_receipts', 'Incomplete GitLab reply lookup; no retry sent.')
            for note in discussion['notes']:
                values.append(dict(id=discussion['id'], root_id=discussion['notes'][0]['id'], notes=[note]))
        result = {}
        for op in operations:
            confirmed = root_note(op.get('receipt') or {})
            receipt_id = confirmed.get('id') if isinstance(confirmed, dict) else None
            result[op['id']] = [self.annotate(op, v) for v in values
                                if op['marker'] in root_note(v)['body'] or
                                (receipt_id is not None and root_note(v)['id'] == receipt_id)]
        return result

    def submit_owner(self, session, op):
        from urllib.parse import quote
        try:
            repo, raw, endpoint = self.owner_bound(session, op)
            state = session.load()
            if raw.get('state') != 'opened' or raw.get('sha') != state['change']['source']['head']:
                raise ApiError('stale_publication', 'The MR is no longer open at the confirmed head.')
            discussion, _ = self.forge.owner_discussion(repo, op['actor'])
            item = next((i for i in discussion['items'] if i['key'] == op['source']), None)
            if not item or item.get('reply_target') != op['target'] or not item.get('can_reply'):
                raise ApiError('permission_denied', 'The native GitLab reply destination is unavailable.')
            from .owner_state import feedback_fingerprint
            saved = session.items(state, [op['source']])[0]
            if feedback_fingerprint(item, state, state['viewer']) != saved['fingerprint']:
                raise ApiError('stale_discussion', 'GitLab feedback changed before reply submission.')
        except (ApiError, OSError) as exc:
            exc.write_not_sent = True
            raise
        note = post(repo, endpoint + '/discussions/' + quote(op['target']['discussion_id'], safe='') + '/notes', op['payload'], command)
        return self.annotate(op, dict(id=op['target']['discussion_id'], root_id=op['target']['root_id'], notes=[note]))

    def render(self, comment):
        return render_body(comment['body'])

    @staticmethod
    def preview(actions):
        """Project only frozen, remaining writes, including reused failed actions."""
        rows = []
        for action in actions:
            for op in action['operations']:
                if op['status'] == 'published' or op.get('receipt_verified'):
                    continue
                body = op['payload']['body']
                suffix = '\n\n' + op['marker']
                if not body.endswith(suffix):
                    raise ApiError('invalid_state', 'Prepared operation is missing its exact tracking suffix.')
                rows.append(dict(operation_id=op['id'], kind='inline' if op['kind'] == 'discussion' else 'review',
                                 destination=copy.deepcopy(op['destination']), body=body[:-len(suffix)],
                                 position=copy.deepcopy(op['payload'].get('position')), marker_omitted=True))
        return {'operations': rows}

    def validate_verdict(self, change, viewer, verdict):
        if verdict not in self.verdicts:
            raise ApiError('verdict_unavailable', 'GitLab publication supports COMMENT only; approvals and request-changes are unavailable.')

    def prepare(self, session, state):
        self.validate_verdict({}, '', state['verdict'])
        selected = [c for c in state['comments'] if c['status'] == 'staged']
        for comment in selected:
            if comment['placement']['kind'] not in self.placements or comment.get('suggestion'):
                raise ApiError('unsupported_operation', 'GitLab supports overall and inline comments, without replies or code suggestions.')
        if not selected:
            return []
        repo, actor, destination, raw = context(self.forge, session, command)
        self.fresh(session, raw)
        version = None
        if any(c['placement']['kind'] == 'inline' for c in selected):
            versions = self.forge.pages(repo, self.forge.endpoint(repo) + '/versions')
            version = next((v for v in versions if self.version_matches(session, v)), None)
            if version is None:
                raise ApiError('invalid_anchor', 'No collected GitLab diff version matches the reviewed head and merge base. Refresh the review.')
        identity = 'api-' + uuid.uuid4().hex
        operations = []
        for comment in sorted(selected, key=lambda c: c['placement']['kind'] != 'review'):
            op_id = 'api-' + uuid.uuid4().hex
            marker = '<!-- pr-review action=' + op_id + ' -->'
            payload = {'body': canonical_body(self.render(comment) + '\n\n' + marker)}
            inline = comment['placement']['kind'] == 'inline'
            if inline:
                refs = dict(base_sha=version['base_commit_sha'], start_sha=version['start_commit_sha'], head_sha=version['head_commit_sha'])
                payload['position'] = position(session, comment['placement'], refs)
            operations.append(dict(version=1, forge='gitlab', id=op_id, kind='discussion' if inline else 'timeline',
                status='draft', event='COMMENT', comment_ids=[comment['id']], payload=payload, marker=marker,
                destination=copy.deepcopy(destination), actor=copy.deepcopy(actor), diff_version=version['id'] if inline else None))
        return [dict(id=identity, kind='review', status='draft', event='COMMENT', target=None,
                     comment_ids=[c['id'] for c in selected], finding_ids=[f for c in selected for f in c.get('finding_ids', [])],
                     operations=operations)]

    @staticmethod
    def fresh(session, raw):
        if raw.get('state') != 'opened' or raw.get('sha') != session.git.head:
            raise ApiError('stale_publication', 'The MR is no longer open at the reviewed head; refresh the review before publishing.')

    @staticmethod
    def version_matches(session, value):
        return (isinstance(value, dict) and type(value.get('id')) is int and value.get('state') == 'collected'
                and value.get('head_commit_sha') == session.git.head and value.get('base_commit_sha') == session.git.base
                and isinstance(value.get('start_commit_sha'), str) and re.fullmatch(r'[0-9a-f]{40,64}', value['start_commit_sha']))

    def bound(self, session, op):
        if op.get('version') != 1 or op.get('forge') != 'gitlab' or op.get('kind') not in ('timeline', 'discussion'):
            raise ApiError('unsupported_operation', 'Unsupported saved GitLab publication operation.')
        repo, actor, destination, raw = context(self.forge, session, command)
        if actor != op['actor'] or destination != op['destination']:
            raise ApiError('account_changed', 'Saved publication destination or account changed; no write was sent.')
        return repo, raw, f"projects/{destination['project_id']}/merge_requests/{destination['iid']}"

    @staticmethod
    def valid_note(note):
        return (isinstance(note, dict) and type(note.get('id')) is int and note['id'] > 0
                and isinstance(note.get('body'), str) and type(note.get('system')) is bool
                and isinstance(note.get('author'), dict) and type(note['author'].get('id')) is int
                and isinstance(note['author'].get('username'), str) and type(note.get('project_id')) is int
                and type(note.get('noteable_id')) is int and note.get('noteable_type') == 'MergeRequest')

    def candidate(self, op, receipt):
        note = root_note(receipt)
        dest = op['destination']
        return (self.valid_note(note) and not note['system'] and note['project_id'] == dest['project_id']
                and note['noteable_id'] == dest['mr_id'] and note.get('noteable_iid') in (None, dest['iid'])
                and str(note['author']['id']) == op['actor']['id'] and op['marker'] in note['body'])

    def matches(self, session, op, receipt):
        if not self.candidate(op, receipt):
            return False
        note = root_note(receipt)
        if op.get('kind') == 'reply':
            prior = root_note(op.get('receipt') or {})
            return (receipt.get('id') == op['target']['discussion_id']
                    and receipt.get('root_id') == op['target']['root_id']
                    and note['id'] != op['target']['root_id']
                    and (not prior.get('id') or prior['id'] == note['id'])
                    and canonical_body(note['body']) == op['payload']['body'])
        prior = root_note(op.get('result') or op.get('response'))
        if isinstance(prior, dict) and type(prior.get('id')) is int and note['id'] != prior['id']:
            return False
        if canonical_body(note['body']) != op['payload']['body']:
            return False
        if op['kind'] == 'discussion':
            if not isinstance(receipt.get('id'), str) or receipt.get('individual_note') is not False:
                return False
            actual = note.get('position') or {}
            expected = op['payload']['position']
            if any(actual.get(k) != v for k, v in expected.items() if k != 'line_range'):
                return False
            if 'line_range' in expected:
                for name, endpoint in expected['line_range'].items():
                    observed = (actual.get('line_range') or {}).get(name) or {}
                    if any(observed.get(k) != endpoint.get(k) for k in ('line_code', 'old_line', 'new_line')):
                        return False
                    if endpoint.get('type') and observed.get('type') != endpoint['type']:
                        return False
        elif note.get('position'):
            return False
        return True

    def annotate(self, op, receipt):
        receipt = copy.deepcopy(receipt)
        note = root_note(receipt)
        if isinstance(note, dict) and type(note.get('id')) is int:
            receipt['html_url'] = op['destination']['url'] + '#note_' + str(note['id'])
        return receipt

    def lookup(self, session, op):
        repo, _, endpoint = self.bound(session, op)
        suffix = '/discussions' if op['kind'] == 'discussion' else '/notes'
        values = self.forge.pages(repo, endpoint + suffix + '?order_by=created_at&sort=asc')
        if any(not self.valid_note(root_note(v)) for v in values):
            raise ApiError('incomplete_receipts', 'Malformed GitLab lookup; no retry sent.')
        return [self.annotate(op, v) for v in values if self.candidate(op, v)]

    def submit(self, session, op):
        if op.get('kind') == 'reply':
            return self.submit_owner(session, op)
        try:
            repo, raw, endpoint = self.bound(session, op)
            self.fresh(session, raw)
            if op['kind'] == 'discussion':
                version = self.forge.get(repo, endpoint + '/versions/' + str(op['diff_version']))
                refs = op['payload']['position']
                if (not self.version_matches(session, version)
                        or any(version[k + '_commit_sha'] != refs[k + '_sha'] for k in ('base', 'start', 'head'))):
                    raise ApiError('invalid_anchor', 'The confirmed GitLab diff version is no longer available; refresh the review.')
        except (ApiError, OSError) as exc:
            exc.write_not_sent = True
            raise
        result = post(repo, endpoint + ('/discussions' if op['kind'] == 'discussion' else '/notes'), op['payload'], command)
        return self.annotate(op, result)

    def comments(self, session, op, receipt, state):
        note = copy.deepcopy(root_note(receipt))
        note['html_url'] = receipt['html_url']
        return {identity: note for identity in op['comment_ids']}

    def evidence(self, op):
        receipt = op.get('result') or {}
        note = root_note(receipt)
        ids = [note['id']] if self.valid_note(note) else []
        return dict(url=receipt.get('html_url'), comment_ids=ids, review_ids=[],
                    timeline_ids=ids if op['kind'] == 'timeline' else [])

    def receipt_url(self, receipt):
        return receipt.get('html_url')

    def retry_equivalent(self, previous, action):
        def intent(value):
            result = []
            for original in value.get('operations', []):
                op = copy.deepcopy(original)
                body = op['payload']['body']
                suffix = '\n\n' + op['marker']
                op['payload']['body'] = body[:-len(suffix)] if body.endswith(suffix) else body
                if 'position' in op['payload']:
                    # A target advance may add another native version of the same diff.
                    # Reuse the frozen original operation rather than changing its refs.
                    op['payload']['position'].pop('start_sha', None)
                result.append({k: op.get(k) for k in ('kind', 'comment_ids', 'event', 'destination', 'actor', 'payload')})
            return result
        return intent(previous) == intent(action)
