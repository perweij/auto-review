"""Generation report identity and storage isolation contracts."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pr_review.common import ApiError, atomic_json
from pr_review import review_reports, session_trace, session_report


class ReviewReportsTest(unittest.TestCase):
    def test_replacement_keeps_historical_identity_and_has_its_own_store(self):
        manifest = {}
        review_reports.new_generation(manifest)
        historical = copy.deepcopy(manifest)
        first = review_reports.location(Path('/session'), historical)
        review_reports.new_generation(manifest)
        self.assertNotEqual(first, review_reports.location(Path('/session'), manifest))
        self.assertEqual(historical['execution_report_id'], manifest['execution_report_parent'])
        self.assertEqual(first, review_reports.location(Path('/session'), historical))

    def test_legacy_and_invalid_identities_are_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for manifest in ({}, {'execution_trace_version': 2, 'execution_report_id': '../outside'}):
                with self.assertRaises(ApiError):
                    review_reports.location(root, manifest)
            self.assertEqual([], list(root.iterdir()))

    def test_selected_generation_uses_only_its_explicit_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {'status': 'complete', 'url': 'https://example.test/pr/1'}
            review_reports.new_generation(manifest)
            folder = review_reports.location(root, manifest)
            identity = review_reports.binding(root, manifest)
            session_trace.initialize(folder, identity)
            with session_trace.operation(folder, True, identity):
                with session_trace.stage('synthesis') as handle:
                    handle[1]['call_ids'] = ['selected']
                    session_trace.complete(handle, {'status': 'passed'})
            atomic_json(root / 'usage/ledger.json', {'calls': {
                'selected': {'usage': {'total_tokens': 12}, 'coverage': 'reported'},
                'other-generation': {'usage': {'total_tokens': 900}, 'coverage': 'reported'}}})
            fields, body = review_reports.generate(SimpleNamespace(root=root, manifest=manifest, id='selected-generation'))
            self.assertIn('## Reviewer execution report', body)
            self.assertEqual('Reviewer', fields['report_kind'])
            legacy = {'report_data_path': fields['report_data_path']}
            self.assertEqual(fields['processing_seconds'], session_report.saved_summary(legacy)['processing_seconds'])
            self.assertEqual('complete', fields['report_coverage'])
            self.assertIn('Reported tokens: 12;', body)
            self.assertEqual('selected-generation', fields['generation'])

    def test_missing_trace_is_not_recreated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {}
            review_reports.new_generation(manifest)
            with self.assertRaises(ValueError):
                review_reports.generate(SimpleNamespace(root=root, manifest=manifest, id='missing'))
            self.assertEqual([], list(root.iterdir()))

    def test_generation_transition_splits_journals_and_reuse_keeps_original_choice(self):
        import json
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / 'first', root / 'second'
            (first / 'reviews').mkdir(parents=True)
            artifact = first / 'reviews/reviewer-1.md'
            artifact.write_text('Validated evidence')
            run = SimpleNamespace(root=root, path=first, manifest={'status': 'running'})
            review_reports.allocate(run)
            old = copy.deepcopy(run.manifest)
            with review_reports.Recording(run):
                with session_trace.stage('reviewer-1', choice={'provider': 'codex', 'model': 'original', 'effort': 'high'}) as handle:
                    session_trace.complete(handle, {'status': 'evidence_available'}, artifact)
                shutil.copytree(first, second)
                review_reports.allocate(run)
                run.path = second
                copied = second / 'reviews/reviewer-1.md'
                source = review_reports.reuse_source(run, 'reviewer-1', copied)
                self.assertIsNotNone(source)
                session_trace.reuse('reviewer-1', copied, {'status': 'evidence_available'},
                                    choice={'model': 'changed'}, external_source=source)
                copied.write_text('Changed evidence')
                self.assertIsNone(review_reports.reuse_source(run, 'reviewer-1', copied))
            original = json.loads((review_reports.location(root, old) / 'execution-trace.json').read_text())
            replacement = json.loads((review_reports.location(root, run.manifest) / 'execution-trace.json').read_text())
            self.assertEqual(1, len(original['invocations']))
            self.assertEqual(1, len(replacement['invocations']))
            reused = next(s for s in replacement['stages'] if s['role'] == 'reviewer-1')
            self.assertEqual('original', reused['choice']['model'])
            self.assertEqual(0, reused['seconds'])
            self.assertEqual([], reused['call_ids'])
            self.assertEqual(old['execution_report_id'], reused['external_source']['report'])

    def test_corrupt_reuse_provenance_is_not_used(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / 'review.md'
            artifact.write_text('evidence')
            for sources in (None, [], {'reviewer-1:review.md': 'invalid'},
                            {'reviewer-1:review.md': {'choice': []}}):
                run = SimpleNamespace(path=root, manifest={'execution_report_sources': sources})
                self.assertIsNone(review_reports.reuse_source(run, 'reviewer-1', artifact))

    def test_plain_review_graph_distinguishes_reviewers_synthesis_and_matching(self):
        from pr_review.session_report import graph
        rows = [
            dict(id='a', role='reviewer-1', category='Review'),
            dict(id='b', role='reviewer-2', category='Review'),
            dict(id='c', role='synthesis', category='Synthesis'),
            dict(id='d', role='contributions', category='Discussion matching'),
        ]
        for number, row in enumerate(rows, 1):
            row.update(stage=True, seconds=1, status='complete', sequence=number, model='none')
        data = dict(activities=rows, transitions=[], review={'generation': 'new'})
        diagram = '\n'.join(graph(data, compact=True))
        for label in ('reviewer-1', 'reviewer-2', 'Synthesis', 'Discussion matching'):
            self.assertIn(label, diagram)
        self.assertNotIn('Acceptance review', diagram)

    def test_report_render_failure_preserves_completed_review(self):
        from unittest.mock import Mock, patch
        manifest = {'status': 'complete'}
        review_reports.new_generation(manifest)
        run = SimpleNamespace(manifest=manifest, save=Mock())
        recording = review_reports.Recording(run)
        selected = SimpleNamespace(root=Path('/review'), manifest=manifest, id='generation')
        recording.finished = [selected]
        with patch('pr_review.state.Session', return_value=SimpleNamespace(paths=lambda: [])), \
             patch.object(review_reports, 'generate', side_effect=OSError('Report disk unavailable')):
            recording.render()
        self.assertEqual('complete', manifest['status'])
        self.assertIn('Report disk unavailable', manifest['report_warning'])

    def test_local_rerender_retains_only_matching_published_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {'status': 'complete'}
            review_reports.new_generation(manifest)
            folder = review_reports.location(root, manifest)
            identity = review_reports.binding(root, manifest)
            session_trace.initialize(folder, identity)
            session = SimpleNamespace(root=root, manifest=manifest, id='generation')
            fields, _ = review_reports.generate(session)
            manifest['execution_report'] = dict(fields, report_publication='complete', report_url='https://example.test/comment')
            again, _ = review_reports.generate(session)
            self.assertEqual('https://example.test/comment', again['report_url'])
            with session_trace.operation(folder, True, identity):
                session_trace.decision('resumed')
            changed, _ = review_reports.generate(session)
            self.assertNotIn('report_url', changed)
