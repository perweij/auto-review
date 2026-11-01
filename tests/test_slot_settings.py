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
                             vars(parser().parse_args(['1', '--reviewers', ','.join(reviewers)]))):
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
        args = vars(parser().parse_args(['1', '--reviewers', 'codex,claude',
                                        '--reviewer-1-model', 'first', '--reviewer-1-effort', 'low',
                                        '--reviewer-2-model', 'second', '--processing-model', 'processor']))
        args.pop('pr')
        from pr_review.issue_cli import parser as issue_parser
        issue_args = vars(issue_parser().parse_args(['--issue', '1', '--reviewer-1-model', 'first', '--processing-model', 'processor']))
        self.assertEqual((issue_args['reviewer_1_model'], issue_args['processing_model']), ('first', 'processor'))
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
