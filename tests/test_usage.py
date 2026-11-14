import json
import unittest

from loop.usage import agent_usage, combine, normalize, record, role_usage


class TokenUsageTests(unittest.TestCase):
    def test_codex_cached_tokens_are_a_subset_of_input(self):
        usage=normalize('codex',{'input_tokens':100,'cached_input_tokens':70,'output_tokens':20})
        self.assertEqual(usage['total_tokens'],120)
        self.assertEqual(usage['input_tokens'],100)
        self.assertEqual(usage['cached_tokens'],70)

    def test_claude_separate_cache_buckets_are_counted_once(self):
        usage=normalize('claude',{'input_tokens':10,'cache_read_input_tokens':30,
                                  'cache_creation_input_tokens':5,'output_tokens':7})
        self.assertEqual(usage['input_tokens'],45)
        self.assertEqual(usage['total_tokens'],52)
        self.assertEqual(usage['cached_tokens'],30)
        self.assertEqual(usage['cache_creation_tokens'],5)
        self.assertEqual(normalize('muse',{'total_tokens':44})['total_tokens'],44)

    def test_missing_usage_is_unknown_and_explicit_zero_is_known(self):
        state={}
        record(state,'worker','codex',{},'a')
        summary=role_usage(state,'worker')
        self.assertIsNone(summary['total_tokens'])
        self.assertIsNone(summary['input_tokens'])
        self.assertEqual(summary['unreported_calls'],1)
        self.assertTrue(summary['partial'])
        record(state,'worker','codex',{'input_tokens':0,'output_tokens':0},'b')
        self.assertEqual(role_usage(state,'worker')['total_tokens'],0)
        self.assertIsNone(normalize('codex',{'input_tokens':True})['input_tokens'])

    def test_persistence_dedup_resumed_sessions_and_model_switches(self):
        state={}
        record(state,'worker','codex',{'input_tokens':100,'output_tokens':20},'attempt-1','model-a',now=1)
        state=json.loads(json.dumps(state))
        record(state,'worker','codex',{'input_tokens':100,'output_tokens':20},'attempt-1','model-a',now=2)
        state['session_id']=None
        record(state,'worker','codex',{'input_tokens':30,'output_tokens':5},'attempt-2','model-b',now=3)
        record(state,'worker','claude',{'input_tokens':10,'output_tokens':2},'attempt-3','model-c',now=4)
        summary=role_usage(state,'worker')
        self.assertEqual(summary['total_tokens'],167)
        self.assertEqual(summary['reported_calls'],3)
        self.assertEqual(len(summary['by_model']),3)
        self.assertEqual(sum(row['total_tokens'] for row in summary['by_model']),167)
        self.assertEqual(sum(row['total_tokens'] for row in summary['by_provider']),167)

    def test_legacy_totals_preserved_without_fabricating_old_breakdowns(self):
        state={'tokens':{'codex':50},'cost':{'worker:codex':{'calls':2}}}
        record(state,'worker','codex',{'input_tokens':8,'output_tokens':2},'new','new-model')
        summary=role_usage(state,'worker')
        self.assertEqual(summary['total_tokens'],60)
        self.assertEqual(summary['input_tokens'],8)
        self.assertEqual(summary['historical_tokens'],50)
        self.assertEqual(summary['unreported_calls'],2)
        self.assertEqual(sum(row['total_tokens'] for row in summary['by_provider']),60)
        self.assertEqual(sum(row['total_tokens'] for row in summary['by_model']),60)
        self.assertTrue(summary['partial'])

    def test_shared_supervisor_totals_do_not_include_agent_tokens_twice(self):
        agents=[]
        for index in range(2):
            state={}
            record(state,'worker','codex',{'input_tokens':100,'output_tokens':20},f'w{index}','worker')
            record(state,'reviewer','claude',{'input_tokens':10,'output_tokens':5},f'r{index}')
            record(state,'supervisor','codex',{'input_tokens':30,'output_tokens':10},f's{index}','supervisor')
            agents.append(agent_usage(state,{}))
        worker_total=combine(agent['agent'] for agent in agents)
        supervisor_total=combine(agent['supervisor'] for agent in agents)
        grand=combine([worker_total,supervisor_total])
        self.assertEqual(worker_total['total_tokens'],270)
        self.assertEqual(supervisor_total['total_tokens'],80)
        self.assertEqual(grand['total_tokens'],350)
        self.assertEqual(len(grand['by_provider']),2)
        self.assertEqual(sum(row['total_tokens'] for row in grand['by_model']),350)


if __name__=='__main__':
    unittest.main()
