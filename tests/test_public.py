import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import tournament as t
from api_agents import roster
from api_budget import Budget, BudgetExceeded
from prospective import task_contract, mock_draft
from research_v6.scheduler import CoordinatorRequired
from research_v7.runner import TournamentV7
from research_v7.contracts import TECH_SCHEMA
from research_v7.decisions import verify_improvement
from tournament_v7 import contract

ROOT=Path(__file__).resolve().parents[1]

def make_runner(path, mocked=True):
    config=t.read(ROOT/'config.v7.json')
    evidence=t.read(ROOT/'examples/synthetic/evidence.json')
    literature=t.read(ROOT/'examples/synthetic/literature.json')
    t.save(path/'evidence.lock.json',evidence)
    t.save(path/'literature.lock.json',literature)
    t.save(path/'manifest.json',{'config':config,'evidence':evidence,'literature':literature,
        'mock':mocked,'task_contract':task_contract(),'agent_roster':roster(config),'v7_contract':contract(config)})
    return TournamentV7(config,path,mocked)

class PublicTests(unittest.TestCase):
    def test_six_mock_rounds_and_cached_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); r=make_runner(p); r.cycle()
            before={f.name:f.read_bytes() for f in (p/'jobs').glob('*.json')}
            make_runner(p).cycle()
            self.assertEqual(before,{f.name:f.read_bytes() for f in (p/'jobs').glob('*.json')})
            self.assertEqual(t.read(p/'completion.json')['rounds'],6)
            self.assertTrue(t.read(p/'completion.json')['mock'])
            self.assertFalse((p/'budget.json').exists())
    def test_quote_punctuation_does_not_eliminate(self):
        criteria=[{'id':'W1','requirement':'Add held-out validation'}]
        review={'substantial':True,'criteria':[{'criterion_id':'W1','met':True,'evidence_quote':'“held-out workloads”','reason':'Added discriminator'}]}
        self.assertTrue(verify_improvement(review,criteria,{'control':'Compare held-out workloads'})[0])
        with self.assertRaises(ValueError):verify_improvement(review,criteria,{'control':'different text'})
    def test_repeated_invalid_output_escalates_and_coordinator_fix_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);r=make_runner(p,False)
            with mock.patch.object(r,'api',return_value=({},{})) as api:
                with self.assertRaises(CoordinatorRequired):r.invoke('r1-format-test','technical',0,{},TECH_SCHEMA)
            self.assertEqual(api.call_count,4)
            self.assertFalse((p/'jobs/r1-format-test.json').exists())
            self.assertTrue((p/'coordinator_handoffs/r1-format-test.json').exists())
            t.save(p/'coordinator_fixes/r1-format-test.json',{'reason':'Incomplete fix','output':{}})
            with self.assertRaises(ValueError):r.invoke('r1-format-test','technical',0,{},TECH_SCHEMA)
            output={'decision':'revise','issues':[],'summary':'Preserve unfavorable judgment'}
            t.save(p/'coordinator_fixes/r1-format-test.json',{'reason':'Restore required fields','output':output})
            with mock.patch.object(r,'api',side_effect=AssertionError('No paid retry')):
                self.assertEqual(r.invoke('r1-format-test','technical',0,{},TECH_SCHEMA),output)
    def test_actual_payload_budget_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);r=make_runner(p);r.config['budget_usd']=0.001
            payload={'model':'gpt-6-luna','input':[{'role':'user','content':'x'*30000}],'text':{},'max_output_tokens':100}
            r.validate_payload('packet',payload,{'input_byte_limit':100})
            with self.assertRaises(BudgetExceeded):r.budget.reserve('packet',payload,r.config['pricing_profiles']['gpt-6-luna'])
    def test_pending_billing_is_reserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'budget_usd':0.01,'pricing':{'model':'example','input_per_million':1,'output_per_million':1,'input_safety_multiplier':1}}
            b=Budget(tmp,config);p={'model':'example','input':[],'text':{},'max_output_tokens':1000}
            b.reserve('one',p)
            with self.assertRaises(BudgetExceeded):b.reserve('two',p)
    def test_real_calls_disabled_by_default(self):
        self.assertFalse(t.read(ROOT/'config.v7.json')['allow_paid_run'])

if __name__=='__main__':unittest.main()
