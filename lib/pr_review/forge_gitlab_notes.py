"""Shared GitLab note identity, normalization and single-attempt transport."""
import json
import re

from .common import ApiError


def canonical_body(body):
    return body.replace('\r', '').rstrip()


def context(forge, session, run):
    from .repository import Repository
    from .forge import check_account
    repo = Repository.from_manifest(session.manifest)
    help_text = run(['glab', 'api', '--help'], cwd=repo.cwd, capture_files=True).stdout
    if any(flag not in help_text for flag in ('--hostname', '--method', '--input')):
        raise ApiError('missing_capability', 'Update glab: publication requires api --hostname, --method and --input.')
    account = forge.account(repo.project, repo.cwd)
    check_account(session.manifest.get('forge_account'), account, session.manifest.get('viewer'))
    if not re.fullmatch(r'[1-9][0-9]*', account.get('id', '')):
        raise ApiError('invalid_response', 'GitLab account has no usable numeric identity.')
    raw = forge.get(repo, forge.endpoint(repo))
    if (not isinstance(raw, dict) or raw.get('iid') != repo.pr
            or type(raw.get('id')) is not int or raw['id'] <= 0
            or type(raw.get('target_project_id')) is not int or raw['target_project_id'] <= 0
            or not isinstance(raw.get('web_url'), str) or raw['web_url'].lower() != repo.url.lower()):
        raise ApiError('invalid_response', 'GitLab comment destination could not be verified; use the canonical MR project.')
    destination = dict(project_id=raw['target_project_id'], mr_id=raw['id'], iid=repo.pr, url=raw['web_url'])
    return repo, account, destination, raw


def post(repo, endpoint, payload, run, *, uncertain_code='uncertain_publication'):
    try:
        result = run(['glab', 'api', '--hostname', 'gitlab.com', '--method', 'POST', endpoint, '--input', '-'],
                     cwd=repo.cwd, input_text=json.dumps(payload))
    except FileNotFoundError as exc:
        error = ApiError('missing_dependency', 'Install glab before publishing comments.')
        error.write_not_sent = error.report_not_sent = True
        raise error from exc
    except ApiError as exc:
        # A native API rejection proves non-delivery; 408/5xx/transport loss do not.
        if exc.code == 'command_failed' and re.search(r'\(HTTP (?:400|401|403|404|405|410|413|415|422|429)\)$', str(exc).strip()):
            exc.write_not_sent = exc.report_not_sent = True
        raise
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ApiError(uncertain_code, 'GitLab response was incomplete; recover the receipt before retrying.') from exc
