"""GitHub publication encoding and native receipt interpretation.

No retries, journal writes or authorization decisions belong here.
"""
import copy
import json
import uuid
from .common import ApiError, command

def gh(session, endpoint, payload=None, *, pages=False, resource="pulls"):
    host, repo = session.manifest["repository"].split("/", 1)
    args = ["gh", "api", "--hostname", host]
    if pages:
        args += ["--paginate", "--slurp"]
    if payload is not None:
        args += ["-X", "POST", "--input", "-"]
    if resource not in ("pulls", "issues"):
        raise ApiError("invalid_request", "Invalid GitHub comment resource.")
    result = command([*args, f"repos/{repo}/{resource}/{session.manifest['pr']}/{endpoint}"],
                     input_text=json.dumps(payload) if payload is not None else None)
    try:
        value = json.loads(result.stdout)
        if pages:
            if not isinstance(value, list) or any(not isinstance(p, list) for p in value):
                raise ValueError("Invalid pagination")
            value = [item for page in value for item in page]
        return value
    except ValueError as exc:
        raise ApiError("invalid_response", "GitHub returned incomplete publication data; resolve before retrying.") from exc


def marker(session, identity):
    return f"<!-- pr-review generation={session.id} head={session.git.head} action={identity} -->"


def with_marker(body, value):
    return body + "\n\n" + value


def make_actions(session, state):
    from .suggestions import render
    selected = [c for c in state["comments"] if c["status"] == "staged"]
    actions = []
    review = [c for c in selected if c["placement"]["kind"] in ("inline", "review")]
    overall = [c for c in review if c["placement"]["kind"] == "review"]
    if len(overall) > 1:
        raise ApiError("multiple_assessments", "Stage one overall assessment per review; inline comments remain separate.")
    for c in selected:
        if c["placement"]["kind"] != "reply":
            continue
        identity = "api-" + uuid.uuid4().hex
        actions.append({"id": identity, "kind": "reply", "status": "draft", "comment_ids": [c["id"]],
                        "finding_ids": c["finding_ids"], "target": copy.deepcopy(c["placement"]), "event": "COMMENT",
                        "marker": marker(session, identity), "viewer": session.manifest["viewer"],
                        "payload": {"body": with_marker(c["body"], marker(session, identity))}})
    # Staged comments use COMMENT unless the user explicitly chose a verdict.
    # Sending a verdict on its own also requires an explicit user choice.
    if review or (state.get("verdict_requested") and not state["verdict_submitted"]):
        identity = "api-" + uuid.uuid4().hex
        comment_markers = {c["id"]: marker(session, identity + "/" + c["id"]) for c in review}
        payload = {"commit_id": session.git.head,
                   "event": state["verdict"] if state.get("verdict_requested") and not state["verdict_submitted"] else "COMMENT",
                   "body": with_marker(overall[0]["body"] if overall else "", marker(session, identity))}
        inline = []
        for c in review:
            if c["placement"]["kind"] != "inline":
                continue
            a = session.git.anchor(c["placement"])
            value = {"path": a["path"], "line": a["line"], "side": a["side"],
                     "body": with_marker(render(c), comment_markers[c["id"]])}
            if a["start_line"] != a["line"]:
                value.update(start_line=a["start_line"], start_side=a["side"])
            inline.append(value)
        if inline:
            payload["comments"] = inline
        actions.append({"id": identity, "kind": "review", "status": "draft", "comment_ids": [c["id"] for c in review],
                        "finding_ids": [f for c in review for f in c["finding_ids"]], "target": None,
                        "event": payload["event"], "marker": marker(session, identity), "comment_markers": comment_markers,
                        "viewer": session.manifest["viewer"], "payload": payload})
    return actions


def valid_receipt(session, action, value):
    if (not isinstance(value, dict) or type(value.get("id")) is not int
            or not isinstance(value.get('user'), dict)
            or not isinstance(value['user'].get('login'), str)
            or not isinstance(value.get('body'), str)):
        return False
    if str((value.get("user") or {}).get("login", "")).lower() != str(action["viewer"]).lower():
        return False
    if action["marker"] not in (value.get("body") or ""):
        return False
    if action["kind"] == "timeline":
        url = value.get("html_url")
        expected = f"https://{session.manifest['repository']}/pull/{session.manifest['pr']}#issuecomment-{value['id']}"
        return isinstance(url, str) and url.lower() == expected.lower()
    if action["kind"] == "reply":
        return value.get("in_reply_to_id") == action["target"]["root_id"]
    expected = {"COMMENT": "COMMENTED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "APPROVE": "APPROVED"}[action["event"]]
    return value.get("commit_id") == session.git.head and value.get("state", "").upper() == expected


class GitHubPublication:
    verdicts = frozenset(('COMMENT', 'APPROVE', 'REQUEST_CHANGES'))
    placements = frozenset(('review', 'inline', 'reply'))
    suggestions = True

    def validate_verdict(self, change, viewer, verdict):
        if verdict not in self.verdicts:
            raise ApiError('verdict_unavailable', 'Unsupported verdict.')
        if verdict != 'COMMENT' and change['author'].lower() == viewer.lower():
            raise ApiError('operation_failed', 'GitHub does not allow self-approval or self-request-changes')

    def render(self, comment):
        return render_comment(comment)

    def prepare(self, session, state):
        for comment in state['comments']:
            if comment['status'] == 'staged' and comment['placement']['kind'] not in self.placements:
                raise ApiError('unsupported_operation', 'Unsupported publication placement.')
        if state['verdict'] not in self.verdicts:
            raise ApiError('verdict_unavailable', 'Unsupported publication verdict.')
        actions = make_actions(session, state)
        for action in actions:
            action['operations'] = [dict(version=1, **copy.deepcopy(action))]
        return actions

    def retry_equivalent(self, previous, action):
        import re
        def content(value):
            operations = [{key: op.get(key) for key in ('version', 'kind', 'target', 'event', 'comment_ids', 'payload')}
                          for op in value.get('operations', [{'version': 1, **value}])]
            return re.sub(r'<!-- pr-review [^>]+ -->', '', json.dumps(operations, sort_keys=True))
        return content(previous) == content(action)

    def submit(self, session, operation):
        if operation.get('version', 1) != 1:
            raise ApiError('unsupported_operation', 'Unsupported saved publication operation version.')
        from .publication import gh as transport
        endpoint = f"comments/{operation['target']['root_id']}/replies" if operation.get('kind') == 'reply' else 'reviews'
        return transport(session, endpoint, operation['payload'])

    def matches(self, session, operation, receipt):
        return valid_receipt(session, operation, receipt)

    def lookup(self, session, operation):
        if operation.get('version', 1) != 1:
            raise ApiError('unsupported_operation', 'Unsupported saved publication operation version.')
        from .publication import gh as transport
        values = transport(session, 'comments' if operation.get('kind') == 'reply' else 'reviews', pages=True)
        if any(not isinstance(v, dict) or type(v.get('id')) is not int or
               not isinstance(v.get('user'), dict) or not isinstance(v['user'].get('login'), str) or
               not isinstance(v.get('body'), str) for v in values):
            raise ApiError('invalid_response', 'Malformed publication lookup; refusing to risk a duplicate')
        return [v for v in values if self.matches(session, operation, v)]

    def comments(self, session, operation, receipt, state):
        from .publication import gh as transport
        result = {}
        if operation['kind'] != 'review' or not operation['payload'].get('comments'):
            return result
        values = transport(session, f"reviews/{receipt['id']}/comments", pages=True)
        if any(not isinstance(v, dict) or type(v.get('id')) is not int or not isinstance(v.get('user'), dict) or not isinstance(v['user'].get('login'), str) or not isinstance(v.get('body'), str) for v in values):
            raise ApiError('incomplete_receipts', 'Review submitted; inline receipts are incomplete.')
        for identity, marker in operation.get('comment_markers', {}).items():
            matches = [v for v in values if marker in v['body'] and v.get('pull_request_review_id') == receipt['id'] and str(v['user'].get('login', '')).lower() == operation['viewer'].lower()]
            if len(matches) > 1:
                raise ApiError('duplicate_receipts', 'Multiple inline receipts match one comment.')
            if matches:
                result[identity] = matches[0]
        expected = [c['id'] for c in state['comments'] if c['id'] in operation['comment_ids'] and c['placement']['kind'] == 'inline']
        if any(i not in result for i in expected):
            raise ApiError('incomplete_receipts', 'Review submitted; waiting for inline receipts. No retry sent.')
        return result

    def evidence(self, operation):
        receipt = operation.get('result') or {}
        comments = [v.get('id') for v in operation.get('comment_results', {}).values() if v.get('pull_request_review_id') is not None]
        reviews = []
        if operation.get('kind') == 'reply':
            comments.append(receipt.get('id'))
            reviews.append(receipt.get('pull_request_review_id'))
        elif operation.get('kind') == 'review':
            reviews.append(receipt.get('id'))
        return dict(url=receipt.get('html_url'), comment_ids=[x for x in comments if x is not None],
                    review_ids=[x for x in reviews if x is not None])

    def receipt_url(self, receipt):
        return receipt.get('html_url')


def render_comment(comment):
    suggestion = comment.get('suggestion')
    if not suggestion:
        return comment['body']
    return comment['body'] + '\n\n```suggestion\n' + suggestion['replacement'] + '```\n'
