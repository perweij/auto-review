"""Shared conversation writes work with opaque keys and forge-owned mutations."""
import copy
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from pr_review import conversations
from pr_review.common import ApiError


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.item = dict(key='opaque discussion key', is_resolved=False, can_resolve=True, can_unresolve=True)
        self.state = {}
        self.saved = []
        self.session = SimpleNamespace(manifest={}, save=lambda state: self.saved.append(copy.deepcopy(state)))
        self.native = Mock(capabilities={'resolve', 'reopen'})
        self.forge = patch('pr_review.forge.adapter', return_value=SimpleNamespace(conversations=self.native))
        self.restore = patch('pr_review.forge.restore', return_value=SimpleNamespace(forge='test'))
        self.forge.start()
        self.restore.start()
        self.addCleanup(self.forge.stop)
        self.addCleanup(self.restore.stop)

    def change(self, refresh):
        return conversations.change(self.session, self.state, self.item, True, refresh, lambda: self.item)

    def test_lost_response_uses_readback_without_retry(self):
        self.native.set_state.side_effect = ApiError('network', 'Lost response')
        def refresh():
            self.item['is_resolved'] = True
            conversations.reconcile(self.state, [self.item])
        self.assertEqual(self.change(refresh), (True, True))
        self.native.set_state.assert_called_once_with(self.session, self.item['key'], True)
        self.assertEqual(self.saved[0]['thread_intent']['status'], 'submitting')
        self.assertNotIn('thread_intent', self.state)

    def test_failed_readback_retains_intent_even_after_success_response(self):
        def refresh():
            raise ApiError('network', 'Read unavailable')
        with self.assertRaises(ApiError):
            self.change(refresh)
        self.native.set_state.assert_called_once()
        self.assertEqual(self.state['thread_intent']['status'], 'ambiguous')

    def test_missing_thread_does_not_confirm_cached_state(self):
        def refresh():
            self.item.update(is_resolved=True, missing=True)
        self.assertEqual(self.change(refresh), (False, False))
        self.assertEqual(self.state['thread_intent']['status'], 'ambiguous')

    def test_permission_and_capability_fail_before_journaling_or_send(self):
        for capability, permission in ((False, True), (True, False)):
            with self.subTest(capability=capability):
                self.native.capabilities = {'resolve'} if capability else set()
                self.item['can_resolve'] = permission
                with self.assertRaises(ApiError):
                    self.change(lambda: None)
                self.assertEqual(self.state, {})
                self.assertEqual(self.saved, [])
                self.native.set_state.assert_not_called()
