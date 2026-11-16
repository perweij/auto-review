"""Explicit GitLab MR execution-report notes, separate from finding publication."""
import re

from .common import ApiError, command
from .forge_gitlab_notes import canonical_body, context, post


class GitLabReports:
    # Offset pagination cannot prove absence after an uncertain submission.
    retry_absent = False

    def __init__(self, forge):
        self.forge = forge

    def validate(self, session):
        self.repo, self.account, self.destination, _ = context(self.forge, session, command)

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
                and note.get('noteable_iid') in (None, self.destination['iid'])
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
                and (note.get('noteable_iid') is None or type(note['noteable_iid']) is int)
                and note.get('noteable_type') == 'MergeRequest')

    def lookup(self, session, action):
        endpoint = self.bound(action)
        notes = self.forge.pages(self.repo, endpoint + '?order_by=created_at&sort=asc')
        if any(not self.valid_note(n) for n in notes):
            raise ApiError('uncertain_report', 'Incomplete GitLab note lookup; no note was sent.')
        return [n for n in notes if self.candidate(action, n)]

    def submit(self, session, action):
        return post(self.repo, self.bound(action), action['payload'], command, uncertain_code='uncertain_report')

    def receipt_url(self, action):
        self.bound(action)
        return action['destination']['url'] + '#note_' + str(action['receipt']['id'])
