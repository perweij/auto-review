import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review import models, settings
from pr_review.common import ApiError


class SlotSettingsTest(unittest.TestCase):
    def test_legacy_artifact_mapping_survives_configuration_upgrade(self):
        from pr_review.reviews import Review
        for pending in (False, True):
            review = Review.__new__(Review)
            review.resuming = True
            review.manifest = {'configuration': {'reviewers': ['codex', 'claude'],
                               'processing_provider': 'codex', 'claude_model': 'old-claude',
                               'codex_model': 'old-codex'}, 'phases': {}}
            review.settings = settings.validate(settings.from_manifest(review.manifest))
            review.runtime = SimpleNamespace(permission='default')
            review.tools, review.viewer, review.permission = {}, None, None
            review.path = Path('/pending' if pending else '/saved')
            review.scope, review.begin, review.save = Mock(), Mock(), Mock()
            with patch('pr_review.reviews.say'):
                review.configure('review', [])
            mapping = review.manifest['legacy_review_artifacts']
            self.assertEqual(mapping['reviewer-1'], {'phase': 'claude',
                'output': 'reviews/claude-independent.md', 'provider': 'claude',
                'model': 'old-claude', 'effort': 'high'})
            self.assertEqual(mapping['reviewer-2']['phase'], 'codex')
            self.assertEqual(mapping['reviewer-2']['output'], 'reviews/codex.md')
            self.assertEqual(mapping['reviewer-2']['model'], 'old-codex')
            self.assertEqual(review.manifest['configuration']['reviewers'], ['claude', 'codex'])
            self.assertIsNone(settings.legacy_review_artifacts(review.manifest))

    def test_legacy_single_review_has_no_independent_artifact_mapping(self):
        for provider in ('claude', 'codex'):
            self.assertIsNone(settings.legacy_review_artifacts(
                {'configuration': {'reviewers': [provider]}}))

    def test_resubmission_honors_edited_structured_choices(self):
        from pr_review.api import dispatch
        for provider, other in (('claude', 'codex'), ('codex', 'claude')):
            request = {'protocol': 1, 'command': 'settings',
                       'reviewer_slots': [{'id': 'reviewer-1', 'provider': provider,
                                           'model': 'first', 'effort': 'low'}],
                       'processing': {'provider': provider, 'model': 'processor', 'effort': 'high'}}
            resolved = dispatch(request)['settings']
            slot = {'id': 'reviewer-1', 'provider': other, 'model': 'edited', 'effort': 'high'}
            processor = {'provider': other, 'model': 'edited-processor', 'effort': 'low'}
            result = dispatch({**request, **resolved, 'reviewer_slots': [slot],
                               'processing': processor})['settings']
            self.assertEqual(result['reviewer_slots'], [slot])
            self.assertEqual(result['processing'], processor)
            # A changed individual value remains an override of the object.
            result = dispatch({**request, **resolved, 'processing': processor,
                               'processing_provider': other,
                               'processing_model': 'individual'})['settings']
            self.assertEqual(result['processing']['model'], 'individual')

    def test_resolved_configuration_reapplies_without_losing_choices(self):
        from pr_review.api import dispatch
        for provider in ('claude', 'codex'):
            slots = [{'id': 'reviewer-1', 'provider': provider, 'model': 'first', 'effort': 'low'},
                     {'id': 'reviewer-2', 'provider': provider, 'model': 'second', 'effort': 'high'}]
            processor = {'provider': provider, 'model': 'processor', 'effort': 'low'}
            request = {'protocol': 1, 'command': 'settings', 'reviewer_slots': slots,
                       'processing': processor}
            resolved = dispatch(request)['settings']
            again = dispatch({**request, **resolved})['settings']
            self.assertEqual(again, resolved)
            # Automatic execution resolves once, then applies that result again.
            configuration = settings.initial_settings(resolved)
            settings.apply_overrides(configuration, resolved, fresh=True)
            self.assertEqual(settings.validate(configuration), resolved)
            changed = dispatch({**request, **resolved, provider + '_model': 'explicit',
                                'reviewer_2_model': 'second-override',
                                'processing_model': 'processing-override'})['settings']
            self.assertEqual([s['model'] for s in changed['reviewer_slots']],
                             ['explicit', 'second-override'])
            self.assertEqual(changed['processing']['model'], 'processing-override')

    def test_public_reviewer_forms_and_individual_overrides(self):
        from pr_review.api import dispatch
        for provider, other in (('claude', 'codex'), ('codex', 'claude')):
            slot = {'id': 'reviewer-1', 'provider': provider, 'model': 'object', 'effort': 'low'}
            request = {'protocol': 1, 'command': 'settings', 'reviewer_slots': [slot]}
            result = dispatch({**request, provider + '_model': 'explicit',
                               provider + '_effort': 'high', 'reviewer_1_model': 'slot-specific'})['settings']
            self.assertEqual(result['reviewer_slots'][0], {**slot, 'model': 'slot-specific', 'effort': 'high'})
            result = dispatch({**request, 'reviewers': [other]})['settings']
            self.assertEqual(result['reviewer_slots'][0]['provider'], other)
            result = dispatch({**request, 'reviewers': [provider]})['settings']
            self.assertEqual(result['reviewer_slots'], [slot])
            for invalid in (None, {}, ['bogus']):
                with self.subTest(provider=provider, invalid=invalid), self.assertRaises(ApiError) as error:
                    dispatch({**request, 'reviewers': invalid})
                self.assertEqual(error.exception.code, 'invalid_request')

    def test_public_rejects_malformed_defaults(self):
        from pr_review.api import dispatch
        for value in (None, [], 'invalid'):
            with self.subTest(value=value), self.assertRaises(ApiError) as error:
                dispatch({'protocol': 1, 'command': 'settings', 'defaults': value})
            self.assertEqual(error.exception.code, 'invalid_request')

    def test_public_combined_processing_overrides(self):
        from pr_review.api import dispatch
        for provider, other in (('claude', 'codex'), ('codex', 'claude')):
            slots = [{'id': 'reviewer-1', 'provider': provider, 'model': 'slot', 'effort': 'low'}]
            base = {'protocol': 1, 'command': 'settings', 'reviewer_slots': slots,
                    'processing': {'provider': provider, 'model': 'object', 'effort': 'high'}}
            result = dispatch({**base, 'processing_provider': other, 'processing_model': 'individual',
                               'processing_effort': 'low'})['settings']
            self.assertEqual(result['reviewer_slots'], slots)
            self.assertEqual(result['processing'], {'provider': other, 'model': 'individual', 'effort': 'low'})
            result = dispatch({**base, 'processing_provider': other})['settings']
            self.assertEqual(result['processing'], {'provider': other,
                'model': settings.DEFAULTS[other + '_model'], 'effort': settings.DEFAULTS[other + '_effort']})
            self.assertEqual(result['reviewer_slots'], slots)
            result = dispatch({**base, 'processing_provider': other, 'processing_model': 'new-model'})['settings']
            self.assertEqual(result['processing'], {'provider': other, 'model': 'new-model',
                                                   'effort': settings.DEFAULTS[other + '_effort']})
            resolved = dispatch(base)['settings']
            result = dispatch({**resolved, 'protocol': 1, 'command': 'settings',
                               'processing_provider': other})['settings']
            self.assertEqual(result['reviewer_slots'], slots)
            self.assertEqual(result['processing'], {'provider': other,
                'model': settings.DEFAULTS[other + '_model'], 'effort': settings.DEFAULTS[other + '_effort']})

    def test_null_structured_defaults_are_unset_but_explicit_null_is_invalid(self):
        from pr_review.api import dispatch
        for provider in ('claude', 'codex'):
            for key in ('version', 'reviewer_slots', 'processing', 'processing_provider'):
                with self.subTest(provider=provider, key=key):
                    result = dispatch({'protocol': 1, 'command': 'settings',
                                       'defaults': {'reviewers': [provider], key: None}})['settings']
                    self.assertEqual(result['reviewers'], [provider])
                    self.assertEqual(result['processing']['provider'], provider)
            for key in ('version', 'reviewer_slots', 'processing'):
                with self.subTest(provider=provider, explicit=key), self.assertRaises(ApiError):
                    dispatch({'protocol': 1, 'command': 'settings', 'reviewers': [provider], key: None})

    def test_public_rejects_malformed_structured_choices(self):
        from pr_review.api import dispatch
        for key, values in (('reviewer_slots', (None, {}, [None], ['invalid'])),
                            ('processing', (None, [], 'invalid'))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ApiError) as error:
                    dispatch({'protocol': 1, 'command': 'settings', key: value})
                self.assertEqual(error.exception.code, 'invalid_request')

    def test_legacy_and_explicit_processing(self):
        for provider in ('claude', 'codex'):
            result = settings.validate(settings.initial_settings({'reviewers': [provider]}) | {'reviewers': [provider]})
            self.assertEqual(result['processing']['provider'], provider)
            self.assertEqual(result['reviewer_slots'][0]['id'], 'reviewer-1')
        result = settings.validate(settings.initial_settings({'reviewers': ['claude'], 'processing_provider': 'codex'}) | {'reviewers': ['claude'], 'processing_provider': 'codex'})
        self.assertEqual(result['processing']['provider'], 'codex')

    def test_duplicate_slots_and_independent_catalog_validation(self):
        slots = [{'id': 'reviewer-1', 'provider': 'claude', 'model': 'one', 'effort': 'low'},
                 {'id': 'reviewer-2', 'provider': 'claude', 'model': 'two', 'effort': 'high'}]
        config = settings.validate({'reviewer_slots': slots, 'processing': {'provider': 'codex', 'model': 'three', 'effort': 'high'}})
        catalogs = {'claude': {'provider': 'claude', 'models': [{'value': 'one', 'efforts': ['low']}, {'value': 'two', 'efforts': ['high']}]},
                    'codex': {'provider': 'codex', 'models': [{'value': 'three', 'efforts': ['high']}]}}
        self.assertEqual(config['reviewers'], ['claude', 'claude'])
        models.validate_selection(config, catalogs)
        for provider in ('claude', 'codex'):
            with self.assertRaises(ApiError):
                models.validate_selection(config, {**catalogs, provider: {'provider': provider, 'models': []}})
        with self.assertRaises(ApiError):
            settings.validate({'reviewer_slots': [slots[1]]})
        with self.assertRaises(ApiError):
            settings.validate({'version': 2})

    def test_legacy_overrides_update_saved_choices(self):
        config = settings.validate(settings.DEFAULTS)
        settings.apply_overrides(config, {'claude_model': 'new', 'processing_provider': 'claude'})
        updated = settings.validate(config)
        self.assertEqual(updated['reviewer_slots'][0]['model'], 'new')
        self.assertEqual(updated['processing']['model'], 'new')
        self.assertEqual(updated['processing']['provider'], 'claude')

    def test_presets_and_legacy_manifest(self):
        config = settings.validate(settings.initial_settings({'economical': True}))
        self.assertEqual(config['reviewer_slots'][0]['model'], 'haiku')
        legacy = settings.validate(settings.from_manifest({'models': {'claude': 'old'}}))
        self.assertEqual(legacy['reviewer_slots'][0]['model'], 'old')
        self.assertEqual(legacy['processing']['provider'], 'claude')

    def test_legacy_order_and_single_reviewer_override(self):
        config = settings.validate({'reviewers': ['codex', 'claude']})
        self.assertEqual(config['reviewers'], ['codex', 'claude'])
        from pr_review.reviews import Review
        review = Review.__new__(Review)
        review.settings = config
        self.assertTrue(review.dual)
        settings.apply_overrides(config, {'reviewers': ['claude']})
        self.assertEqual(settings.validate(config)['processing']['provider'], 'codex')
        settings.apply_overrides(config, {'reviewers': ['claude'], 'processing_provider': 'codex'})
        self.assertEqual(settings.validate(config)['processing']['provider'], 'codex')

    def test_public_resolution_saved_choices_and_cli_controls(self):
        from pr_review.cli import parser
        for provider, other in (('claude', 'codex'), ('codex', 'claude')):
            slots = [{'id': 'reviewer-1', 'provider': provider, 'model': 'one', 'effort': 'low'},
                     {'id': 'reviewer-2', 'provider': provider, 'model': 'two', 'effort': 'high'}]
            processor = {'provider': other, 'model': 'three', 'effort': 'low'}
            saved = settings.validate({'reviewer_slots': slots, 'processing': processor})
            session = SimpleNamespace(pointer=lambda _: None, path=Path('/unused'))
            def resolve(**request):
                with patch('pr_review.settings.Session', return_value=session), \
                     patch('pr_review.settings.read_json', return_value={'configuration': saved}):
                    return settings.resolve({**request, 'session': '/unused'})['settings']
            for reviewers in ([provider, provider], [provider]):
                for args in ({'reviewers': reviewers},
                             vars(parser().parse_args(['1', '--reviewers', ','.join(f'{p}-{i}' for i, p in enumerate(reviewers, 1))]))):
                    result = resolve(**args)
                    self.assertEqual(result['reviewer_slots'], slots[:len(reviewers)])
                    self.assertEqual(result['processing'], processor)
            for args in ({'processing_provider': provider},
                         {'processing': {'provider': provider, 'model': 'four', 'effort': 'high'}}):
                result = resolve(reviewers=[provider, provider], **args)
                self.assertEqual(result['reviewer_slots'], slots)
                self.assertEqual(result['processing']['provider'], provider)
                if 'processing' in args:
                    self.assertEqual(result['processing'], args['processing'])
            result = resolve(defaults={'claude_model': 'changed', 'codex_model': 'changed'}, economical=True)
            self.assertEqual(result['reviewer_slots'], slots)
            self.assertEqual(result['processing'], processor)
            fresh = settings.resolve({'reviewers': [provider]})['settings']
            self.assertEqual(fresh['processing']['provider'], provider)

    def test_frontend_defaults_and_pending_choices_resolve_through_api(self):
        from pr_review.api import dispatch
        for provider, other in (("claude", "codex"), ("codex", "claude")):
            defaults = {"reviewers": [provider]}
            fresh = dispatch({"protocol": 1, "command": "settings", "defaults": defaults})["settings"]
            self.assertEqual(fresh["processing"]["provider"], provider)
            explicit = dispatch({"protocol": 1, "command": "settings", "defaults": {**defaults, "processing_provider": other}})["settings"]
            self.assertEqual(explicit["processing"]["provider"], other)
            saved = settings.validate({"reviewer_slots": [{"id": "reviewer-1", "provider": provider,
                       "model": "first", "effort": "low"}],
                       "processing": {"provider": other, "model": "processor", "effort": "high"}})
            for pending in (False, True):
                session = SimpleNamespace(pointer=lambda _: Path('/pending') if pending else None, path=Path('/saved'))
                with patch('pr_review.settings.Session', return_value=session), \
                     patch('pr_review.settings.read_json', return_value={'configuration': saved}) as read:
                    result = dispatch({"protocol": 1, "command": "settings", "session": "/saved", "defaults": defaults})["settings"]
                    self.assertEqual(result["processing"], saved["processing"])
                    self.assertEqual(read.call_args.args[0], Path('/pending' if pending else '/saved') / 'manifest.json')
                    result = dispatch({"protocol": 1, "command": "settings", "session": "/saved",
                        "defaults": {**defaults, "processing": None, "reviewer_slots": None, "version": None}})["settings"]
                    self.assertEqual(result['reviewer_slots'], saved['reviewer_slots'])
                    self.assertEqual(result['processing'], saved['processing'])
                    again = dispatch({"protocol": 1, "command": "settings", "session": "/saved", **result})["settings"]
                    self.assertEqual(again["processing"], saved["processing"])

    def test_legacy_and_fresh_slot_processor(self):
        for provider, other in (('claude', 'codex'), ('codex', 'claude')):
            legacy = settings.from_manifest({'configuration': {'reviewers': [provider],
                                                                'processing_provider': other}})
            self.assertEqual(settings.validate(legacy)['processing']['provider'], provider)
            slot = {'id': 'reviewer-1', 'provider': provider, 'model': 'model', 'effort': 'low'}
            self.assertEqual(settings.resolve({'reviewer_slots': [slot]})['settings']['processing']['provider'], provider)
            mixed = settings.from_manifest({'configuration': {'reviewers': [provider, other],
                                                               'processing_provider': other}})
            settings.apply_overrides(mixed, {'reviewers': [provider]})
            self.assertEqual(settings.validate(mixed)['processing']['provider'], other)

    def test_legacy_saved_configuration_beats_canonical_defaults(self):
        session = SimpleNamespace(pointer=lambda _: None, path=Path('/unused'))
        for provider, other in (('claude', 'codex'), ('codex', 'claude')):
            legacy = {'reviewers': [provider], 'processing_provider': other,
                      provider + '_model': 'saved', provider + '_effort': 'low'}
            defaults = {'reviewer_slots': [{'id': 'reviewer-1', 'provider': other,
                                             'model': 'default', 'effort': 'high'}],
                        'processing': {'provider': other, 'model': 'default', 'effort': 'high'}}
            with patch('pr_review.settings.Session', return_value=session), \
                 patch('pr_review.settings.read_json', return_value={'configuration': legacy}):
                result = settings.resolve({'session': '/unused', 'defaults': defaults})['settings']
            self.assertEqual(result['reviewer_slots'][0], {'id': 'reviewer-1', 'provider': provider,
                                                            'model': 'saved', 'effort': 'low'})
            self.assertEqual(result['processing'], {'provider': provider, 'model': 'saved', 'effort': 'low'})

    def test_fresh_slot_provider_override_uses_provider_defaults(self):
        for provider, other in (('claude', 'codex'), ('codex', 'claude')):
            result = settings.resolve({'reviewers': [other], 'reviewer_1_provider': provider})['settings']
            self.assertEqual(result['reviewer_slots'][0], {'id': 'reviewer-1', 'provider': provider,
                'model': settings.DEFAULTS[provider + '_model'], 'effort': settings.DEFAULTS[provider + '_effort']})
            self.assertEqual(result['processing']['provider'], provider)
            result = settings.resolve({'reviewers': [other], 'reviewer_1_provider': provider,
                                       'reviewer_1_model': 'custom', 'processing_provider': other})['settings']
            self.assertEqual(result['reviewer_slots'][0]['model'], 'custom')
            self.assertEqual(result['processing']['provider'], other)

    def test_public_slot_controls_and_roundtrip(self):
        from pr_review.cli import parser
        args = vars(parser().parse_args(['1', '--reviewers', 'codex-1,claude-1',
                                        '--codex-1-model', 'first', '--codex-1-effort', 'low',
                                        '--claude-1-model', 'second', '--codex-2-model', 'processor']))
        args.pop('pr')
        from pr_review.issue_cli import parser as issue_parser
        issue_args = vars(issue_parser().parse_args(['--issue', '1', '--codex-1-model', 'first', '--codex-2-model', 'processor']))
        self.assertEqual((issue_args['named_codex-1-model'], issue_args['named_codex-2-model']), ('first', 'processor'))
        config = settings.resolve(args)['settings']
        self.assertEqual([s['provider'] for s in config['reviewer_slots']], ['codex', 'claude'])
        self.assertEqual(config['reviewer_slots'][0]['model'], 'first')
        self.assertEqual(config['reviewer_slots'][0]['effort'], 'low')
        self.assertEqual(config['reviewer_slots'][1]['model'], 'second')
        self.assertEqual(config['processing']['model'], 'processor')
        session = SimpleNamespace(pointer=lambda _: None, path=Path('/unused'))
        with patch('pr_review.settings.Session', return_value=session), \
             patch('pr_review.settings.read_json', return_value={'configuration': config}):
            self.assertEqual(settings.resolve({'session': '/unused'})['settings'], config)

    def test_unchanged_legacy_configuration_does_not_begin_generation(self):
        from pr_review.reviews import Review
        review = Review.__new__(Review)
        review.manifest = {'configuration': dict(settings.DEFAULTS),
                           'phases': {'synthesis': {'status': 'completed'}}}
        review.settings = settings.validate(settings.from_manifest(review.manifest))
        review.runtime = SimpleNamespace(permission='default')
        review.tools, review.viewer, review.permission = {}, None, None
        review.path = Path('/tmp')
        review.scope, review.begin, review.save = Mock(), Mock(), Mock()
        with patch('pr_review.reviews.say'):
            review.configure('review', [])
        review.begin.assert_not_called()


    def test_actual_review_changes_still_replace_a_completed_generation(self):
        import copy
        from pr_review import cli_settings
        from pr_review.reviews import Review
        original = cli_settings.resolve({})
        variants = []
        for role in ('processing', 'reviewer_slots'):
            for field in ('model', 'effort'):
                changed = copy.deepcopy(original)
                choice = changed[role][0] if role == 'reviewer_slots' else changed[role]
                choice[field] = 'other' if field == 'model' else 'low'
                variants.append(changed)
        for key, value in (('synthesis_tests', 'full'), ('suggested_changes', True)):
            variants.append({**original, key: value})
        reordered = copy.deepcopy(original)
        reordered['reviewer_slots'].reverse()
        for index, slot in enumerate(reordered['reviewer_slots'], 1):
            slot['id'] = f'reviewer-{index}'
        variants.append(reordered)
        for config in variants:
            with self.subTest(configuration=config):
                review = Review.__new__(Review)
                review.manifest = {'configuration': original, 'phases': {'synthesis': {'status': 'completed'}}}
                review.settings = config
                review.runtime = SimpleNamespace(permission='default')
                review.tools, review.viewer, review.permission = {}, None, None
                review.path = Path('/unused')
                review.scope, review.begin, review.save = Mock(), Mock(), Mock()
                with patch('pr_review.reviews.say'):
                    review.configure('full', [])
                review.begin.assert_called_once()


class OwnerFinalReviewSettingsTest(unittest.TestCase):
    """The Owner settings contract: ``review`` is task checks, ``final_review`` the shared engine."""
    CHOICE = {'provider': 'claude', 'model': 'opus', 'effort': 'high'}

    def legacy(self, **choice):
        value = {**self.CHOICE, **choice}
        return {'fix': dict(value), 'review': dict(value), 'verification': 'focused'}

    def test_defaults_use_the_common_resolved_configuration(self):
        from pr_review import owner_state
        value = owner_state.default_settings({})
        final = value['final_review']
        self.assertEqual(['claude', 'codex'], [slot['provider'] for slot in final['reviewer_slots']])
        self.assertEqual(['reviewer-1', 'reviewer-2'], [slot['id'] for slot in final['reviewer_slots']])
        self.assertEqual('codex', final['synthesis']['provider'])
        self.assertEqual(settings.DEFAULTS['synthesis_tests'], final['synthesis_tests'])
        self.assertEqual('focused', value['verification'])
        self.assertEqual(value['fix'], value['review'])
        self.assertEqual(owner_state.validate_settings(value), value)

    def test_explicit_defaults_configure_reviewer_slots_and_synthesis(self):
        from pr_review import owner_state
        defaults = {'reviewer_slots': [{'id': 'reviewer-1', 'provider': 'codex', 'model': 'a', 'effort': 'low'},
                                       {'id': 'reviewer-2', 'provider': 'codex', 'model': 'b', 'effort': 'high'}],
                    'processing': {'provider': 'claude', 'model': 'c', 'effort': 'medium'}, 'synthesis_tests': 'full'}
        final = owner_state.default_settings({'defaults': defaults})['final_review']
        self.assertEqual(['codex', 'codex'], [slot['provider'] for slot in final['reviewer_slots']])   # same provider twice
        self.assertEqual({'provider': 'claude', 'model': 'c', 'effort': 'medium'}, final['synthesis'])
        self.assertEqual('full', final['synthesis_tests'])

    def test_validate_settings_retains_and_validates_final_review(self):
        from pr_review import owner_state
        value = {**self.legacy(), 'final_review': {
            'reviewer_slots': [{'id': 'reviewer-1', 'provider': 'codex', 'model': 'a', 'effort': 'low'}],
            'synthesis': {'provider': 'claude', 'model': 'c', 'effort': 'medium'}, 'synthesis_tests': 'focused',
            'unknown': 'dropped'}}
        kept = owner_state.validate_settings(value)['final_review']
        self.assertEqual(['reviewer-1'], [slot['id'] for slot in kept['reviewer_slots']])
        self.assertEqual({'reviewer_slots', 'synthesis', 'synthesis_tests'}, set(kept))
        self.assertNotIn('final_review', owner_state.validate_settings(self.legacy()))      # absent stays absent
        for bad in ({'reviewer_slots': [], 'synthesis': self.CHOICE},
                    {'reviewer_slots': [{'id': 'reviewer-2', **self.CHOICE}], 'synthesis': self.CHOICE},
                    {'reviewer_slots': [{'id': f'reviewer-{i}', **self.CHOICE} for i in (1, 2, 3)], 'synthesis': self.CHOICE},
                    {'reviewer_slots': [{'id': 'reviewer-1', **self.CHOICE}], 'synthesis': {'provider': 'other', 'model': 'm', 'effort': 'e'}},
                    {'reviewer_slots': [{'id': 'reviewer-1', **self.CHOICE}], 'synthesis': {**self.CHOICE, 'model': '--flag'}},
                    {'reviewer_slots': [{'id': 'reviewer-1', **self.CHOICE}], 'synthesis': self.CHOICE, 'synthesis_tests': 'all'},
                    {'reviewer_slots': [{'id': 'reviewer-1', **self.CHOICE}]}, 'text'):
            with self.subTest(bad=bad), self.assertRaises(ApiError):
                owner_state.validate_settings({**self.legacy(), 'final_review': bad})

    def test_a_value_without_final_review_is_single_reviewer_and_never_written_back(self):
        from pr_review import owner_state
        saved = self.legacy(provider='codex', model='gpt', effort='low')
        effective = owner_state.effective_settings(saved)['final_review']
        self.assertEqual([{'id': 'reviewer-1', 'provider': 'codex', 'model': 'gpt', 'effort': 'low'}], effective['reviewer_slots'])
        self.assertEqual(saved['review'], effective['synthesis'])
        self.assertNotIn('final_review', saved)                  # so batch['settings'] != state['settings'] keeps working
        from pr_review import review_engine
        config = settings.final_review_configuration(effective)
        self.assertFalse(review_engine.requires_independent(config, {}))   # one slot matching synthesis: direct review

    def test_claude_only_and_codex_only_users_need_no_second_provider(self):
        from pr_review import owner_state
        for provider in ('claude', 'codex'):
            final = owner_state.effective_settings(self.legacy(provider=provider))['final_review']
            self.assertEqual({provider}, {slot['provider'] for slot in final['reviewer_slots']} | {final['synthesis']['provider']})

    def test_explicit_flag_detection(self):
        for flag in ({'reviewers': ['claude']}, {'reviewer_1_model': 'm'}, {'processing_provider': 'codex'},
                     {'synthesis_tests': 'full'}, {'economical': True}, {'codex_model': 'm'}):
            self.assertTrue(settings.explicit_reviewer_flags(flag), flag)
        for quiet in ({}, {'economical': False, 'expensive': False}, {'reviewers': None}, {'issue': 12, 'adopt': True}):
            self.assertFalse(settings.explicit_reviewer_flags(quiet), quiet)

    def test_final_review_round_trips_through_the_effective_view(self):
        from pr_review import owner_state
        value = owner_state.default_settings({})
        effective = owner_state.effective_settings(value)
        self.assertEqual(value['final_review'], effective['final_review'])
        self.assertEqual(value, owner_state.validate_settings(effective))


class OwnerPreflightTest(unittest.TestCase):
    """Preflight names a missing provider, or capability before any model work starts."""

    def run_preflight(self, final_review, *, bwrap=True, help_text=None, models=None, issue=False):
        import tempfile
        from pr_review import owner_providers
        state = {'settings': {'fix': dict(OwnerFinalReviewSettingsTest.CHOICE), 'review': dict(OwnerFinalReviewSettingsTest.CHOICE),
                              'verification': 'focused', **({'final_review': final_review} if final_review else {})}}
        if issue:
            state['issue_workflow'] = 'workflow-id'
            state['settings']['fix'] = dict(final_review['synthesis'])
        session = SimpleNamespace(path=Path(tempfile.mkdtemp()), save=Mock())
        self.addCleanup(__import__('shutil').rmtree, session.path, True)
        catalogs = models or {'claude': ['opus', 'sonnet'], 'codex': ['gpt-6.1-sol']}
        seen = []
        def discover(request):
            provider = request['provider']
            seen.append(provider)
            return {'provider': provider, 'models': [{'value': m, 'efforts': ['low', 'high']} for m in catalogs[provider]]}
        def command(argv, **kwargs):
            seen.append(' '.join(argv))
            text = help_text if help_text is not None else (
                '--sandbox --output-schema --ephemeral --strict-config --cd --skip-git-repo-check --ignore-rules --add-dir '
                '--settings --permission-prompts --json-schema --no-session-persistence --disallowed --base --output-last-message --json '
                '--model --config --append-system-prompt --output-format stream-json --verbose --disallowed-tools --effort --permission-mode')
            return SimpleNamespace(stdout=text)
        def run(argv, **kwargs):
            seen.append('probe ' + argv[0])
            response = argv[argv.index('-o') + 1] if '-o' in argv else None
            if response:
                Path(response).write_text('PR_REVIEW_AUTH_OK')
            events = [{'type': 'turn.completed'}] if argv[0] == 'codex' else [
                {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'PR_REVIEW_AUTH_OK'}]
            return SimpleNamespace(returncode=0, events=events)
        with patch('pr_review.owner_providers.discover', side_effect=discover), \
                patch('pr_review.owner_providers.command', side_effect=command), \
                patch('pr_review.owner_providers.run', side_effect=run), \
                patch('shutil.which', side_effect=lambda name, *a, **k: '/usr/bin/bwrap' if name == 'bwrap' and bwrap else None):
            owner_providers.preflight(session, state, issue_harness=issue)
        return seen

    def test_issue_preflight_does_not_discover_or_probe_unused_task_check_provider(self):
        choice = {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}
        final = {'reviewer_slots': [{'id': 'reviewer-1', **choice}], 'synthesis': choice, 'synthesis_tests': 'focused'}
        seen = self.run_preflight(final, issue=True)
        self.assertIn('probe codex', seen)
        self.assertFalse(any('claude' in item for item in seen), seen)

    def test_every_slot_and_the_synthesis_choice_are_checked_before_model_work(self):
        final = {'reviewer_slots': [{'id': 'reviewer-1', 'provider': 'claude', 'model': 'opus', 'effort': 'high'},
                                    {'id': 'reviewer-2', 'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}],
                 'synthesis': {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}, 'synthesis_tests': 'focused'}
        seen = self.run_preflight(final)
        self.assertEqual({'claude', 'codex'}, {e.split()[1] for e in seen if e.startswith('probe')})
        self.assertIn('codex exec review --help', seen)       # the review capability of an independent Codex slot

    def test_an_unsupported_slot_model_names_the_slot(self):
        final = {'reviewer_slots': [{'id': 'reviewer-1', 'provider': 'claude', 'model': 'missing', 'effort': 'high'}],
                 'synthesis': {'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'}, 'synthesis_tests': 'focused'}
        with self.assertRaisesRegex(ApiError, 'final review reviewer-1'):
            self.run_preflight(final)

    def test_bubblewrap_is_not_required_in_any_configuration(self):
        two = {'reviewer_slots': [{'id': 'reviewer-1', 'provider': 'claude', 'model': 'opus', 'effort': 'high'},
                                  {'id': 'reviewer-2', 'provider': 'claude', 'model': 'sonnet', 'effort': 'high'}],
               'synthesis': {'provider': 'claude', 'model': 'opus', 'effort': 'high'}, 'synthesis_tests': 'focused'}
        self.run_preflight(two, bwrap=False)
        self.run_preflight(two, bwrap=True)
        single = {'reviewer_slots': [{'id': 'reviewer-1', 'provider': 'claude', 'model': 'opus', 'effort': 'high'}],
                  'synthesis': {'provider': 'claude', 'model': 'opus', 'effort': 'high'}, 'synthesis_tests': 'focused'}
        self.run_preflight(single, bwrap=False)             # one slot matching synthesis: no isolation needed
        self.run_preflight(None, bwrap=False)               # a legacy value derives exactly that

    def test_a_cli_without_the_review_capability_is_named(self):
        final = {'reviewer_slots': [{'id': 'reviewer-1', 'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'},
                                    {'id': 'reviewer-2', 'provider': 'claude', 'model': 'opus', 'effort': 'high'}],
                 'synthesis': {'provider': 'claude', 'model': 'opus', 'effort': 'high'}, 'synthesis_tests': 'focused'}
        owner_flags = '--sandbox --output-schema --ephemeral --strict-config --cd --skip-git-repo-check --ignore-rules --add-dir ' \
                      '--settings --permission-prompts --json-schema --no-session-persistence --disallowed'
        with self.assertRaisesRegex(ApiError, 'review capabilities'):
            self.run_preflight(final, help_text=owner_flags)


class NamedTerminalSettingsTest(unittest.TestCase):
    def request(self, *args, issue=False):
        from pr_review.cli import parser
        from pr_review.issue_cli import parser as issue_parser
        return vars((issue_parser() if issue else parser()).parse_args((['-i', '1'] if issue else ['1']) + list(args)))

    def resolve(self, *args, issue=False, saved=None, **kwargs):
        from pr_review import cli_settings
        request = self.request(*args, issue=issue)
        cli_settings.prepare(request, issue=issue)
        return cli_settings.resolve(request, saved, issue=issue, **kwargs)

    def test_shared_roles_and_independent_defaults(self):
        from pr_review import cli_settings
        default = self.resolve(issue=True)
        changed = self.resolve('--codex-1-model', 'custom', issue=True)
        for role in ('processing', 'planning', 'fix'):
            self.assertEqual(default[role], changed[role])
        shared = self.resolve('--planning', 'codex-1', '--processing', 'codex-1', '--fix', 'codex-2',
                              '--codex-1-model', 'shared', '--codex-2-model', 'implementation', issue=True)
        self.assertEqual('shared', shared['planning']['model'])
        self.assertEqual(shared['planning'], shared['processing'])
        self.assertEqual('shared', shared['reviewer_slots'][1]['model'])
        self.assertEqual('implementation', shared['fix']['model'])
        # Saved identity, including intentional sharing, survives a partial override.
        resumed = self.resolve('--codex-1-effort', 'low', saved=shared, issue=True)
        self.assertEqual('low', resumed['planning']['effort'])
        self.assertEqual('low', resumed['processing']['effort'])
        self.assertEqual('high', resumed['fix']['effort'])
        self.assertEqual(cli_settings.effective(shared), cli_settings.effective(self.resolve(saved=shared, issue=True)))

    def test_order_namespace_and_unused_definitions(self):
        first = self.resolve('--reviewers', 'codex-11,codex-12', '--codex-11-model', 'first', '--codex-12-model', 'second')
        second = self.resolve('--reviewers', 'codex-12,codex-11', saved=first)
        self.assertEqual(['second', 'first'], [s['model'] for s in second['reviewer_slots']])
        with self.assertRaisesRegex(ApiError, 'Unused.*claude-22'):
            self.resolve('--claude-22-effort', 'low')
        for args in (('--reviewers', 'codex'), ('--reviewers', 'codex-1,codex-1'),
                     ('--reviewers', 'codex-1,codex-2,codex-3'), ('--processing', 'codex-0')):
            with self.subTest(args=args), self.assertRaises(ApiError):
                self.resolve(*args)
        result = self.resolve('--processing', 'claude-22', '--claude-22-model', 'processing-only')
        self.assertEqual('processing-only', result['processing']['model'])

    def test_fallback_saved_explicit_and_presets(self):
        single = self.resolve('--reviewers', 'claude-1', '--claude-1-model', 'custom')
        self.assertEqual('opus', single['processing']['model'])
        self.assertEqual('claude', single['processing']['provider'])
        self.assertNotEqual(single['_named']['processing'], single['_named']['reviewers'][0])
        explicit = self.resolve('--reviewers', 'claude-1', '--processing', 'codex-2')
        self.assertEqual('codex', explicit['processing']['provider'])
        self.assertEqual('codex', self.resolve('--reviewers', 'claude-1', saved=explicit)['processing']['provider'])
        self.assertEqual('codex', self.resolve('--reviewers', 'claude-1,claude-2')['processing']['provider'])
        for preset in ('economical', 'expensive'):
            result = self.resolve('--' + preset, issue=True)
            self.assertEqual(settings.DEFAULTS['codex_model'], result['planning']['model'])
            self.assertEqual(settings.DEFAULTS['codex_model'], result['fix']['model'])
            custom = self.resolve('--' + preset, '--planning', 'codex-33', '--fix', 'codex-44', issue=True)
            self.assertEqual(settings.DEFAULTS['codex_model'], custom['planning']['model'])
            self.assertEqual(settings.DEFAULTS['codex_model'], custom['fix']['model'])
            resumed = self.resolve('--' + preset, saved=explicit)
            self.assertEqual(explicit, resumed)

    def test_presets_apply_to_new_reviewer_and_processing_names(self):
        for issue in (False, True):
            for preset, values in settings.PRESETS.items():
                for provider in ('claude', 'codex'):
                    with self.subTest(issue=issue, preset=preset, provider=provider):
                        result = self.resolve('--' + preset, '--reviewers', f'{provider}-9,{provider}-10',
                                              '--processing', f'{provider}-11', issue=issue)
                        for choice in [*result['reviewer_slots'], result['processing']]:
                            self.assertEqual(provider, choice['provider'])
                            self.assertEqual(values[provider + '_model'], choice['model'])
                            self.assertEqual(values[provider + '_effort'], choice['effort'])

    def test_single_new_reviewer_presets_keep_fallback_independent(self):
        for issue in (False, True):
            for preset, values in settings.PRESETS.items():
                for provider in ('claude', 'codex'):
                    with self.subTest(issue=issue, preset=preset, provider=provider):
                        result = self.resolve('--' + preset, '--reviewers', f'{provider}-9',
                                              f'--{provider}-9-effort', 'low', issue=issue)
                        self.assertEqual(values[provider + '_model'], result['reviewer_slots'][0]['model'])
                        self.assertEqual('low', result['reviewer_slots'][0]['effort'])
                        self.assertEqual({'provider': provider, 'model': values[provider + '_model'],
                                          'effort': values[provider + '_effort']}, result['processing'])
                        self.assertNotEqual(result['_named']['processing'], result['_named']['reviewers'][0])

    def test_presets_leave_new_planning_and_fix_only_names_at_defaults(self):
        for preset in settings.PRESETS:
            for provider in ('claude', 'codex'):
                with self.subTest(preset=preset, provider=provider):
                    result = self.resolve('--' + preset, '--planning', f'{provider}-33',
                                          '--fix', f'{provider}-44', issue=True)
                    for role in ('planning', 'fix'):
                        self.assertEqual({'provider': provider, 'model': settings.DEFAULTS[provider + '_model'],
                                          'effort': settings.DEFAULTS[provider + '_effort']}, result[role])

    def test_new_shared_names_use_review_presets_for_every_referencing_role(self):
        for preset, values in settings.PRESETS.items():
            for provider in ('claude', 'codex'):
                for review_role in ('reviewers', 'processing'):
                    with self.subTest(preset=preset, provider=provider, review_role=review_role):
                        result = self.resolve('--' + preset, '--' + review_role, f'{provider}-9',
                                              '--planning', f'{provider}-9', '--fix', f'{provider}-9', issue=True)
                        choice = {'provider': provider, 'model': values[provider + '_model'],
                                  'effort': values[provider + '_effort']}
                        self.assertEqual(choice, result['planning'])
                        self.assertEqual(choice, result['fix'])
                        self.assertEqual(choice, result['_named']['model_configs'][f'{provider}-9'])

    def test_saved_named_choices_win_over_presets_while_new_names_use_them(self):
        for issue in (False, True):
            saved = self.resolve('--reviewers', 'claude-9,codex-9', '--processing', 'codex-10',
                                 '--claude-9-model', 'saved-reviewer', '--codex-10-model', 'saved-processor', issue=issue)
            for preset, values in settings.PRESETS.items():
                with self.subTest(issue=issue, preset=preset):
                    self.assertEqual(saved, self.resolve('--' + preset, saved=saved, issue=issue))
                    changed = self.resolve('--' + preset, '--reviewers', 'claude-9,codex-11', saved=saved, issue=issue)
                    self.assertEqual(saved['reviewer_slots'][0], changed['reviewer_slots'][0])
                    self.assertEqual(saved['processing'], changed['processing'])
                    self.assertEqual(values['codex_model'], changed['reviewer_slots'][1]['model'])

    def test_named_file_choices_override_presets_and_flags_apply_last(self):
        import json
        import tempfile
        for issue in (False, True):
            saved = self.resolve('--economical', '--reviewers', 'claude-9,codex-9', '--processing', 'codex-10', issue=issue)
            value = self.resolve('--reviewers', 'claude-9,codex-9', '--processing', 'codex-10',
                                 '--claude-9-model', 'file-reviewer', '--codex-10-model', 'file-processor', issue=issue)['_named']
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'settings.json'
                path.write_text(json.dumps(value))
                for preset in settings.PRESETS:
                    for file_first in (False, True):
                        with self.subTest(issue=issue, preset=preset, file_first=file_first):
                            file_args = ['--settings-file', str(path)]
                            flags = ['--' + preset, '--codex-9-model', 'explicit', '--claude-9-effort', 'low']
                            result = self.resolve(*(file_args + flags if file_first else flags + file_args),
                                                  saved=saved, issue=issue)
                            self.assertEqual(['file-reviewer', 'explicit'], [c['model'] for c in result['reviewer_slots']])
                            self.assertEqual('low', result['reviewer_slots'][0]['effort'])
                            self.assertEqual('file-processor', result['processing']['model'])

    def test_legacy_fix_retains_processing_while_fresh_fix_uses_its_own_default(self):
        fresh = self.resolve('--reviewers', 'claude-1', '--processing', 'claude-1', issue=True)
        self.assertEqual({'provider': 'codex', 'model': settings.DEFAULTS['codex_model'],
                          'effort': settings.DEFAULTS['codex_effort']}, fresh['fix'])
        choice = {'provider': 'claude', 'model': 'sonnet', 'effort': 'low'}
        saved = settings.validate({'processing': choice})
        imported = self.resolve(saved=saved, issue=True)
        self.assertEqual(choice, imported['fix'])
        self.assertNotEqual(imported['_named']['fix'], imported['_named']['processing'])
        changed = self.resolve('--processing', 'codex-9', saved=saved, issue=True)
        self.assertEqual('codex', changed['processing']['provider'])
        self.assertEqual(choice, changed['fix'])

    def test_legacy_import_keeps_equal_roles_independent(self):
        for issue in (False, True):
            for providers in (('codex', 'codex'), ('claude', 'claude'), ('claude', 'codex')):
                with self.subTest(issue=issue, providers=providers):
                    saved = settings.validate({'reviewer_slots': [
                        {'id': f'reviewer-{i}', 'provider': p,
                         'model': settings.DEFAULTS[p + '_model'], 'effort': 'high'}
                        for i, p in enumerate(providers, 1)]})
                    imported = self.resolve(saved=saved, issue=issue)
                    named = imported['_named']
                    roles = ('processing', 'planning', 'fix') if issue else ('processing',)
                    references = [*named['reviewers'], *(named[r] for r in roles)]
                    self.assertEqual(len(references), len(set(references)))
                    for name in references:
                        changed = self.resolve('--' + name + '-effort', 'medium', saved=imported, issue=issue)
                        for index, reviewer in enumerate(named['reviewers']):
                            self.assertEqual('medium' if reviewer == name else 'high',
                                             changed['reviewer_slots'][index]['effort'])
                        for role in roles:
                            self.assertEqual('medium' if named[role] == name else 'high', changed[role]['effort'])

    def test_api_role_edits_reconcile_names_without_losing_unedited_sharing(self):
        shared = self.resolve('--reviewers', 'claude-1', '--processing', 'claude-1',
                              '--planning', 'claude-1', '--fix', 'claude-1', issue=True)
        settings.apply_overrides(shared, {'reviewer_1_provider': 'codex'})
        named = shared['_named']
        self.assertEqual(['codex-1'], named['reviewers'])
        self.assertEqual(['claude-1'] * 3, [named[r] for r in ('processing', 'planning', 'fix')])
        self.assertEqual('codex', named['model_configs']['codex-1']['provider'])
        resumed = self.resolve('--claude-1-effort', 'medium', saved=shared, issue=True)
        self.assertEqual('high', resumed['reviewer_slots'][0]['effort'])
        self.assertEqual(['medium'] * 3, [resumed[r]['effort'] for r in ('processing', 'planning', 'fix')])

    def test_generation_and_complete_file_roundtrip_without_external_work(self):
        import json
        import tempfile
        from pr_review import cli_settings
        from pr_review.cli import main
        from pr_review.issue_cli import main as issue_main
        for issue, entry in ((False, main), (True, issue_main)):
            with self.subTest(issue=issue), tempfile.TemporaryDirectory() as directory, \
                    patch('subprocess.Popen', side_effect=AssertionError('generation must not launch commands')), \
                    patch('pr_review.repository.Repository.resolve', side_effect=AssertionError('no repository')):
                path = Path(directory) / 'defaults.json'
                self.assertEqual(0, entry(['--generate-json-config', str(path)]))
                original = path.read_bytes()
                value = json.loads(original)
                self.assertEqual(cli_settings.defaults(issue=issue), value)
                generated = self.resolve('--settings-file', str(path), issue=issue)
                self.assertEqual(self.resolve(issue=issue), generated)
                self.assertEqual(1, entry(['--generate-json-config', str(path)]))
                self.assertEqual(original, path.read_bytes())
                self.assertEqual(1, entry(['--generate-json-config', str(path / 'missing')]))

    def test_complete_validation_and_final_reference_validation(self):
        import copy
        from pr_review import cli_settings as c
        for issue in (False, True):
            default = c.defaults(issue=issue)
            for field in default:
                invalid = copy.deepcopy(default)
                del invalid[field]
                with self.subTest(issue=issue, field=field), self.assertRaisesRegex(ApiError, field):
                    c.validate(invalid, issue=issue)
            for field in ('provider', 'model', 'effort'):
                invalid = copy.deepcopy(default)
                del invalid['model_configs']['codex-1'][field]
                with self.assertRaisesRegex(ApiError, 'codex-1'):
                    c.validate(invalid, issue=issue)
            for patch_value in ({'processing': None}, {'processing': 'codex-9'}, {'unknown': True},
                                {'reviewers': None}, {'synthesis_tests': None}, {'processing': {}},
                                {'reviewers': ['codex-1', 'codex-1']}):
                with self.subTest(patch_value=patch_value), self.assertRaises(ApiError):
                    c.validate({**default, **patch_value}, issue=issue)
            for bad_choice in ({'provider': 'claude', 'model': 'opus', 'effort': 'high'},
                               {'provider': 'codex', 'model': None, 'effort': 'high'},
                               {'provider': 'codex', 'model': 'ok', 'effort': '-bad'}):
                invalid = copy.deepcopy(default)
                invalid['model_configs']['codex-1'] = bad_choice
                with self.assertRaisesRegex(ApiError, 'codex-1'):
                    c.validate(invalid, issue=issue)
        with self.assertRaisesRegex(ApiError, 'Unused.*claude-1'):
            c.resolve({'_cli_file': c.defaults(), 'cli_reviewers': ['codex-1']})

    def test_file_replaces_saved_flags_win_in_any_order_and_cannot_repair_file(self):
        import json
        import tempfile
        from pr_review import cli_settings as c
        saved = self.resolve('--codex-1-model', 'saved', '--synthesis-tests', 'full', issue=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            value = c.defaults(issue=True)
            value.update(max_runtime='30m', max_weekly_quota_use=5)
            path.write_text(json.dumps(value))
            for before in (True, False):
                file_args = ['--settings-file', str(path)]
                flags = ['--codex-1-model', 'explicit', '--max-runtime', 'none', '--max-weekly-quota-use', 'none']
                result = self.resolve(*(file_args + flags if before else flags + file_args), saved=saved, issue=True)
                self.assertEqual('explicit', result['reviewer_slots'][1]['model'])
                self.assertEqual('focused', result['synthesis_tests'])
                self.assertIsNone(result['_named']['max_runtime'])
                self.assertIsNone(result['_named']['max_weekly_quota_use'])
            del value['fix']
            path.write_text(json.dumps(value))
            with self.assertRaisesRegex(ApiError, 'Missing fields: fix'):
                self.resolve('--settings-file', str(path), '--fix', 'codex-4', saved=saved, issue=True)
            path.write_text('{')
            with self.assertRaisesRegex(ApiError, 'Cannot read settings file'):
                self.resolve('--settings-file', str(path))
            path.write_text(json.dumps({**c.defaults(), 'suggested_changes': True}))
            self.assertFalse(self.resolve('--no-suggested-changes', '--settings-file', str(path))['suggested_changes'])

    def test_obsolete_options_explain_replacement_for_both_parsers(self):
        from contextlib import redirect_stderr
        from io import StringIO
        for issue in (False, True):
            for flag in ('--codex-model', '--claude-effort', '--reviewer-2-model', '--processing-model', '--planning-provider', '--fix-effort'):
                output = StringIO()
                with self.subTest(issue=issue, flag=flag), redirect_stderr(output), self.assertRaises(SystemExit):
                    self.request(flag, 'old', issue=issue)
                self.assertIn('obsolete', output.getvalue())
                self.assertIn('--NAME-model', output.getvalue())

    def test_api_edit_of_named_session_does_not_restore_stale_choices(self):
        saved = self.resolve()
        settings.apply_overrides(saved, {'reviewer_1_model': 'api-edit'})
        resumed = self.resolve(saved=saved)
        self.assertEqual('api-edit', resumed['reviewer_slots'][0]['model'])


    def test_model_validation_discovers_only_active_roles_and_rejects_before_summary(self):
        from pr_review import cli_settings
        for provider in ('claude', 'codex'):
            result = self.resolve('--reviewers', provider + '-1', '--processing', provider + '-2',
                                  '--planning', provider + '-3', '--fix', provider + '-4', issue=True)
            def catalog(request):
                return {'provider': provider, 'models': [{'value': settings.DEFAULTS[provider + '_model'], 'efforts': ['high']}]}
            with patch('pr_review.models.discover', side_effect=catalog) as discover:
                cli_settings.validate_models(result)
                discover.assert_called_once_with({'provider': provider})
                result['fix']['model'] = 'unavailable'
                with self.assertRaisesRegex(ApiError, 'fix: Choose a listed'):
                    cli_settings.validate_models(result)
        result = self.resolve('--synthesis-tests', 'full')
        from contextlib import redirect_stderr
        from io import StringIO
        from pr_review.providers import Runtime
        output = StringIO()
        with redirect_stderr(output):
            cli_settings.summary(result, Runtime(), request={'followup_threads': ['thread:T1']})
        self.assertIn('Follow-up tests: focused', output.getvalue())
        self.assertNotIn('reviewer-1:', output.getvalue())
        self.assertNotIn('Claude budget:', output.getvalue())
