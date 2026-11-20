"""Owner-only GitLab permissions and conversation identity.

Reviewer discussion bytes deliberately remain independent of this projection.
"""
import copy
import json
import re
from urllib.parse import quote

from .common import ApiError, command
from .forge_context import validate_discussion


NOTE_FIELDS = 'id system resolvable resolved userPermissions { createNote }'
PAGE_FIELDS = 'pageInfo { hasNextPage endCursor }'


def check_cli(repo):
    help_text = command(['glab', 'api', '--help'], cwd=repo.cwd).stdout
    if any(flag not in help_text for flag in ('--hostname', '--method', '--input')):
        raise ApiError('missing_capability', 'Update glab: Owner requires api --hostname, --method and --input.')


def graphql(repo, query, **variables):
    result = command(['glab', 'api', '--hostname', repo.project.host, '--method', 'POST',
                      'graphql', '--input', '-'], cwd=repo.cwd,
                     input_text=json.dumps(dict(query=query, variables=variables)))
    try:
        value = json.loads(result.stdout)
        if value.get('errors') or not isinstance(value.get('data'), dict):
            raise ValueError()
        return value['data']
    except (ValueError, AttributeError) as exc:
        raise ApiError('incomplete_discussion', 'GitLab Owner permissions could not be verified.') from exc


def connection(value):
    if (not isinstance(value, dict) or not isinstance(value.get('nodes'), list)
            or not isinstance(value.get('pageInfo'), dict)
            or type(value['pageInfo'].get('hasNextPage')) is not bool):
        raise ValueError()
    cursor = value['pageInfo'].get('endCursor') if value['pageInfo']['hasNextPage'] else None
    if value['pageInfo']['hasNextPage'] and (not isinstance(cursor, str) or not cursor):
        raise ValueError()
    return value['nodes'], cursor


def permission(value, name):
    result = value['userPermissions'][name]
    if type(result) is not bool:
        raise ValueError()
    return result


def gid(value, kinds):
    match = re.fullmatch(r'gid://gitlab/(' + '|'.join(kinds) + r')/([^/]+)', value)
    if not match:
        raise ValueError()
    return match[2]


def permissions(repo, account):
    """Read both connection levels completely; errors are never partial success."""
    query = '''query($path: ID!, $iid: String!, $after: String) {
      currentUser { id username }
      project(fullPath: $path) { mergeRequest(iid: $iid) {
        iid webUrl userPermissions { createNote }
        discussions(first: 100, after: $after) { %s nodes {
          id replyId resolvable resolved userPermissions { resolveNote }
          notes(first: 100) { %s nodes { %s } }
        } }
      } }
    }''' % (PAGE_FIELDS, PAGE_FIELDS, NOTE_FIELDS)
    after, seen, discussions, allowed = None, set(), {}, None
    try:
        for _ in range(10000):
            data = graphql(repo, query, path=repo.project.path, iid=str(repo.pr), after=after)
            actor = data['currentUser']
            if (gid(actor['id'], ['User']) != account['id']
                    or actor['username'].lower() != account['username'].lower()):
                raise ApiError('account_changed', 'GitLab account changed during Owner refresh.')
            mr = data['project']['mergeRequest']
            if str(mr['iid']) != str(repo.pr) or mr['webUrl'].lower() != repo.url.lower():
                raise ValueError()
            current_allowed = permission(mr, 'createNote')
            if allowed is not None and allowed != current_allowed:
                raise ValueError()
            allowed = current_allowed
            rows, cursor = connection(mr['discussions'])
            for row in rows:
                identity = gid(row['id'], ['Discussion'])
                if identity in discussions or type(row['resolvable']) is not bool or type(row['resolved']) is not bool:
                    raise ValueError()
                permission(row, 'resolveNote')
                if gid(row['replyId'], ['IndividualNoteDiscussion', 'DiffDiscussion', 'Discussion']) != identity:
                    raise ValueError()
                notes, note_cursor = connection(row['notes'])
                notes = list(notes)
                note_seen = set()
                while note_cursor:
                    if note_cursor in note_seen or len(note_seen) >= 10000:
                        raise ValueError()
                    note_seen.add(note_cursor)
                    page = graphql(repo, '''query($id: ID!, $after: String!) {
                      node(id: $id) { ... on Discussion { notes(first: 100, after: $after) {
                        %s nodes { %s }
                      } } }
                    }''' % (PAGE_FIELDS, NOTE_FIELDS), id=row['id'], after=note_cursor)
                    more, note_cursor = connection(page['node']['notes'])
                    notes.extend(more)
                native_notes = {}
                for note in notes:
                    note_id = int(gid(note['id'], ['Note', 'DiffNote', 'DiscussionNote']))
                    if (note_id in native_notes or type(note['system']) is not bool
                            or type(note['resolvable']) is not bool or type(note['resolved']) is not bool):
                        raise ValueError()
                    permission(note, 'createNote')
                    native_notes[note_id] = note
                if not native_notes:
                    raise ValueError()
                discussions[identity] = {**row, 'native_notes': native_notes}
            if not cursor:
                return allowed, discussions
            if cursor in seen:
                raise ValueError()
            seen.add(cursor)
            after = cursor
        raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ApiError('incomplete_discussion', 'Incomplete GitLab Owner permission context; refresh again.') from exc


def discussion(forge, repo, account):
    for attempt in range(2):
        normalized, diagnostics = forge.discussion(repo)
        allowed, native = permissions(repo, account)
        try:
            raw = diagnostics['discussions']
            if {row['id'] for row in raw} != set(native):
                raise ValueError()
            items = copy.deepcopy(normalized['items'])
            by_root = {item['comments'][0]['id']: item for item in items}
            for row in raw:
                remote = native[row['id']]
                notes = row['notes']
                root = notes[0]
                if {note['id'] for note in notes} != set(remote['native_notes']):
                    raise ValueError()
                for note in notes:
                    graph = remote['native_notes'][note['id']]
                    if (graph['system'] != note['system'] or graph['resolvable'] != note['resolvable']
                            or (note['resolvable'] and graph['resolved'] != note['resolved'])):
                        raise ValueError()
                item = by_root[root['id']]
                resolvable = remote['resolvable']
                if resolvable != root['resolvable']:
                    raise ValueError()
                item.update(key='gitlab-note:' + str(root['id']), root_id=root['id'],
                            discussion_id=row['id'], graphql_id=remote['id'],
                            graphql_reply_id=remote['replyId'], owner_semantic=True,
                            resolvable=resolvable, can_reply=allowed and permission(remote['native_notes'][root['id']], 'createNote') and not root['system'],
                            can_resolve=resolvable and permission(remote, 'resolveNote'),
                            can_unresolve=resolvable and permission(remote, 'resolveNote'))
                item['reply_target'] = dict(kind='reply', discussion_id=row['id'], root_id=root['id'])
                if item['kind'] == 'thread':
                    if item['is_resolved'] != remote['resolved']:
                        raise ValueError()
            return validate_discussion(dict(format_version=1, items=items)), diagnostics
        except (KeyError, TypeError, ValueError) as exc:
            if attempt:
                raise ApiError('incomplete_discussion', 'GitLab discussion changed during permission collection; refresh again.') from exc


def anchors(diagnostics):
    return {note['id']: dict(diff_hunk='', position=copy.deepcopy(note.get('position') or {}))
            for row in diagnostics['discussions'] for note in row['notes']}


class GitLabConversations:
    capabilities = frozenset(('resolve', 'reopen'))

    def set_state(self, session, key, resolved):
        # The shared interface supplies a local key. Resolve it through the fresh
        # Owner projection; conversion must never require parsing that key.
        item = session.items(session.load(), [key])[0]
        if not item.get('resolvable') or not item.get('can_resolve' if resolved else 'can_unresolve'):
            raise ApiError('permission_denied', 'GitLab does not permit this thread action.')
        forge, repo = session.repo.forge, session.repo
        endpoint = forge.endpoint(repo) + '/discussions/' + quote(item['discussion_id'], safe='')
        command(['glab', 'api', '--hostname', repo.project.host, '--method', 'PUT', endpoint,
                 '--input', '-'], cwd=repo.cwd, input_text=json.dumps(dict(resolved=resolved)))
