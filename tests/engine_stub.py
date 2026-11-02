"""Answer the shared engine's Owner and issue reviews with the legacy provider test doubles.

Suites written before the shared engine mock the Owner final review as
``owner_providers.invoke(role='review')`` and the issue review as a harness
report. These helpers route the engine's entry points to those doubles, looked up
late so every ``patch`` of ``owner_providers.invoke`` keeps working. They never
launch a provider. The engine itself is exercised by ``test_review_engine.py``.
"""
from pathlib import Path
from unittest.mock import patch

from pr_review import owner_providers, review_engine


def result(status='completed', **value):
    return review_engine.Result({'assessment': 'passed', **value}, status, Path('/nonexistent/synthesis.json'))


def owner_review(session, state, batch, keys, data):
    return result(), owner_providers.invoke(session, state, batch, 'review', data)


def issue_review_from(report):
    """Build a replacement for ``acceptance.review_issue`` from ``report(session, state, batch, specification)``."""
    def review(session, state, batch, specification):
        for field in ('harness_review', 'harness_review_tree', 'review', 'review_tree', 'final_tree'):
            batch.pop(field, None)
        return result(), report(session, state, batch, specification)
    return review


def route_owner(case):
    patcher = patch('pr_review.acceptance.review_owner', side_effect=owner_review)
    patcher.start()
    case.addCleanup(patcher.stop)


def route_issue(case, report):
    patcher = patch('pr_review.acceptance.review_issue', side_effect=issue_review_from(report))
    patcher.start()
    case.addCleanup(patcher.stop)
