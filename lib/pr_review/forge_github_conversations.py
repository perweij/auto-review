"""GitHub thread mutation encoding; retries and completion remain shared."""
import json
from .common import ApiError, command


class GitHubConversations:
    capabilities = frozenset(('resolve', 'reopen'))

    def set_state(self, session, key, resolved):
        verb = 'resolveReviewThread' if resolved else 'unresolveReviewThread'
        query = f'mutation($id: ID!) {{ {verb}(input: {{threadId: $id}}) {{ thread {{ id isResolved }} }} }}'
        value = graphql(session, query, {'id': key.removeprefix('thread:')})
        try:
            thread = value[verb]['thread']
            if thread['id'] != key.removeprefix('thread:') or thread['isResolved'] is not resolved:
                raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError('github_error', 'Thread mutation response could not be confirmed.') from exc


def graphql(session, query, variables):
    host = session.manifest["repository"].split("/", 1)[0]
    response = command(["gh", "api", "--hostname", host, "graphql", "--input", "-"],
                       input_text=json.dumps({"query": query, "variables": variables}))
    try:
        value = json.loads(response.stdout)
        if value.get("errors") or not isinstance(value.get("data"), dict):
            raise ValueError("Missing GraphQL data")
        return value["data"]
    except (ValueError, AttributeError) as exc:
        raise ApiError("github_error", "GitHub could not confirm the conversation operation.") from exc
