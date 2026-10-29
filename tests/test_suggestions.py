"""Exact replacements, durable verification, frontend recovery, and publication."""
import copy
from io import StringIO
from pathlib import Path
import signal
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_api
from pr_review import suggestions as s, publication, settings, providers, terminal
from pr_review.application import dispatch
from pr_review.common import ApiError, atomic_json, digest, read_json
from pr_review.processes import Cancelled, Result
from pr_review.state import Session
from pr_review.cli import parser


ACCEPT = dict(accepted=True, evidence='Replacement independently addresses the finding.', checks=[],
              inspection_only_reason='This text-only correction has no meaningful executable check.')


class SuggestionTest(unittest.TestCase):
    def setUp(self):
        self.f = test_api.ApiTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.session = self.f.session
        self.state = self.session.load()
        self.run = SimpleNamespace(session=lambda: self.session, reload=lambda: None)

    def attach(self, index=0, start=1, end=1, replacement='corrected\n'):
        c = self.state['comments'][index]
        target = self.session.git.anchor(dict(path='source.txt', side='RIGHT', start_line=start, line=end))
        c['suggestion_original_placement'] = copy.deepcopy(c['placement'])
        c['placement'] = target
        c['suggestion'] = dict(head=self.f.head, target=target,
            original=''.join(s.source(self.session, 'source.txt').splitlines(keepends=True)[start-1:end]), replacement=replacement)
        c['suggestion_generation'] = dict(status='generated', fingerprint=s.generation_identity(self.session, c))
        return c

    def accepted(self, c):
        c['suggestion']['verification'] = dict(status='accepted', fingerprint=s.identity(self.session, c), **ACCEPT)

    def save(self):
        self.session.save(self.state)

    def test_opt_in_settings_and_cli_preserve_saved_default(self):
        self.assertFalse(settings.from_manifest({})['suggested_changes'])
        self.assertNotIn('suggested_changes', vars(parser().parse_args(['7'])))
        self.assertTrue(parser().parse_args(['7', '--suggested-changes']).suggested_changes)
        self.assertFalse(parser().parse_args(['7', '--no-suggested-changes']).suggested_changes)
        with self.assertRaises(ApiError):
            settings.validate({**settings.DEFAULTS, 'suggested_changes': 'true'})
        self.assertIn('automatic-review', dispatch({'command': 'capabilities'})['commands'])

    def test_git_lines_do_not_split_unicode_separators(self):
        self.assertEqual(s.lines('one\u2028two\nthree\vfour\n'), ['one\u2028two\n', 'three\vfour\n'])

    def test_added_file_is_supported_but_crlf_and_symlinks_are_not(self):
        (self.f.repo / 'added.py').write_text('answer = 1\n')
        (self.f.repo / 'crlf.txt').write_bytes(b'one\r\ntwo\r\n')
        (self.f.repo / 'link').symlink_to('added.py')
        self.f.git('add', '.')
        self.f.git('commit', '-qm', 'files in PR')
        self.f.head = self.f.git('rev-parse', 'HEAD')
        self.f.manifest['snapshot']['head'] = self.f.head
        atomic_json(self.f.path / 'manifest.json', self.f.manifest)
        self.session = Session({'session': str(self.f.path)})
        self.state = self.session.load()
        c = self.state['comments'][0]
        target = self.session.git.anchor(dict(path='added.py', side='RIGHT', start_line=1, line=1))
        c['placement'] = target
        c['suggestion'] = dict(head=self.f.head, target=target, original='answer = 1\n', replacement='answer = 2\n')
        s.validate(self.session, c)
        with s.checkout(self.session, [c]) as root:
            self.assertEqual((root / 'added.py').read_text(), 'answer = 2\n')
        for path in ('crlf.txt', 'link'):
            with self.assertRaises(ApiError):
                s.source(self.session, path)

    def test_multiline_payload_and_deletion_preserve_exact_range_and_markers(self):
        c = self.attach(start=1, end=2, replacement='    replacement\n\n')
        self.accepted(c)
        c['status'] = 'staged'
        self.assertEqual(self.session.blockers(c, self.state), [])
        payload = publication.make_actions(self.session, self.state)[0]['payload']
        inline = payload['comments'][0]
        self.assertEqual((inline['start_line'], inline['line'], inline['side']), (1, 2, 'RIGHT'))
        self.assertIn('```suggestion\n    replacement\n\n```\n\n\n<!-- pr-review', inline['body'])
        self.assertEqual(payload['commit_id'], self.f.head)
        c['suggestion']['replacement'] = ''
        self.accepted(c)
        self.assertIn('```suggestion\n```\n', s.render(c))
        with s.checkout(self.session, [c]) as root:
            self.assertEqual((root / 'source.txt').read_text(), 'third\nfourth\n')
        self.assertEqual((self.f.repo / 'source.txt').read_text(), 'first\nnew\nthird\nfourth\n')

    def test_invalid_source_noop_newlines_fences_side_head_and_range(self):
        c = self.attach()
        pristine = copy.deepcopy(c)
        cases = [dict(original='wrong\n'), dict(replacement='first\n'), dict(replacement='x\r\n'),
                 dict(replacement='missing newline'), dict(replacement='```\n'), dict(head='f' * 40),
                 dict(target=dict(path='source.txt', side='RIGHT', start_line=1, line=20)),
                 dict(target=dict(path='source.txt', side='LEFT', start_line=2, line=2))]
        for update in cases:
            with self.subTest(update=update):
                c.clear()
                c.update(copy.deepcopy(pristine))
                c['suggestion'].update(update)
                with self.assertRaises(ApiError):
                    s.validate(self.session, c)
        c.clear()
        c.update(pristine)
        c['body'] = 'Unclosed fence\n```python\ncode'
        with self.assertRaises(ApiError):
            s.validate(self.session, c)

    def test_raw_fences_block_without_opt_in_including_replies_and_overall(self):
        c = self.state['comments'][0]
        for body in ('```suggestion\nx\n```', '> ~~~suggestion\nx', '```suggestion:-1+2\nx'):
            for placement in ({'kind': 'review'}, {'kind': 'reply', 'key': 'x', 'root_id': 1}, c['placement']):
                c.update(body=body, placement=placement)
                self.assertIn('raw suggestion fence', '\n'.join(s.blockers(self.session, c, self.state)))

    def test_verification_is_separate_isolated_and_reused(self):
        c = self.attach()
        def verify(session, stage, context, cwd):
            self.assertEqual(stage, 'suggestion-verify')
            self.assertEqual((cwd / 'source.txt').read_text(), 'corrected\nnew\nthird\nfourth\n')
            self.assertEqual((self.f.repo / 'source.txt').read_text(), 'first\nnew\nthird\nfourth\n')
            return copy.deepcopy(ACCEPT), 'saved-log'
        with patch.object(s, 'invoke', side_effect=verify) as model:
            s.verify_comments(self.session, self.state, [c])
            s.verify_comments(self.session, self.state, [c])
        self.assertEqual(model.call_count, 1)
        self.assertTrue(s.verified(self.session, c))
        self.assertIn('No executable checks run', self.inspect(c))
        c['body'] += ' Edited'
        self.assertFalse(s.verified(self.session, c))

    def inspect(self, c):
        with patch('sys.stdout', new_callable=StringIO) as stdout:
            terminal.inspect(next(v for v in self.session.view(self.state)['comments'] if v['id'] == c['id']))
            return stdout.getvalue()

    def test_checks_failure_or_unjustified_inspection_cannot_be_accepted(self):
        c = self.attach()
        for evidence in ({**ACCEPT, 'inspection_only_reason': ''},
                         {**ACCEPT, 'checks': [dict(command='test', result='failed', passed=False)]}):
            with patch.object(s, 'invoke', return_value=(evidence, 'log')):
                s.verify_comments(self.session, self.state, [c])
            self.assertFalse(s.verified(self.session, c))
            self.assertTrue(s.blockers(self.session, c, self.state))

    def test_verifier_modification_is_rejected(self):
        c = self.attach()
        def model(session, stage, context, cwd):
            (cwd / 'source.txt').write_text('unexpected\n')
            return ACCEPT, 'log'
        with patch.object(s, 'invoke', side_effect=model):
            s.verify_comments(self.session, self.state, [c])
        self.assertEqual(c['suggestion']['verification']['status'], 'failed')
        self.assertIn('changed the proposed source', c['suggestion']['verification']['evidence'])

    def test_combined_verification_and_overlap_block_bulk_staging(self):
        a, b = self.attach(), self.attach(1, start=2, end=2, replacement='other\n')
        with patch.object(s, 'invoke', return_value=(ACCEPT, 'log')) as model:
            s.verify_comments(self.session, self.state, [a, b])
        self.assertEqual(model.call_count, 3)
        self.assertEqual(self.session.blockers(a, self.state, bulk=True), [])
        self.save()
        result = self.f.change('stage', comments=['p1', 'p2'], bulk=True)
        self.assertEqual(result['skipped'], [])
        self.assertEqual([c['status'] for c in result['review']['comments']], ['staged', 'staged'])
        b['suggestion']['target'] = copy.deepcopy(a['suggestion']['target'])
        self.assertTrue(any('overlap' in r for r in s.blockers(self.session, a, self.state, bulk=True)))

    def test_combined_rejection_blocks_individually_accepted_replacements(self):
        a, b = self.attach(), self.attach(1, start=2, end=2, replacement='other\n')
        responses = [(ACCEPT, 'a'), (ACCEPT, 'b'), ({**ACCEPT, 'accepted': False, 'evidence': 'Interaction found.'}, 'both')]
        with patch.object(s, 'invoke', side_effect=responses):
            s.verify_comments(self.session, self.state, [a, b])
        self.assertTrue(s.verified(self.session, a))
        self.assertIn('together', '\n'.join(s.blockers(self.session, a, self.state, bulk=True)))
        self.assertEqual(self.state['suggestion_batch']['evidence'], 'Interaction found.')

    def test_rejected_unselected_candidate_allows_verified_interactive_subset_only(self):
        from pr_review.automation import AutomaticRun
        self.state['mutation_tracking'] = True
        self.state['comments'].append(copy.deepcopy(self.state['comments'][0]))
        self.state['comments'][-1].update(id='p3', finding_ids=['F3'])
        comments = [self.attach(i, start=i+1, end=i+1, replacement=f'fixed{i}\n') for i in range(3)]
        for c in comments[:2]:
            self.accepted(c)
        def model(session, stage, context, cwd):
            if context['combined']:
                self.assertEqual([f['id'] for f in context['findings']], ['p1', 'p2'])
                return ACCEPT, 'combined'
            return {**ACCEPT, 'accepted': False, 'evidence': 'Third suggestion is incomplete.'}, 'rejected'
        with patch.object(s, 'invoke', side_effect=model) as verify:
            s.verify_comments(self.session, self.state, comments)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(self.state['suggestion_batch']['status'], 'accepted')
        self.save()
        github, posts = self.f.fake_github()
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=github):
            outcome = AutomaticRun(dict(operation_id='b' * 32, log_path=str(self.f.root / 'auto.log'))).publish(self.session)
        self.assertEqual(outcome['outcome'], 'needs_attention')
        self.assertEqual(posts, [])
        self.assertTrue(all(c['status'] == 'unstaged' for c in self.session.load()['comments']))
        for identity in ('p1', 'p2'):
            self.assertEqual(self.f.change('stage', comment=identity)['skipped'], [])
        self.assertTrue(self.f.change('stage', comment='p3')['skipped'])

    def test_batch_preview_does_not_claim_an_edited_suggestion_was_verified(self):
        comments = [self.attach(), self.attach(1, start=2, end=2)]
        with patch.object(s, 'invoke', return_value=(ACCEPT, 'log')):
            s.verify_comments(self.session, self.state, comments)
        comments[0]['body'] += ' Changed'
        self.accepted(comments[0])
        shown = self.session.view(self.state)['comments']
        self.assertEqual(shown[0]['suggestion_preview']['batch_verification']['status'], 'not_verified')
        self.assertIn('together', '\n'.join(shown[0]['bulk_blockers']))
        self.assertEqual(shown[1]['suggestion_preview']['batch_verification']['status'], 'accepted')
        self.assertNotIn('Combined verification: accepted', self.inspect(comments[0]))

    def test_interruption_retains_first_verification_and_resume_reuses_it(self):
        a, b = self.attach(), self.attach(1, start=2, end=2, replacement='other\n')
        with patch.object(s, 'invoke', side_effect=[(ACCEPT, 'a'), Cancelled(signal.SIGTERM)]):
            with self.assertRaises(Cancelled):
                s.verify_comments(self.session, self.state, [a, b])
        self.state = self.session.load()
        a, b = self.state['comments']
        self.assertTrue(s.verified(self.session, a))
        self.assertEqual(b['suggestion']['verification']['status'], 'interrupted')
        with patch.object(s, 'invoke', return_value=(ACCEPT, 'log')) as model:
            s.verify_comments(self.session, self.state, [a, b])
        self.assertEqual(model.call_count, 2)  # remaining individual + combined

    def test_generation_omits_complex_fix_and_does_not_rewrite_prose(self):
        candidate = dict(target=dict(path='source.txt', side='RIGHT', start_line=2, line=2), original='new\n', replacement='fixed\n')
        with patch.object(s, 'invoke', side_effect=[({'reason': 'Self-contained.', 'suggestion': candidate}, 'gen'),
                ({'reason': 'Requires coordinated edits.', 'suggestion': None}, 'omit'), (ACCEPT, 'verify')]) as model:
            s.generate(self.run)
        state = self.session.load()
        a, b = state['comments']
        self.assertEqual(a['body'], 'Comment 1')
        self.assertEqual(a['placement']['line'], 2)
        self.assertFalse(a['human_edited'])
        self.assertTrue(s.verified(self.session, a))
        self.assertEqual(b['suggestion_generation']['status'], 'omitted')
        with patch.object(s, 'invoke', side_effect=AssertionError('Unexpected rerun')):
            s.generate(self.run)
        self.assertEqual(model.call_count, 3)

    def test_generation_failure_is_visible_and_removable(self):
        with patch.object(s, 'invoke', side_effect=ApiError('failed', 'No structured result')):
            s.generate(self.run)
        self.state = self.session.load()
        c = self.state['comments'][0]
        self.assertIn('generation is incomplete', '\n'.join(s.blockers(self.session, c, self.state)))
        self.f.change('suggestion-remove', comment='p1')
        c = self.session.load()['comments'][0]
        self.assertEqual(c['body'], 'Comment 1')
        self.assertEqual(s.blockers(self.session, c, self.session.load()), [])

    def test_regeneration_keeps_history_without_becoming_a_manual_decision(self):
        from pr_review.automation import manual_reason
        from pr_review.commands import remember
        c = self.attach()
        c['suggestion_generation']['fingerprint'] = 'older-generation'
        self.state['mutation_tracking'] = True
        self.state['comments'][1]['status'] = 'suppressed'
        self.save()
        with patch.object(s, 'invoke', return_value=({'reason': 'Needs companion edits.', 'suggestion': None}, 'log')):
            s.generate(self.run)
        state = self.session.load()
        self.assertIsNone(manual_reason(state))
        c = state['comments'][0]
        self.assertEqual(c['versions'][0]['suggestion']['replacement'], 'corrected\n')
        self.assertEqual(c['placement']['line'], 1)
        remember(c)
        self.assertIsNotNone(manual_reason(state))
        # Untagged historical versions have unknown authorship, not automatic trust.
        c['versions'] = [{}]
        self.assertIsNotNone(manual_reason(state))

    def test_invalid_candidate_preserves_placement_and_resume_regenerates(self):
        self.state['comments'][1]['status'] = 'suppressed'
        self.save()
        original = copy.deepcopy(self.state['comments'][0]['placement'])
        candidate = dict(target=dict(path='source.txt', side='RIGHT', start_line=2, line=2),
                         original='wrong\n', replacement='fixed\n')
        with patch.object(s, 'invoke', return_value=({'reason': 'Local fix.', 'suggestion': candidate}, 'failed-log')) as model:
            s.generate(self.run)
        self.assertEqual(model.call_count, 1)
        c = self.session.load()['comments'][0]
        self.assertEqual(c['placement'], original)
        self.assertNotIn('suggestion', c)
        self.assertEqual(c['suggestion_generation']['status'], 'failed')
        self.assertEqual(c['suggestion_generation']['log_path'], 'failed-log')
        candidate['original'] = 'new\n'
        with patch.object(s, 'invoke', side_effect=[({'reason': 'Corrected.', 'suggestion': candidate}, 'gen'), (ACCEPT, 'verify')]) as model:
            s.generate(self.run)
        self.assertEqual(model.call_count, 2)
        c = self.session.load()['comments'][0]
        self.assertTrue(s.verified(self.session, c))
        with patch.object(s, 'invoke', side_effect=AssertionError('Successful work must be reused')):
            s.generate(self.run)

    def test_resume_replaces_legacy_failed_or_pending_attached_candidates(self):
        for status in ('failed', 'pending'):
            with self.subTest(status=status):
                c = self.attach(start=2, end=2)
                c['suggestion']['original'] = 'wrong\n'
                c['suggestion_generation']['status'] = status
                self.state['comments'][1]['status'] = 'suppressed'
                self.save()
                candidate = dict(target=dict(path='source.txt', side='RIGHT', start_line=2, line=2),
                                 original='new\n', replacement='fixed\n')
                with patch.object(s, 'invoke', side_effect=[({'reason': 'Corrected.', 'suggestion': candidate}, 'gen'), (ACCEPT, 'verify')]) as model:
                    s.generate(self.run)
                self.assertEqual(model.call_count, 2)
                self.assertTrue(s.verified(self.session, self.session.load()['comments'][0]))
    def test_edit_remove_and_history_keep_finding_and_invalidate_acceptance(self):
        c = self.attach(start=2, end=2)
        self.accepted(c)
        c['status'] = 'staged'
        self.save()
        result = self.f.change('suggestion-edit', comment='p1', replacement='edited\n')
        c = result['review']['comments'][0]
        self.assertEqual(c['status'], 'unstaged')
        self.assertFalse(s.verified(self.session, c))
        self.assertEqual(c['versions'][-1]['suggestion']['replacement'], 'corrected\n')
        result = self.f.change('suggestion-remove', comment='p1')
        c = result['review']['comments'][0]
        self.assertNotIn('suggestion', c)
        self.assertEqual(c['placement']['line'], 1)
        self.assertEqual(c['body'], 'Comment 1')

    def test_body_edit_and_placement_invalidate_verification(self):
        c = self.attach()
        self.accepted(c)
        self.save()
        result = self.f.change('edit', comment='p1', body='Revised explanation')
        self.assertFalse(s.verified(self.session, result['review']['comments'][0]))
        with self.assertRaises(ApiError):
            self.f.change('placement', comment='p1', placement={'kind': 'review'})
        result = self.f.change('placement', comment='p1', placement=dict(kind='inline', path='source.txt', side='RIGHT', start_line=2, line=2))
        c = result['review']['comments'][0]
        self.assertEqual(c['suggestion']['original'], 'new\n')
        self.assertEqual(c['suggestion']['target']['line'], 2)
        self.assertFalse(s.verified(self.session, c))

    def test_recovery_preserves_replacement_but_requires_fresh_verification(self):
        c = self.attach()
        self.accepted(c)
        self.save()
        self.f.change('suggestion-edit', comment='p1', replacement='edited\n')
        result = self.f.change('recover', source_generation=self.session.id, source_comment='p1', version=0)
        recovered = result['review']['comments'][-1]
        self.assertEqual(recovered['suggestion']['replacement'], 'corrected\n')
        self.assertEqual(recovered['finding_ids'], ['F1'])
        self.assertFalse(s.verified(self.session, recovered))
        self.assertEqual(recovered['evidence'], c['evidence'])
        self.assertEqual(recovered['locations'], c['locations'])

    def test_recover_pre_suggestion_version_restores_saved_placement(self):
        from pr_review.commands import remember
        c = self.state['comments'][0]
        original = copy.deepcopy(c['placement'])
        remember(c)
        self.attach(start=2, end=2)
        self.save()
        result = self.f.change('recover', source_generation=self.session.id, source_comment='p1', version=0, comment='p1')
        c = result['review']['comments'][0]
        self.assertEqual(c['placement'], original)
        self.assertNotIn('suggestion', c)
        self.assertNotIn('suggestion_original_placement', c)
        self.assertEqual(c['status'], 'unstaged')

    def test_recover_other_finding_or_document_keeps_destination_identity_and_ordinary_anchor(self):
        self.attach()
        destination = self.state['comments'][1]
        original = copy.deepcopy(destination)
        self.attach(1, start=3, end=3)
        self.save()
        (self.session.path / 'publication/review-body-history-old.md').write_text('Historical document')
        for source in (dict(source_comment='p1'), dict(document='review-body-history-old.md')):
            with self.subTest(source=source):
                # Each case starts with the destination's own attached replacement.
                self.save()
                result = self.f.change('recover', source_generation=self.session.id, comment='p2', **source)
                c = result['review']['comments'][1]
                for key in ('finding_ids', 'classification', 'title', 'evidence', 'locations', 'placement'):
                    self.assertEqual(c[key], original[key], key)
                self.assertNotIn('suggestion', c)
                self.assertEqual(c['body'], 'Comment 1' if 'source_comment' in source else 'Historical document')
                self.assertTrue(c['human_edited'])

    def test_cross_generation_recovery_does_not_reuse_colliding_finding_ids(self):
        self.attach()
        self.save()
        old = self.session.id
        candidate = Path(str(self.f.path) + '-history') / 'attempt-new'
        shutil.copytree(self.f.path, candidate)
        manifest = read_json(candidate / 'manifest.json')
        manifest['session_directory'] = str(self.f.path)
        atomic_json(candidate / 'manifest.json', manifest)
        (self.f.path / '.current-review').symlink_to(candidate)
        self.f.session = Session({'session': str(self.f.path)})
        for destination in ({}, dict(comment='p2')):
            with self.subTest(destination=destination):
                result = self.f.change('recover', source_generation=old, source_comment='p1', **destination)
                c = next(c for c in result['review']['comments'] if c['id'] == result['changed'][0])
                self.assertNotIn('suggestion', c)
                self.assertEqual(c['finding_ids'], ['F2'] if destination else [])
                self.assertEqual(c['body'], 'Comment 1')
                self.assertEqual(c['provenance']['generation'], old)

    def test_changed_model_settings_after_prepare_prevent_publication(self):
        c = self.attach()
        self.accepted(c)
        c['status'] = 'staged'
        self.save()
        github, posts = self.f.fake_github()
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', side_effect=github):
            prepared = dispatch(self.f.request('prepare'))
            self.assertTrue(prepared['ready'])
            manifest = read_json(self.f.path / 'manifest.json')
            manifest['configuration'] = {**settings.DEFAULTS, 'claude_effort': 'medium'}
            atomic_json(self.f.path / 'manifest.json', manifest)
            result = dispatch(self.f.request('publish', confirmation=prepared['confirmation']))
        self.assertFalse(result['complete'])
        self.assertEqual(posts, [])

    def test_prepare_blocks_invalid_attached_suggestion_without_post(self):
        c = self.attach()
        self.accepted(c)
        c['status'] = 'staged'
        c['suggestion']['replacement'] = 'bad'
        self.save()
        with patch.object(publication, 'repository_operation'), patch.object(publication, 'gh', return_value=[]) as gh:
            result = dispatch(self.f.request('prepare'))
        self.assertFalse(result['ready'])
        self.assertFalse(any(call.args[2:] or 'payload' in call.kwargs for call in gh.call_args_list))

    def test_cancelled_validation_preserves_staging_and_saved_state(self):
        c = self.attach()
        self.accepted(c)
        c['status'] = 'staged'
        self.save()
        before = self.session.file.read_bytes()
        with patch.object(publication, 'recover'), patch.object(publication, 'refresh', return_value=[]), \
                patch.object(publication, 'repository_operation'), \
                patch.object(s, 'validate', side_effect=Cancelled(signal.SIGTERM)):
            with self.assertRaises(Cancelled):
                publication.prepare(self.session, self.state)
        self.assertEqual(c['status'], 'staged')
        self.assertEqual(self.session.file.read_bytes(), before)

    def test_api_verification_uses_shared_operation_and_current_revision(self):
        c = self.attach()
        self.save()
        from pr_review.repository import Repository
        with patch.object(Repository, 'from_manifest', return_value=SimpleNamespace(validate_publication=lambda *args: None)), patch.object(s, 'invoke', return_value=(ACCEPT, 'log')):
            result = dispatch(self.f.request('verify-suggestions'))
        self.assertTrue(s.verified(self.session, result['review']['comments'][0]))
        stale = self.f.request('verify-suggestions')
        self.f.change('edit', comment='p1', body='changed')
        with self.assertRaises(ApiError) as error:
            dispatch(stale)
        self.assertEqual(error.exception.code, 'stale_revision')

    def test_provider_invocations_and_structured_output_for_both_providers(self):
        for provider in ('claude', 'codex'):
            self.session.manifest['configuration'] = {**settings.DEFAULTS, 'processing_provider': provider}
            def execute(argv, **kwargs):
                self.assertEqual(kwargs['cwd'], self.f.repo)
                if provider == 'codex':
                    atomic_json(Path(argv[argv.index('-o') + 1]), ACCEPT)
                    self.assertIn('sandbox_mode=workspace-write', argv)
                    return Result(0, '', '', events=[])
                return Result(0, '', '', events=[dict(type='result', subtype='success', structured_output=ACCEPT)])
            with patch.object(s, 'process', side_effect=execute):
                result, directory = s.invoke(self.session, 'suggestion-verify', {}, self.f.repo)
                self.assertEqual(result, ACCEPT)
                self.assertTrue((Path(directory) / 'result.json').exists())


if __name__ == '__main__':
    unittest.main()
