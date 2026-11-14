"""Publication workflow owns retries and journals adapter operations independently."""
import copy
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from pr_review import publication
from pr_review.common import ApiError, digest
from pr_review.receipts import evidence


class PublicationAdapterTest(unittest.TestCase):
    def setUp(self):
        self.session = SimpleNamespace(manifest={'repository':'github.com/org/repo'}, save=Mock(), blockers=lambda *_: [], view=lambda state: state)
        self.adapter = SimpleNamespace(lookup=Mock(), submit=Mock(), matches=lambda *args: True,
                                       comments=lambda session, op, receipt, state: {},
                                       evidence=lambda op: dict(url=op['result']['url'], comment_ids=[op['result']['id']], review_ids=[]))
        self.operation = dict(id='one', kind='reply', status='ambiguous', comment_ids=['c'], event='COMMENT')
        self.action = dict(id='logical', kind='reply', status='ambiguous', event='COMMENT', comment_ids=['c'], operations=[copy.deepcopy(self.operation), {**self.operation, 'id':'two'}])
        self.state = dict(comments=[{'id':'c','status':'staged'}], batches=[dict(actions=[self.action])], verdict_submitted=False)
        self.writes = []
        self.patches = [patch.object(publication, 'publication_adapter', return_value=self.adapter),
                        patch.object(publication, 'persist_action', side_effect=lambda *args: self.writes.append(copy.deepcopy(self.action)))]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)

    def test_partial_recovery_never_resends_and_preserves_operation_receipts(self):
        self.adapter.lookup.side_effect = [[dict(id='remote-one',url='u1')], []]
        publication.recover(self.session, self.state)
        self.assertEqual(['published','failed'], [o['status'] for o in self.action['operations']])
        self.assertEqual('staged', self.state['comments'][0]['status'])
        self.assertEqual(['u1'], [e['url'] for e in evidence([self.action])])
        self.adapter.lookup.side_effect = [[dict(id='remote-two',url='u2')]]
        publication.recover(self.session, self.state)
        self.assertEqual('published', self.action['status'])
        self.assertEqual('published', self.state['comments'][0]['status'])
        self.adapter.submit.assert_not_called()
        self.assertTrue(any(o['operations'][0].get('result') and o['operations'][0]['status'] != 'published' for o in self.writes))

    def test_parent_receipt_survives_missing_child_evidence(self):
        self.adapter.comments = Mock(side_effect=ApiError('incomplete_receipts','Missing'))
        receipt = dict(id='accepted',url='url')
        with self.assertRaises(ApiError):
            publication.verify_operation(self.session,self.state,self.action,self.action['operations'][0],receipt)
        self.assertEqual(receipt,self.writes[-1]['operations'][0]['result'])
        self.assertNotEqual('published',self.action['operations'][0]['status'])

    def test_duplicate_receipts_never_trigger_submission(self):
        self.adapter.lookup.return_value = [dict(id='a'),dict(id='b')]
        with self.assertRaises(ApiError) as caught:
            publication.recover(self.session,self.state)
        self.assertEqual('duplicate_receipts',caught.exception.code)
        self.adapter.submit.assert_not_called()

    def test_partial_publish_skips_confirmed_operation(self):
        first = self.action['operations'][0]
        first.update(status='published',result=dict(id='accepted',url='u1'),comment_results={},evidence=dict(url='u1',comment_ids=['accepted'],review_ids=[]))
        second = self.action['operations'][1]
        second['status']='draft'
        batch=dict(actions=[self.action],review_status='stable')
        self.state.update(prepared=batch,batches=[])
        self.adapter.submit.return_value=dict(id='new',url='u2')
        with patch.object(publication,'refresh',return_value=[]), patch.object(publication,'repository_operation'), patch('pr_review.workflow.status_fingerprint',return_value='stable'), patch('pr_review.suggestions.blockers',return_value=[]):
            result=publication.publish(self.session,self.state,{'confirmation':digest(batch)})
        self.assertTrue(result['complete'])
        self.assertEqual(1,self.adapter.submit.call_count)
        self.assertEqual('two',self.adapter.submit.call_args.args[1]['id'])

    def test_missing_verdict_capability_is_a_structured_error(self):
        from pr_review.commands import mutate
        session = SimpleNamespace(verdicts=lambda state: [])
        with self.assertRaises(ApiError) as caught:
            mutate(session, {}, {'operation': 'verdict', 'verdict': 'APPROVE'})
        self.assertEqual('verdict_unavailable', caught.exception.code)

    def test_adapter_rendering_reaches_preview_without_editing_human_text(self):
        comment = {'body': 'Human text'}
        forge = SimpleNamespace(capabilities={'publication'}, publication=SimpleNamespace(render=lambda value: value['body'] + '\nRendered suffix'))
        with patch('pr_review.forge.adapter', return_value=forge), patch('pr_review.forge.restore', return_value=SimpleNamespace(forge='test')):
            self.assertEqual('Human text\nRendered suffix', publication.render_comment(self.session, comment))
        self.assertEqual({'body': 'Human text'}, comment)

    def test_github_duplicate_inline_receipts_are_not_arbitrarily_selected(self):
        from pr_review.forge_github_publication import GitHubPublication
        operation = dict(kind='review', payload={'comments': [{}]}, comment_markers={'c': 'marker'}, viewer='reviewer', comment_ids=['c'])
        receipt = dict(id=42)
        values = [dict(id=i, user={'login':'reviewer'}, body='marker', pull_request_review_id=42) for i in (1,2)]
        state = {'comments':[dict(id='c',placement={'kind':'inline'})]}
        with patch.object(publication,'gh',return_value=values), self.assertRaises(ApiError) as caught:
            GitHubPublication().comments(self.session,operation,receipt,state)
        self.assertEqual('duplicate_receipts',caught.exception.code)

    def test_unknown_operation_version_cannot_send(self):
        from pr_review.forge_github_publication import GitHubPublication
        with patch.object(publication,'gh',side_effect=AssertionError('Remote request')), self.assertRaises(ApiError):
            GitHubPublication().submit(self.session,{'version':2})

    def test_stop_between_operations_remains_resumable_without_reposting(self):
        for op in self.action['operations']:
            op['status'] = 'draft'
        batch = dict(actions=[self.action], review_status='stable')
        self.state.update(prepared=batch,batches=[])
        self.adapter.submit.return_value=dict(id='first',url='u1')
        with patch.object(publication,'refresh',side_effect=[[],[],['changed']]), patch.object(publication,'repository_operation'), patch('pr_review.workflow.status_fingerprint',return_value='stable'), patch('pr_review.suggestions.blockers',return_value=[]):
            result=publication.publish(self.session,self.state,{'confirmation':digest(batch)})
        self.assertFalse(result['complete'])
        self.assertEqual('failed',self.action['status'])
        self.assertEqual(['published','draft'],[op['status'] for op in self.action['operations']])
        publication.recover(self.session,self.state)
        self.adapter.lookup.assert_not_called()
        self.assertEqual('failed',self.action['status'])
        resumed = dict(id='second-approval',actions=[copy.deepcopy(self.action)],review_status='stable')
        self.state['prepared']=resumed
        self.adapter.submit.return_value=dict(id='second',url='u2')
        with patch.object(publication,'refresh',return_value=[]), patch.object(publication,'repository_operation'), patch('pr_review.workflow.status_fingerprint',return_value='stable'), patch('pr_review.suggestions.blockers',return_value=[]):
            result=publication.publish(self.session,self.state,{'confirmation':digest(resumed)})
        self.assertTrue(result['complete'])
        self.assertEqual(['one','two'],[c.args[1]['id'] for c in self.adapter.submit.call_args_list])
        self.assertEqual(1,sum(len(b['actions']) for b in self.state['batches']))
        self.assertEqual('published',self.action['status'])

    def test_partial_action_cannot_be_replaced_after_edit(self):
        self.action['status']='failed'
        self.action['operations'][0].update(status='published',result=dict(id='first',url='u1'))
        self.state.update(verdict='COMMENT')
        candidate={**copy.deepcopy(self.action),'id':'replacement'}
        self.adapter.retry_equivalent=lambda *_: False
        with patch.object(publication,'recover'), patch.object(publication,'refresh',return_value=[]), patch.object(publication,'repository_operation'), patch.object(publication,'make_actions',return_value=[candidate]):
            result=publication.prepare(self.session,self.state)
        self.assertFalse(result['ready'])
        self.assertIn('unchanged',result['reason'])
        from pr_review.commands import mutable
        with self.assertRaises(ApiError):
            mutable(self.state['comments'][0],self.state)
        mutable(self.state['comments'][0],self.state,unchanged=True)

    def test_versioned_evidence_never_silently_uses_github(self):
        action={'status':'published','version':1,'kind':'reply','result':{'id':'opaque'}}
        with self.assertRaises(ApiError):
            list(evidence([action]))
        fake=SimpleNamespace(evidence=lambda _: dict(url='url',comment_ids=['opaque'],review_ids=[]))
        self.assertEqual(['opaque'],list(evidence([action],fake))[0]['comment_ids'])

    def test_gitlab_automation_reporting_does_not_require_publication_adapter(self):
        from pr_review.automation import AutomaticRun
        run=AutomaticRun({'operation_id':'test','log_path':'unused'})
        run.store=SimpleNamespace(manifest={'repository':'gitlab.com/group/repo','forge_project':{'forge':'gitlab','host':'gitlab.com','path':'group/repo'}})
        run.save=Mock()
        result=run.finish('needs_attention','Read-only pilot',{'comments':[],'batches':[]})
        self.assertEqual([],result['receipts'])

    def test_partial_action_blocks_verdict_change_and_unrelated_submission(self):
        from pr_review.commands import mutate
        self.action['status']='failed'
        self.action['operations'][0]['status']='published'
        self.state.update(verdict='COMMENT')
        with self.assertRaises(ApiError) as caught:
            mutate(self.session,self.state,{'operation':'verdict','verdict':'APPROVE'})
        self.assertEqual('partial_publication',caught.exception.code)
        self.assertEqual('COMMENT',self.state['verdict'])
        self.state['comments'][0]['status']='unstaged'
        unrelated={**copy.deepcopy(self.action),'id':'unrelated','comment_ids':['other']}
        with patch.object(publication,'recover'), patch.object(publication,'refresh',return_value=[]), patch.object(publication,'repository_operation'), patch.object(publication,'make_actions',return_value=[unrelated]):
            result=publication.prepare(self.session,self.state)
        self.assertFalse(result['ready'])
        self.assertIn('logical',result['reason'])
        self.assertIn('comments: c',result['reason'])

    def test_unsupported_suggestion_fails_before_preparation(self):
        self.adapter.suggestions=False
        self.adapter.prepare=Mock(side_effect=AssertionError('Prepared unsupported suggestion'))
        state={'comments':[{'status':'staged','suggestion':{'replacement':'x'},'placement':{'kind':'inline'}}]}
        with self.assertRaises(ApiError) as caught:
            publication.make_actions(self.session,state)
        self.assertEqual('unsupported_operation',caught.exception.code)
        self.adapter.prepare.assert_not_called()
