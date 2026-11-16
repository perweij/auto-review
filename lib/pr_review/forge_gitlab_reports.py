"""Explicit GitLab MR execution-report notes, separate from finding publication."""
import json
import re

from .common import ApiError, command


def canonical_body(body):
    return body.replace('\r', '').rstrip()


class GitLabReports:
    # Offset pagination cannot prove absence after an uncertain submission.
    retry_absent = False

    def __init__(self, forge):
        self.forge = forge

    def validate(self, session):
        from .repository import Repository
        from .forge import check_account
        self.repo = Repository.from_manifest(session.manifest)
        help_text = command(['glab', 'api', '--help'], cwd=self.repo.cwd, capture_files=True).stdout
        if any(flag not in help_text for flag in ('--hostname', '--method', '--input')):
            raise ApiError('missing_capability', 'Update glab: report publication requires api --hostname, --method and --input.')
        self.account = self.forge.account(self.repo.project, self.repo.cwd)
        check_account(session.manifest.get('forge_account'), self.account, session.manifest.get('viewer'))
        if not re.fullmatch(r'[1-9][0-9]*', self.account.get('id', '')):
            raise ApiError('invalid_response', 'GitLab account has no usable numeric identity.')
        raw = self.forge.get(self.repo, self.forge.endpoint(self.repo))
        if (not isinstance(raw, dict) or raw.get('iid') != self.repo.pr
                or type(raw.get('id')) is not int or raw['id'] <= 0
                or type(raw.get('target_project_id')) is not int or raw['target_project_id'] <= 0
                or not isinstance(raw.get('web_url'), str)
                or raw['web_url'].lower() != self.repo.url.lower()):
            raise ApiError('invalid_response', 'GitLab report destination could not be verified; use the canonical MR project.')
        self.destination = dict(project_id=raw['target_project_id'], mr_id=raw['id'],
                                iid=self.repo.pr, url=raw['web_url'])

    def prepare(self, session, key, body, viewer, identity):
        marker = '<!-- pr-review report=' + identity + ' -->'
        # Never permit slash commands even if a future renderer emits one.
        body = re.sub(r'(?m)^([ \t]*)/', lambda m: m[1] + '/\u200b', body)
        return dict(version=1, forge='gitlab', id=identity, report=key, status='draft',
                    destination=dict(self.destination), actor=dict(self.account), marker=marker,
                    payload={'body': canonical_body(body + '\n\n' + marker)})

    def bound(self, action):
        if (action.get('version') != 1 or action.get('forge') != 'gitlab'
                or action.get('destination') != self.destination or action.get('actor') != self.account):
            raise ApiError('invalid_session', 'Saved report destination or account changed; no note was sent.')
        return f"projects/{self.destination['project_id']}/merge_requests/{self.destination['iid']}/notes"

    def candidate(self, action, note):
        # Identity must not depend on body equality: an edited match blocks retry.
        return (not note.get('system') and note.get('project_id') == self.destination['project_id']
                and note.get('noteable_type') == 'MergeRequest'
                and note.get('noteable_id') == self.destination['mr_id']
                and note.get('noteable_iid') == self.destination['iid']
                and str(note['author']['id']) == action['actor']['id']
                and action['marker'] in note['body'])

    def matches(self, session, action, receipt):
        self.bound(action)
        previous = action.get('receipt')
        if previous is not None and (not isinstance(previous, dict) or not isinstance(receipt, dict)
                                     or previous.get('id') != receipt.get('id')):
            return False
        return (self.valid_note(receipt) and self.candidate(action, receipt)
                and canonical_body(receipt['body']) == action['payload']['body'])

    @staticmethod
    def valid_note(note):
        return (isinstance(note, dict) and type(note.get('id')) is int and note['id'] > 0
                and isinstance(note.get('body'), str) and type(note.get('system')) is bool
                and isinstance(note.get('author'), dict) and type(note['author'].get('id')) is int
                and isinstance(note['author'].get('username'), str)
                and type(note.get('project_id')) is int and type(note.get('noteable_id')) is int
                and type(note.get('noteable_iid')) is int and note.get('noteable_type') == 'MergeRequest')

    def lookup(self, session, action):
        endpoint = self.bound(action)
        notes = self.forge.pages(self.repo, endpoint + '?order_by=created_at&sort=asc')
        if any(not self.valid_note(n) for n in notes):
            raise ApiError('uncertain_report', 'Incomplete GitLab note lookup; no note was sent.')
        return [n for n in notes if self.candidate(action, n)]

    def submit(self, session, action):
        endpoint = self.bound(action)
        try:
            result = command(['glab', 'api', '--hostname', 'gitlab.com', '--method', 'POST',
                              endpoint, '--input', '-'], cwd=self.repo.cwd,
                             input_text=json.dumps(action['payload']))
        except FileNotFoundError as exc:
            error = ApiError('missing_dependency', 'Install glab before publishing the report.')
            error.report_not_sent = True
            raise error from exc
        except ApiError as exc:
            # glab reports definitive API rejection as "... (HTTP NNN)".
            # Timeouts (408), transport losses and server errors are uncertain.
            if exc.code == 'command_failed' and re.search(r'\(HTTP (?:400|401|403|404|405|410|413|415|422|429)\)$', str(exc).strip()):
                exc.report_not_sent = True
            raise
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise ApiError('uncertain_report', 'GitLab report response was incomplete; retry to look up its receipt.') from exc

    def receipt_url(self, action):
        self.bound(action)
        return action['destination']['url'] + '#note_' + str(action['receipt']['id'])
