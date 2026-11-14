"""GitHub read transport and translation. Workflow state stays in shared code."""
import json
import re
from .common import ApiError, ROOT, command, now
from .storage import write_json


def fail(message):
    raise ApiError('operation_failed', message)


def metadata(repository, destination=None):
    fields = "number,title,body,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,isCrossRepository,author,headRepository,headRepositoryOwner,maintainerCanModify"
    try:
        value = json.loads(command(["gh", "pr", "view", str(repository.pr), "-R", repository.identity,
                                    "--json", fields], cwd=repository.cwd).stdout)
    except (ApiError, ValueError) as exc:
        if isinstance(exc, ApiError) and exc.code == "cancelled":
            raise
        fail("Repository or PR inaccessible")
    if (not isinstance(value, dict) or str(value.get("number")) != str(repository.pr)
            or str(value.get("url", "")).lower().rstrip("/") != repository.url
            or any(not re.fullmatch(r"[0-9a-f]{40,64}", str(value.get(k, "")))
                   for k in ("headRefOid", "baseRefOid"))
            or not isinstance(value.get("baseRefName"), str) or not value["baseRefName"]
            or not isinstance(value.get("author"), dict) or not isinstance(value["author"].get("login"), str)
            or value.get("state") not in ("OPEN", "CLOSED", "MERGED")):
        fail("PR metadata identity or snapshot is invalid")
    repository.git("check-ref-format", "refs/heads/" + value["baseRefName"])
    if destination:
        write_json(destination, value)
    return value

def pages(repository, endpoint, kind="array"):
    result = command(["gh", "api", "--hostname", repository.host, "--paginate", "--slurp", endpoint], cwd=repository.cwd)
    try:
        pages = json.loads(result.stdout)
        if not isinstance(pages, list):
            raise ValueError()
        if kind == "actions":
            pages = [p["workflow_runs"] for p in pages]
        if any(not isinstance(p, list) for p in pages):
            raise ValueError()
        return [item for page in pages for item in page]
    except (ValueError, KeyError, TypeError) as exc:
        raise ApiError("invalid_response", "Incomplete GitHub pages") from exc

def graphql(repository, query, **variables):
    # Keep the existing gh query/field convention and authentication setup.
    args = ["gh", "api", "--hostname", repository.host, "graphql", "-f", "query=" + query]
    for name, value in variables.items():
        args += ["-F" if type(value) is int else "-f", f"{name}={value}"]
    try:
        value = json.loads(command(args, cwd=repository.cwd).stdout)
        if not isinstance(value, dict) or value.get("errors"):
            raise ValueError()
        return value["data"]
    except (ValueError, KeyError) as exc:
        raise ApiError("invalid_response", "Incomplete GraphQL response") from exc


def normalize(issues, reviews, inline, threads):
    def invalid():
        raise ApiError("incomplete_discussion", "Incomplete or inconsistent discussion snapshot")

    def valid_comment(c):
        return (isinstance(c, dict) and type(c.get("id")) is int and c["id"] > 0
                and isinstance(c.get("user"), dict)
                and isinstance(c["user"].get("login"), str)
                and all(isinstance(c.get(k), str) for k in ("body", "html_url", "created_at", "updated_at")))

    def comment(c):
        return {"id": c["id"], "author": c["user"]["login"], "body": c["body"],
                "url": c["html_url"], "created_at": c["created_at"], "updated_at": c["updated_at"]}

    if not all(isinstance(v, list) for v in (issues, reviews, inline, threads)):
        invalid()
    if not all(valid_comment(c) for c in issues + inline):
        invalid()
    try:
        if any(type(r.get("id")) is not int or not isinstance(r["user"]["login"], str)
               or any(not isinstance(r.get(k), str) for k in ("body", "html_url", "state"))
               or (r["state"] != "PENDING" and not isinstance(r.get("submitted_at"), str)) for r in reviews):
            invalid()
        for thread in threads:
            if (not isinstance(thread.get("id"), str)
                    or any(type(thread.get(k)) is not bool for k in ("isResolved", "isOutdated", "viewerCanReply"))
                    or len(thread["comments"]["nodes"]) != 1
                    or type(thread["comments"]["nodes"][0]["databaseId"]) is not int
                    or type(thread["comments"]["totalCount"]) is not int):
                invalid()
        roots = [t["comments"]["nodes"][0]["databaseId"] for t in threads]
        if (len(roots) != len(set(roots))
                or sorted(c["id"] for c in inline if c.get("in_reply_to_id") is None) != sorted(roots)
                or any((c.get("in_reply_to_id") or c["id"]) not in roots for c in inline)):
            invalid()
        items = []
        for thread, root in zip(threads, roots):
            comments = sorted((c for c in inline if (c.get("in_reply_to_id") or c["id"]) == root),
                              key=lambda c: (c["created_at"], c["id"]))
            if len(comments) != thread["comments"]["totalCount"]:
                invalid()
            items.append({"key": "thread:" + thread["id"], "kind": "thread", "root_id": root,
                          "url": next(c["html_url"] for c in comments if c["id"] == root),
                          "path": thread.get("path"), "line": thread.get("line"),
                          "original_line": thread.get("originalLine"), "is_resolved": thread["isResolved"],
                          "is_outdated": thread["isOutdated"], "can_reply": thread["viewerCanReply"],
                          "can_resolve": thread.get("viewerCanResolve"), "can_unresolve": thread.get("viewerCanUnresolve"),
                          "resolved_by": (thread.get("resolvedBy") or {}).get("login"),
                          "comments": [comment(c) for c in comments]})
        items += [{"key": "comment:" + str(c["id"]), "kind": "comment", "comments": [comment(c)]} for c in issues]
        items += [{"key": "review:" + str(r["id"]), "kind": "review", "state": r["state"],
                   "comments": [{"id": r["id"], "author": r["user"]["login"], "body": r["body"],
                                 "url": r["html_url"], "created_at": r["submitted_at"], "updated_at": r["submitted_at"]}]}
                  for r in reviews if r["state"] != "PENDING"]
        return {"format_version": 1, "items": sorted(items, key=lambda item: item["key"])}
    except (TypeError, KeyError, AttributeError, StopIteration):
        invalid()



def fetch(repository):
    """Fetch complete discussion without depending on a reviewer session."""
    values = {}
    for name, endpoint in (("issues", f"issues/{repository.pr}/comments"),
                           ("reviews", f"pulls/{repository.pr}/reviews"),
                           ("inline", f"pulls/{repository.pr}/comments")):
        values[name] = repository.pages(f"repos/{repository.name}/{endpoint}")
    threads, cursor, seen = [], None, set()
    while True:
        variables = dict(owner=repository.name.split("/")[0], name=repository.name.split("/")[1], number=repository.pr)
        if cursor:
            variables["cursor"] = cursor
        try:
            data = repository.graphql((ROOT / "prompts/threads.graphql").read_text(), **variables)
            pr = data["repository"]["pullRequest"]
            page = pr["reviewThreads"]
            if pr["number"] != repository.pr or not isinstance(page["nodes"], list) or type(page["pageInfo"]["hasNextPage"]) is not bool:
                raise KeyError("thread page")
            threads.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"].get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise KeyError("thread cursor")
            seen.add(cursor)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ApiError("incomplete_discussion", "Incomplete GraphQL thread response") from exc
    values["threads"] = threads
    return normalize(values["issues"], values["reviews"], values["inline"], threads), values



def review_status(repository):
    reviews, cursor, seen = [], None, set()
    try:
        while True:
            variables = dict(owner=repository.name.split("/")[0], name=repository.name.split("/")[1], number=repository.pr)
            if cursor:
                variables["cursor"] = cursor
            data = repository.graphql((ROOT / "prompts/review-status.graphql").read_text(), **variables)
            pr = data["repository"]["pullRequest"]
            page = pr["latestOpinionatedReviews"]
            if (pr["number"] != repository.pr or not isinstance(data["viewer"]["login"], str)
                    or not isinstance(page["nodes"], list) or type(page["pageInfo"]["hasNextPage"]) is not bool
                    or any(not isinstance(r["author"]["login"], str) or not isinstance(r["state"], str) for r in page["nodes"])):
                return
            reviews.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"].get("endCursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                return
            seen.add(cursor)
        return {"available": True, "checked_at": now(), "decision": pr.get("reviewDecision"),
                                 "latest_activity": pr.get("viewerLatestReview"),
                                 "opinionated": next((r for r in reviews if r["author"]["login"].lower() == data["viewer"]["login"].lower()), None)}
    except (ApiError, KeyError, TypeError, AttributeError) as exc:
        if isinstance(exc, ApiError) and exc.code == "cancelled":
            raise
        return  # Review status is optional; it must never imply approval.



def change_from(value, project):
    from .forge_context import validate_change
    source = value.get('headRepository') or {}
    source_path = source.get('nameWithOwner')
    if not source_path and source.get('name') and (value.get('headRepositoryOwner') or {}).get('login'):
        source_path = value['headRepositoryOwner']['login'] + '/' + source['name']
    from .forge import adapter
    source_project = adapter(project.forge).project(project.host + '/' + source_path).saved() if source_path else (
        None if value.get('isCrossRepository') else project.saved())
    return validate_change({'id': value['number'], 'url': value['url'], 'title': value.get('title', ''),
                            'body': value.get('body') or '', 'author': value['author']['login'],
                            'state': value['state'].lower(), 'draft': value.get('isDraft', False),
                            'source': {'project': source_project,
                                       'branch': value.get('headRefName'), 'head': value['headRefOid']},
                            'target': {'project': project.saved(), 'branch': value['baseRefName'], 'head': value['baseRefOid']},
                            'cross_project': value.get('isCrossRepository', False),
                            'permissions': {'maintainer_can_modify': value.get('maintainerCanModify')}})


def review_context(repository, change, issues, reviews):
    """Initial model context; strict discussion matching is a separate read."""
    def comments(values):
        if not isinstance(values, list) or any(
                not isinstance(v, dict) or type(v.get('id')) is not int
                or not isinstance(v.get('user'), dict) or not isinstance(v['user'].get('login'), str)
                or not isinstance(v.get('body'), str) or not isinstance(v.get('html_url'), str)
                for v in values):
            raise ApiError('invalid_response', 'Malformed GitHub review context.')
        return [{'id': v['id'], 'author': v['user']['login'], 'body': v.get('body') or '',
                 'url': v['html_url'], 'created_at': v.get('created_at') or v.get('submitted_at'),
                 'updated_at': v.get('updated_at') or v.get('submitted_at'),
                 **({'state': v['state']} if 'state' in v else {}),
                 **({'anchor': {'path': v['path'], 'line': v.get('line'), 'side': {'LEFT': 'old', 'RIGHT': 'new'}.get(v.get('side'))}} if v.get('path') else {}),
                 **({'reply_to': v['in_reply_to_id']} if v.get('in_reply_to_id') is not None else {})}
                for v in values]
    raw, warnings = {}, []
    for name, endpoint, kind in [('inline', f'pulls/{repository.pr}/comments', 'array'),
                                 ('runs', f'actions/runs?head_sha={change["source"]["head"]}', 'actions'),
                                 ('statuses', f'commits/{change["source"]["head"]}/statuses', 'array')]:
        try:
            raw[name] = repository.pages(f'repos/{repository.name}/{endpoint}', kind)
            if name == 'inline':
                comments(raw[name])
            elif any(not isinstance(v, dict) or not isinstance(v.get('status' if name == 'runs' else 'state'), str)
                     for v in raw[name]):
                raise ApiError('invalid_response', 'Malformed optional CI context.')
        except ApiError as exc:
            if exc.code == 'cancelled':
                raise
            raw[name] = None
            warnings.append(name + ' unavailable; context is incomplete.')
    checks = []
    for run in raw['runs'] or []:
        checks.append({'name': run.get('name', ''), 'url': run.get('html_url'),
                       'state': 'pending' if run.get('status') != 'completed' else
                       'passed' if run.get('conclusion') in ('success', 'neutral', 'skipped') else 'failed'})
    latest = {}
    for status in sorted(raw['statuses'] or [], key=lambda value: value.get('created_at', '')):
        latest[status.get('context')] = status
    for name, status in latest.items():
        checks.append({'name': name, 'url': status.get('target_url'),
                       'state': {'success': 'passed', 'pending': 'pending'}.get(status.get('state'), 'failed')})
    normalized = {}
    for name, values in (('timeline', issues), ('reviews', reviews)):
        try:
            normalized[name] = comments(values)
        except ApiError:
            normalized[name] = None
            warnings.append(name + ' unavailable; context is incomplete.')
    value = {'format': 1, 'change': change, **normalized,
             'inline': comments(raw['inline']) if raw['inline'] is not None else None,
             'inline_available': raw['inline'] is not None,
             'ci': {'available': any(raw[k] is not None for k in ('runs', 'statuses')),
                    'complete': all(raw[k] is not None for k in ('runs', 'statuses')), 'checks': checks,
                    'coverage': 'GitHub Actions and commit statuses; excludes third-party check runs'},
             'warnings': warnings}
    return value, {'issues': issues, 'reviews': reviews, **raw}


def owner_context(repository):
    from .pulls import github_json
    from .repository import normalize_remote
    value = repository.metadata()
    raw = github_json(repository.identity, f'repos/{repository.name}/pulls/{repository.pr}', repository.cwd)
    if raw.get('number') != repository.pr or (raw.get('html_url') or '').lower() != repository.url:
        raise ApiError('invalid_response', 'GitHub returned a different owner PR.')
    head = raw.get('head') or {}
    if head.get('sha') != value['headRefOid']:
        raise ApiError('stale_source', 'PR changed during refresh; retry.')
    change = change_from(value, repository.project)
    target = normalize_remote((head.get('repo') or {}).get('html_url', ''))
    change['source']['project'] = repository.forge.project(target).saved() if target else None
    change['source']['branch'] = head.get('ref')
    account = repository.forge.account_from(repository.project, github_json(repository.identity, 'user', repository.cwd))
    return change, account


def normalize_pull(pull, repository):
    if not isinstance(pull, dict):
        raise ApiError("invalid_response", "GitHub returned an invalid pull request.")
    number = pull.get("number")
    url = f"https://{repository}/pull/{number}"
    if (type(number) is not int or number < 1 or not isinstance(pull.get("html_url"), str)
            or pull["html_url"].lower() != url
            or pull.get("state") not in ("open", "closed") or not isinstance(pull.get("title"), str)):
        raise ApiError("invalid_response", "GitHub pull request identity or state is invalid.")
    return {"pr": number, "repository": repository, "url": pull["html_url"], "title": pull["title"],
            "state": "MERGED" if pull.get("merged_at") else pull["state"].upper(),
            "draft": bool(pull.get("draft")),
            "head_branch": pull["head"].get("ref") if isinstance(pull.get("head"), dict) else None}



def list_changes(repository, cwd):
    from .pulls import github_json
    endpoint = 'repos/' + repository.split('/', 1)[1] + '/pulls'
    pages = github_json(repository, endpoint + '?state=open&per_page=100', cwd, pages=True)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ApiError('invalid_response', 'GitHub returned incomplete pull request pages.')
    return [normalize_pull(raw, repository) for page in pages for raw in page]


def listed_change(repository, number, cwd):
    from .pulls import github_json
    value = normalize_pull(github_json(repository, 'repos/' + repository.split('/', 1)[1] + '/pulls/' + str(number), cwd), repository)
    if value['pr'] != number:
        raise ApiError('invalid_response', 'GitHub returned a different saved pull request.')
    return value
