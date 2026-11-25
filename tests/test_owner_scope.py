import unittest

from pr_review import owner_scope
from pr_review.common import ApiError


class OwnerScopeTest(unittest.TestCase):
    def test_same_finding_changes_disposition_with_original_contract(self):
        finding = dict(blocker='F1', kind='requirement', reference='pr', source='readers follow a moved directory',
                       scenario='Rename output during verification', impact='Reader loses its input')
        scope = dict(pr='Verify stable archives')
        with self.assertRaises(ApiError):
            owner_scope.validate([finding], ['F1'], scope, [])
        owner_scope.validate([], [], scope, [])  # report as an observation
        scope['pr'] += '; readers follow a moved directory'
        owner_scope.validate([finding], ['F1'], scope, [])

    def test_mixed_api_regression_and_optional_ui_observation(self):
        scope = dict(baseline='a' * 40)
        api = dict(blocker='API', kind='regression', reference=scope['baseline'], source='client.list_users',
                   scenario='Call without a filter', impact='Existing client raises TypeError')
        for _ in range(4):
            owner_scope.validate([api], ['API'], scope, [])
        for rows, blockers in (([api], ['API', 'UI']), ([dict(api, reference='HEAD')], ['API']),
                               ([dict(api, impact='')], ['API']), ([], ['API'])):
            with self.assertRaises(ApiError):
                owner_scope.validate(rows, blockers, scope, [])

    def test_missing_legacy_reference_and_feedback_keys(self):
        scope = owner_scope.contract(dict(baseline='a' * 40))
        basis = dict(blocker='receipt', kind='feedback', reference='thread:1', source='Check artifact identity',
                     scenario='Read inconsistent saved evidence', impact='Invalid artifact receives approval')
        owner_scope.validate([basis], ['receipt'], scope, ['thread:1'])
        with self.assertRaises(ApiError):
            owner_scope.validate([basis], ['receipt'], scope, ['thread:2'])
        with self.assertRaises(ApiError):
            owner_scope.validate([dict(basis, kind='requirement', reference='plan')], ['receipt'], scope, [])
