"""The approved Markdown specification: approval, storage, handoff and resumption."""
import copy
import json
from pathlib import Path
import re
import shutil
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
import test_issue_steps
import test_issues
from pr_review import acceptance, issue_harness, owner_import, owner_state, specification
from pr_review.common import ApiError, digest, read_json
from pr_review.issues import Workflow, dispatch
from pr_review.issue_writes import Writes

REAL_INVOKE = issue_harness.invoke
UNICODE_PLAN = ('## Outcome\nExport résumé ✓ 日本語 \U0001F600 verbatim.\n\n## Scope\nOnly exports.\n\n'
                '## Approach\nReuse the shared service.\n\n## Acceptance scenarios\n- Round trip keeps ✓.\n\n'
                '## Verification\nRun the export tests.')


def comment_document(comment):
    body = comment['body']
    return re.fullmatch(r'## Plan revision \d+\n\n(.*)\n\nApprove with `/fix-issue approve \d+` or `fix-issue -i \d+ --approve \d+`\.'
                        r'\n\n<!-- fix-issue workflow=\w+ action=\w+ -->', body, re.S).group(1)


class WorkflowSpecificationTest(unittest.TestCase):
    def setUp(self):
        self.t = test_issues.IssueIntegrationTest()
        self.addCleanup(self.t.doCleanups)
        self.t.setUp()
        self.req = {**self.t.req, 'issue': None, 'pr': 7}
        for p in (patch.object(Writes, 'push', autospec=True, side_effect=self.t.simulate_push),):
            p.start()
            self.addCleanup(p.stop)
        self.t.assessment.update(plan=UNICODE_PLAN, acceptance=['Round trip keeps ✓', 'Exports are complete'])
        self.delivered = []
        real = self.t.harness

        def harness(session, state, batch, specification_plan, feedback):
            self.delivered.append(specification.handoff(session, state))
            return real(session, state, batch, specification_plan, feedback)
        patcher = patch('pr_review.issue_harness.invoke', side_effect=harness)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plans(self):
        return [c for c in self.t.f['comments'] if c['body'].startswith('## Plan revision')]

    def state(self, result=None):
        return Workflow(self.req, self.t.repo_object, self.t.f).state

    def artifact(self, revision=1):
        return Workflow(self.req, self.t.repo_object, self.t.f).specification_path(revision)

    def propose(self):
        result = dispatch(self.req)
        self.assertEqual('approval', result['outcome'], result)
        return result

    def test_approval_comment_digest_and_artifact_come_from_one_document(self):
        result = self.propose()
        document = specification.render(UNICODE_PLAN, ['Round trip keeps ✓', 'Exports are complete'])
        state = self.state()
        path = Path(state['specifications']['1']['path'])
        self.assertEqual(document.encode('utf-8'), path.read_bytes())
        self.assertEqual(1, len(self.plans()))
        self.assertEqual(document, comment_document(self.plans()[0]))
        self.assertEqual(state['plan']['body'], document)
        self.assertEqual(digest(document), state['plan']['digest'])
        self.assertEqual(state['plan']['digest'], state['specifications']['1']['digest'])
        self.assertIn('Acceptance criteria:\n- Round trip keeps ✓\n- Exports are complete', document)
        # Revision labels and commands are not part of the approved document.
        self.assertNotIn('Approve with', document)
        self.assertNotIn('Plan revision', document)
        self.assertIn('Approve with `/fix-issue approve 1` or `fix-issue -i 12 --approve 1`.', self.plans()[0]['body'])
        # Outside the source worktree, every Git directory and the main checkout.
        for forbidden in (self.t.repo, self.t.repo / '.git', Path(state.get('planning_worktree', '/nonexistent'))):
            self.assertFalse(path.resolve().is_relative_to(forbidden.resolve()))
        self.assertTrue(path.resolve().is_relative_to(Path(result['session']).resolve()))

    def test_idempotent_reruns_keep_the_artifact_and_the_comment(self):
        """Idempotent reruns neither rewrite the document nor repost the comment."""
        self.propose()
        path = self.artifact()
        inode, mtime = path.stat().st_ino, path.stat().st_mtime_ns
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.assertEqual(1, len(self.plans()))
        self.assertEqual((inode, mtime), (path.stat().st_ino, path.stat().st_mtime_ns))
        self.assertEqual(1, len(self.t.assessments))

    def test_continue_after_new_discussion_retains_revision_and_artifact(self):
        self.propose()
        before = self.artifact().read_bytes()
        inode = self.artifact().stat().st_ino
        self.t.f['comments'].append({'id': 900, 'body': 'Thanks, looks fine.', 'user': {'login': 'author'},
                                     'created_at': '2026-11-01T00:00:00+00:00'})
        self.t.assessment['decision'] = 'continue'
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.assertEqual(2, len(self.t.assessments))
        state = self.state()
        self.assertEqual(1, state['plan']['revision'])
        self.assertEqual(['1'], sorted(state['specifications']))
        self.assertEqual((before, inode), (self.artifact().read_bytes(), self.artifact().stat().st_ino))
        self.assertEqual(1, len(self.plans()))

    def test_approved_document_reaches_owner_and_harness_byte_for_byte(self):
        self.propose()
        original = self.artifact().read_bytes()
        result = dispatch({**self.req, 'approve': 1})
        self.assertEqual('ready', result['outcome'], result)
        owner = owner_state.OwnerSession({'session': self.t.path})
        item = next(i for i in owner.load()['feedback'] if i['kind'] == 'issue_plan')
        located = item['source']['specification']
        self.assertTrue(Path(located['path']).resolve().is_relative_to(owner.path.resolve()))
        self.assertEqual(original, Path(located['path']).read_bytes())
        self.assertEqual(self.state()['approval']['digest'], item['source']['plan']['digest'])
        self.assertEqual(comment_document(self.plans()[0]).encode('utf-8'), original)
        self.assertTrue(self.delivered)
        self.assertEqual({original.decode('utf-8')}, {d['text'] for d in self.delivered})

    def test_a_revision_needs_a_new_approval_and_keeps_the_earlier_document(self):
        self.propose()
        first = self.artifact(1).read_bytes()
        self.t.f['comments'].append({'id': 901, 'body': 'Also keep history.', 'user': {'login': 'author'},
                                     'created_at': '2026-11-01T00:00:00+00:00'})
        revised = UNICODE_PLAN.replace('Only exports.', 'Exports and history.')
        self.t.assessment.update(plan=revised, acceptance=['History is retained'])
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.assertEqual(2, self.state()['plan']['revision'])
        self.assertEqual(first, self.artifact(1).read_bytes())
        self.assertIn('Exports and history.', self.artifact(2).read_text(encoding='utf-8'))
        self.assertEqual([1, 2], [int(re.match(r'## Plan revision (\d+)', c['body']).group(1)) for c in self.plans()])
        self.assertEqual('stale_approval', dispatch({**self.req, 'approve': 1})['error']['code'])
        self.assertEqual('ready', dispatch({**self.req, 'approve': 2})['outcome'])
        self.assertEqual({self.artifact(2).read_text(encoding='utf-8')}, {d['text'] for d in self.delivered})
        self.assertEqual(self.state()['approval']['digest'], self.state()['specifications']['2']['digest'])

    def test_a_tampered_or_missing_document_stops_before_approval_or_any_write(self):
        self.propose()
        path = self.artifact()
        original = path.read_bytes()
        comments = len(self.t.f['comments'])
        for label, damage in (('modified', lambda: (path.chmod(0o600), path.write_bytes(original + b'\nExtra'))),
                              ('missing', lambda: path.unlink())):
            with self.subTest(label):
                damage()
                result = dispatch({**self.req, 'approve': 1})
                self.assertEqual('specification_' + label, result['error']['code'])
                self.assertIn('Restore', result['error']['message'])
                self.assertEqual(comments, len(self.t.f['comments']))
                self.assertEqual([], self.delivered)
                self.assertNotIn('approval', self.state())
                specification.write(path, original.decode('utf-8'))
        self.assertEqual('ready', dispatch({**self.req, 'approve': 1})['outcome'])

    def test_edited_approval_text_in_saved_state_is_detected(self):
        self.propose()
        flow = Workflow(self.req, self.t.repo_object, self.t.f)
        flow.state['plan']['body'] += '\n- Quietly added requirement'
        flow.save()
        result = dispatch({**self.req, 'approve': 1})
        self.assertEqual('specification_modified', result['error']['code'])
        self.assertEqual([], self.delivered)

    def test_an_oversized_specification_is_refused_without_publishing_or_truncating(self):
        marker = 'END-OF-SPEC'
        self.t.assessment['plan'] = UNICODE_PLAN + '\n\n' + 'Detail line.\n' * 6000 + marker
        result = dispatch(self.req)
        self.assertEqual('specification_too_large', result['error']['code'], result)
        self.assertIn('never truncated', result['error']['message'])
        self.assertEqual([], self.plans())
        state = self.state()
        self.assertNotIn('plan', state)
        self.assertFalse((Path(result['session']) / 'specifications').exists())
        self.assertFalse(any(marker in c['body'] for c in self.t.f['comments']))

    def test_the_largest_accepted_specification_is_published_in_full(self):
        wrapper = Workflow(self.req, self.t.repo_object, self.t.f)
        overhead = len(wrapper.plan_comment(1, '', published=True)) + len(specification.render('', ['A'])) + 2
        filler = 'é' * (specification.COMMENT_LIMIT - overhead - len(UNICODE_PLAN) - 2)
        self.t.assessment.update(plan=UNICODE_PLAN + '\n\n' + filler, acceptance=['A'])
        self.propose()
        document = specification.render(self.t.assessment['plan'], ['A'])
        self.assertEqual(document, comment_document(self.plans()[0]))
        self.assertLessEqual(len(self.plans()[0]['body']) + 80, specification.COMMENT_LIMIT + 80)

    def test_a_saved_plan_without_a_document_is_materialized_exactly(self):
        self.t.assessment.update(plan='Implement the fix.', acceptance=['Correct output'])
        self.propose()
        legacy = Workflow(self.req, self.t.repo_object, self.t.f)
        body = legacy.state['plan']['body']
        self.assertEqual('Implement the fix.\n\nAcceptance criteria:\n- Correct output', body)
        # Model a session saved before canonical documents existed.
        shutil.rmtree(legacy.path / 'specifications')
        legacy.state.pop('specifications')
        legacy.save()
        comments = len(self.t.f['comments'])
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.assertEqual(comments, len(self.t.f['comments']))
        state = self.state()
        self.assertEqual(body, state['plan']['body'])
        self.assertEqual('saved-plan', state['specifications']['1']['origin'])
        self.assertEqual(body.encode('utf-8'), self.artifact().read_bytes())
        self.assertEqual('ready', dispatch({**self.req, 'approve': 1})['outcome'])
        self.assertEqual({body}, {d['text'] for d in self.delivered})
        # Nothing was strengthened: the plan keeps its exact saved shape.
        self.assertEqual(['revision', 'body', 'text', 'acceptance', 'digest', 'published_at'], list(self.state()['plan']))

    def test_legacy_owner_items_gain_the_exact_approved_body_without_restarting(self):
        self.propose()
        self.assertEqual('ready', dispatch({**self.req, 'approve': 1})['outcome'])
        owner = owner_state.OwnerSession({'session': self.t.path})
        data = owner.load()
        item = next(i for i in data['feedback'] if i['kind'] == 'issue_plan')
        fingerprint = item['fingerprint']
        body = item['source']['plan']['body']
        located = item['source'].pop('specification')
        Path(located['path']).chmod(0o600)
        Path(located['path']).unlink()
        owner.save(data)
        reloaded = owner.load()
        document = specification.handoff(owner, reloaded)
        self.assertEqual(body, document['text'])
        self.assertEqual(body.encode('utf-8'), Path(document['path']).read_bytes())
        again = next(i for i in owner.load()['feedback'] if i['kind'] == 'issue_plan')
        self.assertEqual(fingerprint, again['fingerprint'])
        self.assertEqual('saved-plan', again['source']['specification']['origin'])

    def test_recovered_publication_reuses_the_same_revision_and_document(self):
        real = self.t.api
        failures = []

        def lost_response(endpoint, payload, method='POST'):
            value = real(endpoint, payload, method)
            if payload.get('body', '').startswith('## Plan revision') and not failures:
                failures.append(value)
                raise ApiError('provider_failed', 'HTTP 502 after acceptance')
            return value
        with patch('pr_review.issue_writes.Writes.api', side_effect=lost_response):
            first = dispatch(self.req)
        self.assertEqual('uncertain', first['error']['code'], first)
        self.assertEqual(1, len(self.plans()))
        self.assertEqual('approval', dispatch(self.req)['outcome'])
        self.assertEqual(1, len(self.plans()))
        state = self.state()
        self.assertEqual(['1'], sorted(state['specifications']))
        self.assertEqual(self.artifact().read_text(encoding='utf-8'), comment_document(self.plans()[0]))
        self.assertEqual('ready', dispatch({**self.req, 'approve': 1})['outcome'])

    def test_no_controller_task_pipeline_is_introduced(self):
        self.propose()
        self.assertEqual('ready', dispatch({**self.req, 'approve': 1})['outcome'])
        batch = owner_state.OwnerSession({'session': self.t.path}).load()['batches'][-1]
        self.assertEqual('issue_harness', batch['execution_kind'])
        self.assertEqual(['approved-specification'], [t['id'] for t in batch['tasks']])
        self.assertNotIn('plan', batch)


class HandoffCase(unittest.TestCase):
    """Real harness adapter, fake provider process: exactly what each provider is handed."""
    autostart = True

    def setUp(self, plan=None):
        self.f = test_issue_steps.IssueStepsTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.session = self.f.owner
        self.source = Path(self.f.fixture.root) / 'workflow-documents'
        self.install(plan or {'revision': 1, 'body': UNICODE_PLAN + '\n\nAcceptance criteria:\n- Round trip keeps ✓',
                              'acceptance': ['Round trip keeps ✓']})
        if self.autostart:
            self.f.start(issue_harness=True)
        self.state = self.session.load()
        self.batch = self.state['batches'][-1] if self.state['batches'] else None
        self.identity = '00000000-0000-4000-8000-000000000001'
        self.commands, self.prompts, self.read = [], [], []

    def install(self, plan, name='revision-1.md'):
        plan = {**plan, 'digest': digest(plan['body'])}
        expected = specification.write(self.source / name, plan['body'])
        owner_import.from_issue(self.session, plan, {'title': 'Provider slots', 'html_url': 'https://github.com/org/repo/issues/12'},
                                'workflow', None, specification.locator(plan['revision'], self.source / name, expected, origin='planner'))
        return plan

    def item(self):
        return specification.issue_item(self.session.load())

    def plan(self):
        return self.item()['source']['plan']

    def transport(self, argv, **kwargs):
        self.commands.append(argv)
        prompt = kwargs['input_bytes'].decode()
        self.prompts.append(prompt)
        context = json.loads(prompt[prompt.index('{"approved_specification"'):])
        self.read.append(Path(context['approved_specification']['file']).read_bytes())
        provider = argv[0]
        identity = self.identity
        for flag in ('--session-id', '--resume'):
            if flag in argv:
                identity = argv[argv.index(flag) + 1]
        event = {'type': 'thread.started', 'thread_id': identity} if provider == 'codex' else {
            'type': 'system', 'subtype': 'init', 'session_id': identity}
        kwargs['on_line']('stdout', json.dumps(event))
        value = {'status': 'ready', 'summary': 'Tested complete candidate', 'checks': ['Focused check passed'],
                 'questions': [], 'findings': []}
        Path(kwargs['stdout_path']).with_name('result.json').write_text(json.dumps(value))
        if getattr(self, 'during', None):
            self.during()
        return SimpleNamespace(returncode=0, events=[{'type': 'turn.completed'}] if provider == 'codex' else [
            {'type': 'result', 'subtype': 'success', 'structured_output': value}])

    def invoke(self, provider='codex'):
        self.batch['settings']['fix']['provider'] = provider
        with patch('pr_review.issue_harness.run', side_effect=self.transport):
            return REAL_INVOKE(self.session, self.state, self.batch, self.plan(), [])


class HandoffTest(HandoffCase):
    def test_both_providers_are_handed_the_identical_readable_markdown_file(self):
        reads = {}
        for provider in ('codex', 'claude'):
            self.invoke(provider)
            context = json.loads(self.prompts[-1][self.prompts[-1].index('{"approved_specification"'):])
            handle = context['approved_specification']
            self.assertEqual('markdown', handle['format'])
            self.assertTrue(handle['file'].endswith('.md'))
            self.assertEqual(self.plan()['body'].encode('utf-8'), self.read[-1])
            self.assertEqual(len(self.read[-1]), handle['bytes'])
            reads[provider] = (self.read[-1], handle['sha256'], handle['file'])
        self.assertEqual(reads['codex'], reads['claude'])
        self.assertEqual((self.source / 'revision-1.md').read_bytes(), reads['codex'][0])

    def test_the_prompt_separates_provenance_and_discussion_from_the_specification(self):
        self.invoke()
        prompt = self.prompts[-1]
        context = json.loads(prompt[prompt.index('{"approved_specification"'):])
        self.assertNotIn('Export résumé', prompt)
        self.assertNotIn('Round trip keeps', prompt)
        feedback = next(i for i in context['selected_feedback'] if i['kind'] == 'issue_plan')
        self.assertNotIn('comments', feedback)
        self.assertEqual({'revision', 'digest'}, set(feedback['source']['plan']))
        self.assertIn('issue_context', context)

    def test_the_file_is_outside_the_worktree_and_git_directory(self):
        self.invoke()
        path = Path(json.loads(self.prompts[-1][self.prompts[-1].index('{"approved_specification"'):])['approved_specification']['file'])
        for root in (self.state['worktree'], self.session.repo.common):
            self.assertFalse(path.resolve().is_relative_to(Path(root).resolve()))

    def test_large_documents_are_handed_over_complete(self):
        body = ('## Outcome\n' + 'Behavior détail ✓.\n' * 3000 + '\n## Scope\nS\n\n## Approach\nA\n\n## Acceptance scenarios\nX\n\n'
                '## Verification\nV\n\nAcceptance criteria:\n- LAST CRITERION')
        self.install({'revision': 2, 'body': body, 'acceptance': ['LAST CRITERION']}, 'revision-2.md')
        self.assertEqual(2, self.plan()['revision'])
        self.state = self.session.load()
        self.invoke()
        self.assertEqual(body.encode('utf-8'), self.read[-1])
        self.assertLess(len(self.prompts[-1]), 5000)

    def test_a_missing_or_modified_copy_stops_before_a_provider_starts(self):
        located = Path(self.item()['source']['specification']['path'])
        original = located.read_bytes()
        for label, damage in (('modified', lambda: (located.chmod(0o600), located.write_bytes(original + b' '))),
                              ('missing', lambda: located.unlink())):
            with self.subTest(label), patch('pr_review.issue_harness.run') as launch:
                damage()
                with self.assertRaises(ApiError) as caught:
                    REAL_INVOKE(self.session, self.state, self.batch, self.plan(), [])
                self.assertEqual('specification_' + label, caught.exception.code)
                self.assertIn(str(located), str(caught.exception))
                launch.assert_not_called()
                located.parent.mkdir(exist_ok=True)
                located.write_bytes(original)
                located.chmod(0o400)

    def test_a_document_changed_by_the_provider_is_detected_after_its_turn(self):
        located = Path(self.item()['source']['specification']['path'])

        def tamper():
            located.chmod(0o600)
            located.write_text('weakened requirements', encoding='utf-8')
        self.during = tamper
        with self.assertRaises(ApiError) as caught:
            self.invoke()
        self.assertEqual('specification_modified', caught.exception.code)
        self.assertEqual('failed', self.session.load()['batches'][-1]['attempts'][-1]['status'])

    def test_changed_location_or_metadata_continues_the_same_implementation_session(self):
        self.invoke('codex')
        self.assertNotIn('resume', self.commands[0])
        data = self.session.load()
        item = specification.issue_item(data)
        old = Path(item['source']['specification']['path'])
        new = old.with_name('relocated-copy.md')
        shutil.copy(old, new)
        new.chmod(0o400)
        item['source']['specification'] = {**item['source']['specification'], 'path': str(new), 'origin': 'moved', 'extra': 1}
        old.chmod(0o600)
        old.unlink()
        self.session.save(data)
        self.state = self.session.load()
        self.invoke('codex')
        self.assertEqual(['codex', 'exec', 'resume'], self.commands[1][:3])
        self.assertEqual(1, len(self.session.load()['issue_harness_sessions']))
        self.assertEqual(self.read[0], self.read[1])

    def test_reimporting_with_a_new_location_never_marks_work_stale(self):
        before = self.item()
        moved = self.source / 'moved.md'
        plan = before['source']['plan']
        expected = specification.write(moved, plan['body'])
        owner_import.from_issue(self.session, plan, {'title': 'Provider slots', 'html_url': 'https://github.com/org/repo/issues/12'},
                                'workflow', None, specification.locator(plan['revision'], moved, expected, origin='planner'))
        data = self.session.load()
        after = specification.issue_item(data)
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(before.get('versions', []), after.get('versions', []))
        self.assertFalse(any(b.get('discussion_stale') for b in data['batches']))
        self.assertEqual(plan['body'].encode('utf-8'), Path(after['source']['specification']['path']).read_bytes())

    def test_changed_content_is_a_new_item_version_and_restarts_work(self):
        before = self.item()
        revised = {**before['source']['plan'], 'revision': 2, 'body': before['source']['plan']['body'] + '\n- New requirement'}
        self.install(revised, 'revision-2.md')
        data = self.session.load()
        after = specification.issue_item(data)
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(len(before.get('versions', [])) + 1, len(after['versions']))
        self.assertTrue(any(b.get('discussion_stale') for b in data['batches']))

    def test_a_modified_workflow_document_is_never_imported(self):
        revised = {**self.plan(), 'revision': 2, 'body': 'Changed'}
        revised['digest'] = digest('Changed')
        expected = specification.write(self.source / 'r2.md', 'Changed')
        (self.source / 'r2.md').chmod(0o600)
        (self.source / 'r2.md').write_text('Not what was approved')
        with self.assertRaises(ApiError) as caught:
            owner_import.from_issue(self.session, revised, {'title': 't', 'html_url': 'u'}, 'workflow', None,
                                    specification.locator(2, self.source / 'r2.md', expected))
        self.assertEqual('specification_modified', caught.exception.code)
        self.assertEqual(1, self.plan()['revision'])

    def test_acceptance_review_receives_the_same_document(self):
        adapter = acceptance.IssueAdapter(self.session, self.state, self.batch, self.plan())
        context = adapter.reviewer_context()
        self.assertEqual(self.plan()['body'], context['approved_specification']['document'])
        self.assertEqual(self.read_text(), context['approved_specification']['document'])
        self.assertEqual(digest(self.plan()['body']), self.plan()['digest'])
        feedback = next(i for i in context['selected_feedback'] if i['kind'] == 'issue_plan')
        self.assertNotIn('comments', feedback)

    def read_text(self):
        return Path(self.item()['source']['specification']['path']).read_text(encoding='utf-8')

    def test_acceptance_review_stops_on_a_modified_document(self):
        Path(self.item()['source']['specification']['path']).chmod(0o600)
        Path(self.item()['source']['specification']['path']).write_text('x')
        adapter = acceptance.IssueAdapter(self.session, self.state, self.batch, self.plan())
        with self.assertRaises(ApiError) as caught:
            adapter.reviewer_context()
        self.assertEqual('specification_modified', caught.exception.code)

    def test_execution_boundary_has_no_controller_tasks_or_checkpoints(self):
        self.assertEqual(('session', 'state', 'batch', 'specification', 'feedback'), REAL_INVOKE.__code__.co_varnames[:5])
        self.invoke()
        record = self.session.load()['batches'][-1]
        self.assertEqual(['approved-specification'], [t['id'] for t in record['tasks']])
        self.assertNotIn('commit_plan_tasks', record)
        self.assertFalse([k for k in record if k.startswith('task_') or k.endswith('_approval')])


class ResumptionTest(HandoffCase):
    autostart = False

    def test_resumption_verifies_the_document_before_any_provider_turn(self):
        located = Path(self.item()['source']['specification']['path'])
        with patch('pr_review.issue_harness.invoke', side_effect=ApiError('model_failed', 'interrupted')):
            with self.assertRaises(ApiError):
                self.f.start(issue_harness=True)
        unfinished = self.session.load()['batches'][-1]
        self.assertNotEqual('ready', unfinished['status'])
        located.chmod(0o600)
        located.write_bytes(b'tampered')
        with patch('pr_review.issue_harness.invoke') as turn:
            with self.assertRaises(ApiError) as caught:
                self.f.start(batch=unfinished['id'], issue_harness=True)
            self.assertEqual('specification_modified', caught.exception.code)
            turn.assert_not_called()

    def test_an_intact_document_resumes_the_unfinished_batch(self):
        with patch('pr_review.issue_harness.invoke', side_effect=ApiError('model_failed', 'interrupted')):
            with self.assertRaises(ApiError):
                self.f.start(issue_harness=True)
        unfinished = self.session.load()['batches'][-1]
        view = self.f.start(batch=unfinished['id'], issue_harness=True)
        self.assertEqual(unfinished['id'], view['batches'][-1]['id'])
        self.assertEqual('ready', view['batches'][-1]['status'])


if __name__ == '__main__':
    unittest.main()
