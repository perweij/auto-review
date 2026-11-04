"""Answer the shared engine's Owner and issue reviews with the legacy provider test doubles.

Suites written before the shared engine mock the Owner final review as
``owner_providers.invoke(role='review')`` and the issue review as a harness
report. These helpers route the engine's entry points to those doubles, looked up
late so every ``patch`` of ``owner_providers.invoke`` keeps working. They never
launch a provider. The engine itself is exercised by ``test_review_engine.py``.
"""
from pathlib import Path
from unittest.mock import patch

from pr_review import owner_providers, review_engine, review_history
from pr_review.common import digest
from pr_review.storage import write_json


def result(status='completed', **value):
    return review_engine.Result({'assessment': 'passed', **value}, status, Path('/nonexistent/synthesis.json'))


def owner_review(session, state, batch, keys, data):
    return result(), owner_providers.invoke(session, state, batch, 'review', data)


def issue_review_from(report):
    """Build a replacement for ``acceptance.review_issue`` from ``report(session, state, batch, specification)``."""
    def review(session, state, batch, specification):
        for field in ('harness_review', 'harness_review_tree', 'review', 'review_tree', 'final_tree'):
            batch.pop(field, None)
        projected = report(session, state, batch, specification)
        if not review_history.enabled(batch):
            return result(), projected
        # This mock supplies explicit fixture lineage; production derives none
        # from a title. Real schema/provider regressions live in test_review_history.
        key, frozen = review_history.snapshot(batch, review_history.context_identity(session, state, batch))
        previous = [key for key, f in frozen['findings'].items() if f['status'] == 'open' and f['blocking']]
        findings = [{'id': f'F{i + 1}', 'title': text, 'body_md': text, 'evidence': 'mock candidate inspection',
                     'blocking': True, 'severity': 'High', 'validity': 'CONFIRMED', 'source': 'new'}
                    for i, text in enumerate(projected.get('findings', []))]
        links = [{'local_id': f['id'], 'prior_id': previous[i] if i < len(previous) else None}
                 for i, f in enumerate(findings)]
        current = {link['prior_id'] for link in links}
        value = {'assessment': {'ready': 'passed', 'changes_requested': 'changes_requested',
                              'needs_input': 'needs_input'}.get(projected['status'], 'incomplete'),
                 'summary': projected['summary'], 'checks': projected.get('checks', []), 'findings': findings,
                 'outcomes': projected.get('outcomes', []), 'case_results': [], 'questions': projected.get('questions', []),
                 'finding_lineage': links, 'previous_dispositions': [{'finding_id': key,
                     'status': 'open' if key in current else 'resolved', 'reason': 'Mock inspection',
                     'evidence': 'mock candidate inspection'} for key in previous]}
        artifact = session.path / 'mock-acceptance' / (digest([value, key]) + '.json')
        write_json(artifact, value)
        candidate = {'tree': batch['checkpoint']['tree'], 'sha': batch['checkpoint']['tree']}
        record = review_history.ingest(batch, value, frozen, candidate, artifact, key)
        batch['review_round'] = record['id']
        session.save(state)
        return review_engine.Result(value, 'completed', artifact), projected
    return review


def route_owner(case):
    patcher = patch('pr_review.acceptance.review_owner', side_effect=owner_review)
    patcher.start()
    case.addCleanup(patcher.stop)


def route_issue(case, report):
    patcher = patch('pr_review.acceptance.review_issue', side_effect=issue_review_from(report))
    patcher.start()
    case.addCleanup(patcher.stop)
