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

    def run_preflight(self, final_review, *, bwrap=True, help_text=None, models=None):
        import tempfile
        from pr_review import owner_providers
        state = {'settings': {'fix': dict(OwnerFinalReviewSettingsTest.CHOICE), 'review': dict(OwnerFinalReviewSettingsTest.CHOICE),
                              'verification': 'focused', **({'final_review': final_review} if final_review else {})}}
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
            owner_providers.preflight(session, state)
        return seen

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
