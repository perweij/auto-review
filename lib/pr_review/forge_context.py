"""Application-owned review facts and legacy GitHub compatibility projections."""
from .common import ApiError

FORMAT = 'normalized-review-v1'


def validate_change(value):
    if (not isinstance(value, dict) or not isinstance(value.get('id'), (int, str))
            or isinstance(value.get('id'), bool) or not value.get('url')
            or value.get('state') not in ('open', 'closed', 'merged')
            or type(value.get('draft')) is not bool
            or not all(isinstance(value.get(k), str) for k in ('title', 'body', 'author'))
            or not all(isinstance(value.get(k), dict) for k in ('source', 'target'))
            or not value['source'].get('head') or not value['target'].get('head')):
        raise ApiError('invalid_response', 'Incomplete change-request facts.')
    for name in ('source', 'target'):
        side = value[name]
        if not isinstance(side.get('head'), str) or not side['head']:
            raise ApiError('invalid_response', 'Missing change-request commit identity.')
        if side.get('branch') is not None and not isinstance(side['branch'], str):
            raise ApiError('invalid_response', 'Invalid change-request branch.')
        project = side.get('project')
        if project is not None:
            from .forge import Project
            if not isinstance(project, dict) or not all(isinstance(project.get(k), str) for k in ('forge', 'host', 'path')):
                raise ApiError('invalid_response', 'Invalid project identity.')
            Project(project['forge'], project['host'], project['path'])
    if not value['target'].get('project') or not value['target'].get('branch'):
        raise ApiError('invalid_response', 'Incomplete target identity.')
    return value


def legacy_metadata(change):
    """Preserve public fields while workflows migrate to application-owned facts."""
    validate_change(change)
    return {'number': change['id'], 'url': change['url'], 'title': change['title'],
            'body': change['body'], 'author': {'login': change['author']},
            'state': change['state'].upper(), 'isDraft': change['draft'],
            'headRefOid': change['source']['head'], 'headRefName': change['source'].get('branch'),
            'baseRefOid': change['target']['head'], 'baseRefName': change['target'].get('branch'),
            'isCrossRepository': change.get('cross_project', False)}


def validate_discussion(value):
    if not isinstance(value, dict) or not isinstance(value.get('items'), list):
        raise ApiError('incomplete_discussion', 'Incomplete discussion facts.')
    keys, ids = set(), set()
    for item in value['items']:
        if (not isinstance(item, dict) or not isinstance(item.get('key'), str)
                or item['key'] in keys or item.get('kind') not in ('thread', 'comment', 'review')
                or not isinstance(item.get('comments'), list)):
            raise ApiError('incomplete_discussion', 'Invalid discussion identity.')
        keys.add(item['key'])
        if item['kind'] == 'thread':
            if (not isinstance(item.get('root_id'), (str, int)) or isinstance(item.get('root_id'), bool)
                    or not isinstance(item.get('url'), str)
                    or type(item.get('is_resolved')) is not bool or type(item.get('is_outdated')) is not bool):
                raise ApiError('incomplete_discussion', 'Incomplete conversation state.')
            if any(item.get(k) is not None and type(item[k]) is not bool for k in ('can_reply', 'can_resolve', 'can_unresolve')):
                raise ApiError('incomplete_discussion', 'Invalid conversation permission.')
        if item['kind'] == 'review' and not isinstance(item.get('state'), str):
            raise ApiError('incomplete_discussion', 'Missing review state.')
        for comment in item['comments']:
            if (not isinstance(comment, dict) or not isinstance(comment.get('id'), (str, int)) or isinstance(comment['id'], bool)
                    or not all(isinstance(comment.get(k), str) for k in ('author', 'body', 'url', 'created_at', 'updated_at'))):
                raise ApiError('incomplete_discussion', 'Incomplete discussion comment.')
            identity = (item['kind'], comment['id'])
            if identity in ids:
                raise ApiError('incomplete_discussion', 'Duplicate discussion comment.')
            ids.add(identity)
    return value


def owner_change(state):
    """Read canonical change facts; metadata is only a legacy compatibility projection.

    New writers update change and derive metadata together (Owner refresh).
    Readers never migrate old sessions or treat metadata as a second authority.
    """
    if state.get('change'):
        return state['change']
    metadata = state['metadata']
    return {'source': {'head': metadata['headRefOid'], 'branch': metadata.get('headRefName')},
            'target': {'head': metadata['baseRefOid'], 'branch': metadata.get('baseRefName')}}


def read_change(repository):
    return validate_change(repository.forge.change(repository))


def read_context(repository, change, issues, reviews):
    validate_change(change)
    value, diagnostics = repository.forge.review_context(repository, change, issues, reviews)
    if not isinstance(value, dict) or value.get('change') != change:
        raise ApiError('invalid_response', 'Context belongs to a different change request.')
    if not isinstance(value.get('warnings'), list) or any(not isinstance(w, str) for w in value['warnings']):
        raise ApiError('invalid_response', 'Invalid context warnings.')
    ci = value.get('ci')
    if (not isinstance(ci, dict) or type(ci.get('available')) is not bool or type(ci.get('complete')) is not bool
            or not isinstance(ci.get('coverage'), str) or not isinstance(ci.get('checks'), list)
            or any(not isinstance(c, dict) or c.get('state') not in ('passed', 'failed', 'pending') for c in ci['checks'])):
        raise ApiError('invalid_response', 'Invalid CI facts.')
    for name in ('timeline', 'reviews', 'inline'):
        if name not in value or (value[name] is not None and not isinstance(value[name], list)):
            raise ApiError('invalid_response', 'Missing conversation context availability.')
        for comment in value[name] or []:
            if (not isinstance(comment, dict) or not isinstance(comment.get('id'), (int, str))
                    or isinstance(comment['id'], bool)
                    or not all(isinstance(comment.get(k), str) for k in ('author', 'body', 'url'))):
                raise ApiError('invalid_response', 'Invalid conversation context.')
            anchor = comment.get('anchor')
            if anchor is not None and (not isinstance(anchor, dict) or not isinstance(anchor.get('path'), str)
                                       or anchor.get('side') not in (None, 'old', 'new')
                                       or (anchor.get('line') is not None and (type(anchor['line']) is not int or anchor['line'] < 1))):
                raise ApiError('invalid_response', 'Invalid code anchor.')
    return value, diagnostics
