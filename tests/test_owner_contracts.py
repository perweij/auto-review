"""Contract coverage must never substitute for independent verification."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_issue_steps
from test_owner import REAL_INVOKE
from pr_review import owner_contracts as contracts, owner_providers, owner_completion, owner_git, owner_publication
from pr_review.common import ApiError


def case(identity='explicit', intent='preserve'):
    return {'id': identity, 'scenario': 'Explicit choice competes with a default',
            'expected': 'Explicit choice wins', 'basis': 'Approved precedence requirement', 'intent': intent}


def result(required, status='tested', complete=True):
    return {'complete': complete, 'blocking': [] if complete else ['Evidence unavailable'],
            'case_results': [{'id': c['id'], 'status': status, 'evidence': 'Focused test command passed'} for c in required]}


class ContractTest(unittest.TestCase):
    def test_cases_distinguish_preserved_and_intentional_changes(self):
        self.assertEqual(2, len(contracts.cases([case(),case('new','change')])))
        for invalid in ([case(),case()], [{**case(),'basis':''}], [case(str(i)) for i in range(9)]):
            with self.assertRaises(ApiError): contracts.cases(invalid)

    def test_missing_duplicate_and_unknown_evidence_cannot_pass(self):
        cases = [case()]
        for rows in (None, [], [result(cases)['case_results'][0]]*2,
                     [{'id':'other','status':'tested','evidence':'pass'}], [{'id':None}]):
            with self.subTest(rows=rows), self.assertRaises(ApiError):
                contracts.bind({'complete': True,'blocking': [],'case_results':rows}, cases, 'tree')

    def test_failed_and_unavailable_evidence_are_not_success(self):
        for status in ('failed','unavailable'):
            with self.assertRaises(ApiError): contracts.bind(result([case()],status), [case()], 'tree')
            value = contracts.bind(result([case()],status,False), [case()], 'tree')
            self.assertFalse(contracts.verified(value,[case()],'tree'))
        mixed = contracts.bind({**result([case()],'unavailable',False),
                                'assessment':'changes_requested'},[case()],'tree')
        self.assertFalse(contracts.verified(mixed, [case()], 'tree'))

    def test_confirmed_defects_survive_unavailable_coverage(self):
        required = [case(), case('pending')]
        report = result(required, complete=False)
        report.update(assessment='changes_requested', blocking=['Wrong provider selected'])
        report['case_results'][0]['status'] = 'failed'
        report['case_results'][1]['status'] = 'unavailable'
        bound = contracts.bind(report, required, 'tree')
        self.assertEqual(['Wrong provider selected'], bound['blocking'])
        self.assertTrue(contracts.matches(bound, required, 'tree'))
        self.assertFalse(contracts.verified(bound, required, 'tree'))

    def test_identity_binds_case_expectations_and_source(self):
        value = contracts.bind(result([case()]),[case()],'tree')
        self.assertTrue(contracts.matches(value,[case()],'tree'))
        self.assertFalse(contracts.matches(value,[{**case(),'expected':'default wins'}],'tree'))
        self.assertFalse(contracts.matches(value,[case()],'changed-tree'))
        self.assertFalse(contracts.matches(value,[],'tree'))

    def test_fixer_can_add_but_cannot_rewrite_cases(self):
        task = {'verification_cases':[case()]}
        added = case('other','change')
        report = {**result([case(),added]),'verification_cases':[added]}
        contracts.implementation(task,report,'tree')
        self.assertEqual(2,len(task['verification_cases']))
        with self.assertRaises(ApiError):
            contracts.implementation(task,{**report,'verification_cases':[{**case(),'expected':'changed'}]},'tree')

    def test_legacy_proofs_remain_readable_without_fabricated_evidence(self):
        report = {'complete':True,'blocking':[]}
        self.assertTrue(contracts.matches(report,[],'tree'))
        self.assertNotIn('case_contract',contracts.bind(report,[],'tree'))
        self.assertFalse(contracts.matches(report,[case()],'tree'))

    def test_combined_case_ids_disambiguate_tasks(self):
        tasks = [{'id':t,'status':'complete','verification_cases':[case()]} for t in ['p1-a','p1-b']]
        self.assertEqual(['p1-a/explicit','p1-b/explicit'],[c['id'] for c in contracts.combined(tasks)])

    def test_provider_schemas_use_same_cases_and_evidence(self):
        for role in ('plan','fix','review'):
            schema = owner_providers.output_schema(role,['issue:key'])
            self.assertFalse(schema['additionalProperties'])
            if role == 'plan':
                self.assertIn('verification_cases',schema['properties']['tasks']['items']['required'])
            else:
                self.assertIn('case_results',schema['required'])
        self.assertEqual(['issue:key'],owner_providers.output_schema('fix',['issue:key'])['properties']['outcomes']['items']['properties']['item']['enum'])


class ContractWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.fixture = test_issue_steps.IssueStepsTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        original = self.fixture.model
        def model(session,state,batch,role,context):
            report = original(session,state,batch,role,context)
            if role == 'plan':
                for task in report['tasks']:task['verification_cases']=[case()]
            else:
                required = context.get('verification_cases',[])
                report.update(result(required,complete=report['complete']))
                if role == 'fix': report['verification_cases']=[]
            return report
        p=patch('pr_review.owner_providers.invoke',side_effect=model)
        self.provider=p.start();self.addCleanup(p.stop)

    def test_evidence_flows_to_final_verification_and_changed_cases_block_commit(self):
        view=self.fixture.start()
        batch=view['batches'][-1]
        self.assertTrue(contracts.current(batch))
        self.assertEqual(2,len(batch['review']['case_results']))
        data=self.fixture.owner.load()
        data['batches'][-1]['tasks'][0]['verification_cases'][0]['expected']='A changed requirement'
        self.fixture.owner.save(data)
        prepared=self.fixture.fixture.request('owner-prepare-commit')
        self.assertFalse(prepared['ready'])
        self.assertIn('Verification cases',prepared['reason'])
        self.assertFalse(owner_completion.execution_complete(data['batches'][-1]))
        invalid=data['batches'][-1]
        reply={'batch':invalid['id'],'source':'thread','fingerprint':'same','body':'Fixed'}
        item={'kind':'thread','fingerprint':'same'}
        session=SimpleNamespace(items=lambda state,keys:[item])
        with self.assertRaisesRegex(ApiError,'Verification cases'):
            owner_publication.blockers(session,data,reply,'remote-head')
        self.assertFalse(owner_publication.automatic_proof(session,data,reply))

    def test_paused_step_retains_case_contract_and_resume_verifies_it(self):
        self.fixture.blocked_task='pipeline'
        view=self.fixture.start()
        batch=view['batches'][-1]
        self.assertEqual('paused',batch['status'])
        self.assertEqual([case()],batch['tasks'][1]['verification_cases'])
        before=copy.deepcopy(batch['tasks'][0])
        self.fixture.blocked_task=None
        view=self.fixture.fixture.request('owner-fix',batch=batch['id'])
        self.assertEqual(before['review'],view['batches'][-1]['tasks'][0]['review'])
        self.assertEqual(before['after'],view['batches'][-1]['tasks'][0]['after'])
        self.assertTrue(contracts.current(view['batches'][-1]))

    def test_incomplete_case_coverage_cannot_reach_commit(self):
        original=self.provider.side_effect
        def missing(*args):
            value=original(*args)
            if args[3]=='review':value['case_results']=[]
            return value
        self.provider.side_effect=missing
        with self.assertRaises(ApiError):self.fixture.start(auto_commit=True)
        data=self.fixture.owner.load();batch=data['batches'][-1]
        self.assertEqual(batch['baseline'],self.fixture.fixture.git('rev-parse','HEAD',cwd=data['worktree']))
        self.assertFalse(self.fixture.fixture.posts)

    def test_repair_preserves_cases_and_consumed_budget_on_rejection(self):
        original=self.provider.side_effect
        seen=[]
        def rejection(session,state,batch,role,context):
            value=original(session,state,batch,role,context)
            if role=='review':
                value['blocking']=['Concrete regression remains']
                value['case_results'][0]['status']='failed'
            if role=='fix' and 'review_findings' in context:
                seen.append(copy.deepcopy(context))
            return value
        self.provider.side_effect=rejection
        view=self.fixture.start(auto_commit=True)
        batch=view['batches'][-1]
        self.assertEqual('paused',batch['status'])
        self.assertEqual(1,batch['tasks'][0]['repairs'])
        self.assertEqual(1,len(seen))
        self.assertEqual([case()],seen[0]['verification_cases'])
        self.assertEqual('failed',seen[0]['review_findings']['case_results'][0]['status'])
        self.assertEqual(batch['baseline'],self.fixture.fixture.git('rev-parse','HEAD',cwd=view['worktree']))


    def test_integration_repair_keeps_task_proofs_and_verifies_final_tree(self):
        original=self.provider.side_effect
        repaired=False
        def integration(session,state,batch,role,context):
            nonlocal repaired
            if role=='fix' and context.get('scope'):
                repaired=True
                (Path(state['worktree'])/'config.txt').write_text('Integrated configuration\n')
                batch['checkpoint']=owner_git.checkpoint(session,state,'integration repair')
                return {**result(context['verification_cases']), 'verification_cases':[],
                        'summary':'Repaired interaction','checks':['Focused regression passed'],
                        'outcomes':[{'item':self.fixture.key,'status':'fixed','evidence':'Interaction corrected'}]}
            value=original(session,state,batch,role,context)
            if role=='review' and context.get('scope') and not repaired:
                value['blocking']=['Interaction needs correction']
                value['case_results'][0]['status']='failed'
            return value
        self.provider.side_effect=integration
        view=self.fixture.start(auto_commit=True)
        batch=view['batches'][-1]
        self.assertTrue(repaired)
        self.assertTrue(contracts.current(batch))
        self.assertEqual(3,len(batch['commit_progress']['commits']))
        self.assertEqual(batch['final_tree'],batch['review']['case_tree'])
        self.assertNotEqual(batch['final_tree'],batch['tasks'][0]['review']['case_tree'])
        batch['review']['case_tree']='different tree'
        self.assertFalse(contracts.current(batch))

    def test_interrupted_repair_retains_cases_and_cannot_reset_allowance(self):
        original=self.provider.side_effect
        repair_calls=[]
        def interrupted(session,state,batch,role,context):
            value=original(session,state,batch,role,context)
            if role=='review':
                value['blocking']=['Concrete regression']
                value['case_results'][0]['status']='failed'
            if role=='fix' and 'review_findings' in context:
                repair_calls.append(context)
                raise ApiError('provider_failed','Interrupted repair')
            return value
        self.provider.side_effect=interrupted
        with self.assertRaises(ApiError):self.fixture.start()
        data=self.fixture.owner.load();batch=data['batches'][-1]
        self.assertEqual(1,batch['tasks'][0]['repairs'])
        self.assertEqual([case()],batch['tasks'][0]['verification_cases'])
        with self.assertRaisesRegex(ApiError,'adopt'):
            self.fixture.fixture.request('owner-fix',batch=batch['id'])
        view=self.fixture.fixture.request('owner-fix',batch=batch['id'],adopt=True)
        self.assertEqual('paused',view['batches'][-1]['status'])
        self.assertEqual(1,len(repair_calls))
        self.assertEqual(1,view['batches'][-1]['tasks'][0]['repairs'])

    def test_both_provider_response_paths_preserve_case_evidence(self):
        self.fixture.start()
        state=self.fixture.owner.load();batch=state['batches'][-1]
        required=contracts.combined(batch['tasks'])
        report={**result(required),'summary':'Verified','outcomes':[], 'checks':[]}
        def provider(args,**kwargs):
            directory=Path(kwargs['env']['TMPDIR']).parent
            schema=json.loads((directory/'schema.json').read_text())
            self.assertIn('case_results',schema['required'])
            self.assertIn(required[0]['id'],kwargs['input_bytes'].decode())
            if args[0]=='codex':
                Path(args[args.index('-o')+1]).write_text(json.dumps(report))
                events=[{'type':'turn.completed'}]
            else:
                events=[{'type':'result','subtype':'success','structured_output':report}]
            return SimpleNamespace(returncode=0,events=events)
        for name in ('claude','codex'):
            batch['settings']['review']['provider']=name
            with self.subTest(provider=name),patch('pr_review.owner_providers.run',side_effect=provider):
                response=REAL_INVOKE(self.fixture.owner,state,batch,'review',{'feedback':[], 'verification_cases':required})
            contracts.bind(response,required,batch['checkpoint']['tree'])
            self.assertTrue(contracts.verified(response,required,batch['checkpoint']['tree']))
