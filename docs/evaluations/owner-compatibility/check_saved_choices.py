"""Ratified saved-choice semantics; separate from the historical precedence oracle.

Usage: python check_saved_choices.py /candidate/checkout
No providers, live sessions, or GitHub calls; only public settings resolution.
"""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(sys.argv[1]).resolve() / 'lib'))
from pr_review import settings


def expect(actual, expected):
    if actual != expected:
        raise AssertionError({'expected': expected, 'actual': actual})


def resolve(saved, request):
    manifest = {'configuration': copy.deepcopy(saved)}
    before = copy.deepcopy(manifest)
    session = SimpleNamespace(pointer=lambda _: None, path=Path('/unused-evaluation-session'))
    with patch.object(settings, 'Session', return_value=session), \
            patch.object(settings, 'read_json', return_value=manifest):
        result = settings.resolve({'session': str(session.path), **request})['settings']
    expect(manifest, before)
    return result


def canonical(provider):
    other = 'codex' if provider == 'claude' else 'claude'
    return settings.validate({
        'reviewer_slots': [
            {'id': 'reviewer-1', 'provider': provider, 'model': 'first-model', 'effort': 'low'},
            {'id': 'reviewer-2', 'provider': provider, 'model': 'second-model', 'effort': 'high'}],
        'processing': {'provider': other, 'model': 'saved-processor', 'effort': 'low'}})


def unchanged_reviewers(provider):
    saved = canonical(provider)
    actual = resolve(saved, {'reviewers': [provider, provider]})
    expect(actual['reviewer_slots'], saved['reviewer_slots'])
    expect(actual['processing'], saved['processing'])


def narrowed_reviewers(provider):
    saved = canonical(provider)
    actual = resolve(saved, {'reviewers': [provider]})
    expect(actual['reviewers'], [provider])
    expect(actual['processing'], saved['processing'])


def legacy_saved_processing(provider):
    other = 'codex' if provider == 'claude' else 'claude'
    # Both providers were enabled: this legacy processing selection was effective,
    # unlike the historical ignored other-provider field with a sole reviewer.
    saved = {'reviewers': ['claude', 'codex'], 'processing_provider': other,
             other + '_model': 'saved-processor', other + '_effort': 'low'}
    actual = resolve(saved, {'reviewers': [provider]})
    expect(actual['reviewers'], [provider])
    expect(actual['processing'], {'provider': other, 'model': 'saved-processor', 'effort': 'low'})


def explicit_processing(provider, canonical_request):
    saved = canonical(provider)
    request = {'reviewers': [provider, provider]}
    if canonical_request:
        request['processing'] = {'provider': provider, 'model': 'replacement-processor', 'effort': 'high'}
    else:
        request['processing_provider'] = provider
    actual = resolve(saved, request)
    expect(actual['processing']['provider'], provider)
    if canonical_request:
        expect(actual['processing'], request['processing'])
    expect(actual['reviewer_slots'], saved['reviewer_slots'])


def saved_over_provisional(provider):
    saved = canonical(provider)
    actual = resolve(saved, {'defaults': {'reviewers': ['claude', 'codex']}, 'economical': True})
    expect(actual['reviewer_slots'], saved['reviewer_slots'])
    expect(actual['processing'], saved['processing'])


results = []
for provider in ('claude', 'codex'):
    for name, function in (
            ('unchanged-reviewers-preserve-slots-and-processing', unchanged_reviewers),
            ('narrowed-reviewers-preserve-processing', narrowed_reviewers),
            ('legacy-effective-processing-survives-reviewer-change', legacy_saved_processing),
            ('explicit-processing-flag-preserves-slots', lambda p: explicit_processing(p, False)),
            ('explicit-processing-object-preserves-slots', lambda p: explicit_processing(p, True)),
            ('saved-choices-outrank-provisional-preset', saved_over_provisional)):
        row = {'case': name, 'provider': provider}
        try:
            function(provider)
            row['passed'] = True
        except Exception as exc:
            row.update(passed=False, error=str(exc) or type(exc).__name__)
        results.append(row)
print(json.dumps(results, indent=2))
raise SystemExit(any(not row['passed'] for row in results))
