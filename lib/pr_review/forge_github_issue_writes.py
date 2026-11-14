"""GitHub issue write payloads and receipt interpretation."""
import copy
import json
import re
from .common import ApiError, command, digest
from .forge_github_issues import pages

def comment_marker(workflow, key):
    return f'<!-- fix-issue workflow={workflow} action={digest(key)[:24]} -->'


class GitHubIssueWrites:
    capabilities = frozenset(("comment", "create", "draft", "acknowledge"))

    def api(self, writer, endpoint, payload, method='POST', *, timeout=None):
        response = command(['gh', 'api', '--hostname', writer.repo.host, '-X', method, endpoint, '--input', '-'],
                           cwd=writer.repo.cwd, input_text=json.dumps(payload), timeout=timeout)
        try:
            value = json.loads(response.stdout)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except ValueError as exc:
            raise ApiError('invalid_response', 'Malformed issue operation response; inspect before retrying.') from exc

    def comment_text(self, writer, body, key):
        return body + '\n\n' + comment_marker(writer.state["id"], key)

    def readback(self, writer, key, action):
        """Inspect one saved intent without sending or changing its journal."""
        receipt = action.get('receipt', {})
        if not isinstance(receipt, dict):
            return {'outcome': 'changed', 'candidates': [], 'matches': []}
        if key == 'create-pr':
            marker = '<!-- fix-issue workflow=' + writer.state['id'] + ' -->'
            candidates = pages(writer.repo, f'repos/{writer.repo.name}/pulls?state=all&head='
                               + writer.repo.name.split('/')[0] + ':' + writer.state['remote_branch'])
            self.validate_records(candidates, creation=True)
            found = [p for p in candidates if marker in (p.get('body') or '')
                     and (p.get('base') or {}).get('ref') == writer.state['base_branch']
                     and (p.get('head') or {}).get('ref') == writer.state['remote_branch']
                     and ((p.get('head') or {}).get('repo') or {}).get('full_name', '').lower() == writer.repo.name
                     and (p.get('user') or {}).get('login', '').lower() == writer.state['viewer'].lower()
                     and p.get('body') == action.get('payload', {}).get('body', p.get('body'))]
        elif 'body' in action:
            text = writer.comment_text(action['body'], key)
            marker = text.rsplit('\n\n', 1)[1]
            comments = pages(writer.repo, f'repos/{writer.repo.name}/issues/{writer.state["issue"]}/comments')
            self.validate_records(comments)
            candidates = [c for c in comments if marker in (c.get('body') or '') or c.get('id') == receipt.get('id')]
            found = [c for c in candidates if c.get('body') == text
                     and (c.get('user') or {}).get('login', '').lower() == writer.state['viewer'].lower()]
        else:
            raise ApiError('invalid_request', 'Only comment and PR-creation intents support this recovery operation.')
        if len(found) == len(candidates) == 1 and (not receipt or found[0].get('id') == receipt.get('id')):
            outcome = 'accepted'
        elif candidates or receipt or action['status'] == 'complete':
            outcome = 'ambiguous' if len(candidates) > 1 else 'changed'
        else:
            outcome = 'absent'
        return {'outcome': outcome, 'candidates': candidates, 'matches': found}

    def plan_comment_url(self, writer):
        key = 'plan:' + str(writer.state['plan']['revision'])
        action = writer.state.get('actions', {}).get(key, {})
        receipt = action.get('receipt', {})
        if not isinstance(receipt, dict):
            raise ApiError('facts_incomplete', 'Malformed saved plan receipt.')
        comment_id = receipt.get('id')
        if (action.get('status') != 'complete' or type(comment_id) is not int or comment_id <= 0
                or 'body' not in action or receipt.get('body') != writer.comment_text(action['body'], key)
                or (receipt.get('user') or {}).get('login', '').lower() != writer.state['viewer'].lower()):
            raise ApiError('facts_incomplete', 'A confirmed current plan comment is required before PR creation.')
        # Build from trusted repository identity and a numeric receipt, never a
        # model-written URL or prose that could add closing directives.
        return f'https://{writer.repo.identity}/issues/{writer.state["issue"]}#issuecomment-{comment_id}'

    def prepare_comment(self, writer, body, key):
        return dict(version=1, repository=writer.repo.identity, viewer=writer.state['viewer'], account=copy.deepcopy(writer.state.get('forge_account')), endpoint=f'repos/{writer.repo.name}/issues/{writer.state["issue"]}/comments',
                    payload={'body': self.comment_text(writer, body, key)})

    def prepare_creation(self, writer, action):
        state = writer.state
        body = f'Closes #{state["issue"]}\n\nPlan: {self.plan_comment_url(writer)}\n\n<!-- fix-issue workflow={state["id"]} -->'
        payload = copy.deepcopy(action.get('payload', {'title': state['title'], 'head': state['remote_branch'], 'base': state['base_branch'], 'draft': True}))
        if payload.get('head') != state['remote_branch'] or payload.get('base') != state['base_branch'] or payload.get('draft') is not True:
            raise ApiError('action_changed', 'Saved creation destination or draft state changed.')
        payload['body'] = body
        return dict(version=1, repository=writer.repo.identity, viewer=writer.state['viewer'], account=copy.deepcopy(writer.state.get('forge_account')), endpoint=f'repos/{writer.repo.name}/pulls', payload=payload)

    def validate(self, writer, operation):
        if operation.get('version') != 1:
            raise ApiError('unsupported_operation', 'Unsupported issue operation version.')
        if operation.get('repository') != writer.repo.identity or operation.get('viewer') != writer.state['viewer'] or operation.get('account') != writer.state.get('forge_account'):
            raise ApiError('action_changed', 'Saved issue operation identity changed.')
        if operation.get('endpoint') not in (f'repos/{writer.repo.name}/issues/{writer.state["issue"]}/comments', f'repos/{writer.repo.name}/pulls'):
            raise ApiError('action_changed', 'Saved publication destination changed.')

    def submit(self, writer, operation):
        self.validate(writer, operation)
        try:
            return writer.api(operation['endpoint'], operation['payload'])
        except ApiError as exc:
            if re.search(r'HTTP (401|403|404|422)\b', str(exc)):
                raise ApiError('publication_rejected', str(exc)) from exc
            raise

    def draft_state(self, writer):
        data = writer.api('graphql', {'query': 'query($owner:String!,$name:String!,$n:Int!){repository(owner:$owner,name:$name){pullRequest(number:$n){id isDraft state}}}',
            'variables': {'owner': writer.repo.name.split('/')[0], 'name': writer.repo.name.split('/')[1], 'n': writer.state['pr']}})
        try:
            pr = data['data']['repository']['pullRequest']
            if data.get('errors') or pr['state'] != 'OPEN' or type(pr['isDraft']) is not bool or not pr['id']:
                raise ValueError()
            return dict(id=pr['id'], draft=pr['isDraft'])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError('facts_incomplete', 'Cannot verify draft status.') from exc

    def set_draft(self, writer, identity, desired):
        verb = 'convertPullRequestToDraft' if desired else 'markPullRequestReadyForReview'
        query = 'mutation($id:ID!){' + verb + '(input:{pullRequestId:$id}){pullRequest{isDraft}}}'
        writer.api('graphql', {'query': query, 'variables': {'id': identity}})

    def acknowledge(self, writer, comment):
        result = writer.api(f'repos/{writer.repo.name}/issues/comments/{comment["id"]}/reactions', {'content': 'eyes'}, timeout=10)
        if not isinstance(result, dict) or result.get('content') != 'eyes' or not result.get('id'):
            raise ApiError('reaction_failed', 'Invalid reaction receipt.')
        return result

    def feedback(self, repo, number):
        return [pages(repo, f'repos/{repo.name}/{endpoint}') for endpoint in
                (f'issues/{number}/comments', f'pulls/{number}/comments', f'pulls/{number}/reviews')]

    def change_number(self, receipt):
        return receipt['number']

    @staticmethod
    def semantic_comment(comment):
        """Keep discussion content and review anchors, excluding GitHub metadata."""
        result = {'id': comment.get('id'), 'user': {'login': (comment.get('user') or {}).get('login', '')},
                  'body': comment.get('body') or ''}
        for key in ('path', 'line', 'side', 'start_line', 'start_side', 'original_line', 'original_start_line',
                    'position', 'original_position', 'subject_type', 'commit_id', 'original_commit_id',
                    'in_reply_to_id', 'pull_request_review_id', 'diff_hunk', 'state'):
            if key in comment:
                result[key] = comment[key]
        return result


    @staticmethod
    def matches_receipt(comment, receipt):
        if not isinstance(receipt, dict):
            raise ApiError('invalid_session', 'Malformed saved issue receipt; inspect the write journal.')
        return (comment.get('id') == receipt.get('id') and comment.get('body') == receipt.get('body')
                and (comment.get('user') or {}).get('login', '').lower() == (receipt.get('user') or {}).get('login', '').lower())


    def validate_records(self, values, creation=False):
        if not isinstance(values, list):
            raise ApiError('facts_incomplete', 'Incomplete write lookup.')
        for value in values:
            if (not isinstance(value, dict) or type(value.get('id')) is not int
                    or value.get('body') is not None and not isinstance(value['body'], str)
                    or value.get('user') is not None and (not isinstance(value['user'], dict)
                        or not isinstance(value['user'].get('login'), str))):
                raise ApiError('facts_incomplete', 'Malformed write receipt.')
            if creation and (type(value.get('number')) is not int
                    or not isinstance(value.get('base'), dict) or not isinstance(value.get('head'), dict)
                    or value['head'].get('repo') is not None and (not isinstance(value['head']['repo'], dict)
                        or not isinstance(value['head']['repo'].get('full_name'), str))):
                raise ApiError('facts_incomplete', 'Incomplete creation identity.')

    def evidence(self, receipt):
        return {'id': receipt['id'], 'author': receipt['user']['login'], 'body': receipt['body'],
                'url': receipt.get('html_url'), 'created_at': receipt.get('created_at')}

    def actor(self, comment):
        return (comment.get('user') or {}).get('login', '')

    def receipt_id(self, receipt):
        if not isinstance(receipt, dict) or type(receipt.get('id')) is not int:
            raise ApiError('invalid_session', 'Malformed saved issue receipt; inspect the write journal.')
        return receipt['id']
