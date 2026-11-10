"""Elaboration captures evidence without editing the saved review."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from pr_review import elaboration
from pr_review.common import ApiError, digest, atomic_json


class ElaborationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.state = {'comments': [{'id': 'p1', 'body': 'Inspect Dependabot logs after merge.', 'finding_ids': ['F1']}]}
        self.session = SimpleNamespace(lock=root/'lock', path=root, root=root, id='old-generation',
            manifest={'snapshot': {'head': 'reviewed-sha'}}, load=lambda: copy.deepcopy(self.state),
            comment_context=lambda *args: {'rows': []})
        atomic_json(root/'reviews/synthesis.json', {'findings': [{'id': 'F1', 'blocking': False}]})
        self.request = {'generation': self.session.id, 'revision': digest(self.state), 'comment': 'p1'}

    def test_unsaved_wording_and_original_evidence_are_distinct(self):
        before = copy.deepcopy(self.state)
        with patch('pr_review.operations.processing_text', return_value={'body': 'Manual verification'}) as call, \
             patch('pr_review.usage.summary', return_value={}):
            result = elaboration.explain(self.session, {**self.request, 'body': 'Unsaved wording'})
        prompt = call.call_args.args[2]
        for text in ('Unsaved wording', 'Inspect Dependabot', 'reviewed-sha', 'manual post-merge verification'):
            self.assertIn(text, prompt)
        self.assertEqual('elaborate', call.call_args.args[3])
        self.assertEqual('Unsaved wording', result['captured_wording'])
        self.assertEqual(before, self.state)

    def test_stale_revision_and_unlinked_comments_do_not_call_model(self):
        with patch('pr_review.operations.processing_text') as call:
            with self.assertRaises(ApiError):
                elaboration.explain(self.session, {**self.request, 'revision': 'stale'})
            self.state['comments'][0]['finding_ids'] = []
            with self.assertRaises(ApiError):
                elaboration.explain(self.session, {**self.request, 'revision': digest(self.state)})
            call.assert_not_called()

    def test_failure_preserves_saved_state(self):
        before = copy.deepcopy(self.state)
        with patch('pr_review.operations.processing_text', side_effect=ApiError('model_failed', 'Failed')):
            with self.assertRaises(ApiError):
                elaboration.explain(self.session, self.request)
        self.assertEqual(before, self.state)

    def test_missing_and_truncated_evidence_is_explicit(self):
        (self.session.path/'reviews/synthesis.json').unlink()
        (self.session.path/'reviews/reviewer-1.md').write_text('evidence ' * 5000)
        with patch('pr_review.operations.processing_text', return_value={'body': 'Evidence is incomplete'}) as call, \
             patch('pr_review.usage.summary', return_value={}):
            elaboration.explain(self.session, self.request)
        prompt = call.call_args.args[2]
        self.assertIn('"findings": []', prompt)
        self.assertIn('[Report truncated]', prompt)
        atomic_json(self.session.path/'reviews/synthesis.json', {'report_md': 'x' * 33000})
        with patch('pr_review.operations.processing_text', return_value={'body': 'Partial evidence'}) as call, \
             patch('pr_review.usage.summary', return_value={}):
            elaboration.explain(self.session, self.request)
        self.assertIn('[Synthesis truncated]', call.call_args.args[2])

    def test_cancellation_preserves_state(self):
        from pr_review.processes import Cancelled
        before = copy.deepcopy(self.state)
        with patch('pr_review.operations.processing_text', side_effect=Cancelled(15)):
            with self.assertRaises(Cancelled):
                elaboration.explain(self.session, self.request)
        self.assertEqual(before, self.state)
