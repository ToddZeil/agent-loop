import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from loop.keeper import Scribe
from loop.supervise import Fleet, SupervisedRun, git
from tests.support import isolate_registry


class SuperviseTests(unittest.TestCase):
    def setUp(self):
        isolate_registry(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repos = []
        for name in ('one', 'two'):
            repo = self.root / name
            repo.mkdir()
            git(repo, 'init', '-q')
            git(repo, 'config', 'user.email', 'test@example.invalid')
            git(repo, 'config', 'user.name', 'Test')
            (repo / 'app.py').write_text('value = 1\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-qm', 'initial')
            self.repos.append(repo)
        self.source = self.root / 'tasks.json'
        self.spec = {'max_parallel': 2, 'tasks': [
            {'id': name, 'repo': str(repo), 'goal': 'Set value to 2',
             'acceptance': ["python3 -B -c \"from app import value; assert value == 2\""]}
            for name, repo in zip(('one', 'two'), self.repos)]}
        self.source.write_text(json.dumps(self.spec))
        self.fleet = Fleet(self.root / 'fleet')
        self.fleet.create(self.source)
        self.scribe = mock.patch.object(SupervisedRun, 'scribe', property(lambda _: Scribe()))
        self.scribe.start()
        self.addCleanup(self.scribe.stop)

    def test_unsupported_model_stops_retrying_and_old_wait_is_migrated(self):
        run=self.review_run('bad-model')
        state=run.state();state['attempt']='bad-model'
        message="The 'WrongModel' model is not supported when using Codex with a ChatGPT account."
        run._consume(state,{'status':'configuration_error','error':message})
        self.assertEqual(run.state()['state'],'waiting')
        self.assertEqual(run.state()['retry_at'],0)
        state=run.state();state.update(retry_at=time.time()+1800,wait_reason=message)
        run.save_state(state)
        run.step({'max_turns':100})
        self.assertEqual(run.state()['retry_at'],0)

    def test_follow_up_note_reopens_completed_run_without_resetting_history(self):
        run=self.review_run('follow-up')
        state=run.state();state.update(state='complete',turns=4)
        accepted=state['accepted_commit'];run.save_state(state)
        run.note('Recheck the result and address this follow-up.')
        state=run.state();run._read_notes(state)
        self.assertEqual(state['state'],'ready')
        self.assertEqual(state['turns'],4)
        self.assertEqual(state['accepted_commit'],accepted)
        self.assertIn('address this follow-up',run._prompt(state))

    def test_follow_up_with_existing_checks_requires_fresh_review_for_no_change(self):
        run=self.review_run('checked-follow-up')
        config=run.load('config.json',{});config['acceptance']=['true'];run.save('config.json',config)
        state=run.state();state.update(state='complete',baseline={'true':{'code':1}},
            review_evidence={'attempt':'old','verdict':{'verdict':'pass'}})
        run.save_state(state);run.note('Recheck without changing files.')
        state=run.state();run._read_notes(state);state['attempt']='new'
        run.invoke=mock.Mock(return_value=(0,self.review_output(),''))
        run._consume(state,{'status':'ok','response':'Rechecked; no changes needed.','usage':{}})
        self.assertEqual(run.state()['state'],'complete')
        self.assertEqual(run.state()['review_evidence']['attempt'],'new')
        self.assertEqual(sum(call.args[0][0] == 'codex' for call in run.invoke.call_args_list),1)

    def test_message_during_active_turn_stays_unread_until_follow_up(self):
        run=self.review_run('queued-message')
        state=run.state();state.update(state='working',attempt='active')
        run.save_state(state);run.note('Follow up after the current turn.')
        run._read_notes(state)
        self.assertEqual(state.get('inbox_seen',0),0)
        state.update(state='complete',attempt=None);run.save_state(state)
        run._read_notes(state)
        self.assertEqual(state['state'],'ready')
        self.assertEqual(state['inbox_seen'],1)

    def test_unlimited_parallelism_and_effort_validation(self):
        spec={**self.spec,'max_parallel':None,'supervisor':{'effort':'high'}}
        spec['tasks'][0]['options']={'worker_effort':'xhigh'}
        checked=self.fleet.normalized(spec,self.source.parent)
        self.assertIsNone(checked['max_parallel'])
        self.assertEqual(checked['supervisor']['effort'],'high')
        self.assertEqual(checked['tasks'][0]['options']['worker_effort'],'xhigh')
        for invalid in (0,-1,True):
            with self.assertRaises(ValueError):
                self.fleet.normalized({**spec,'max_parallel':invalid},self.source.parent)
        with self.assertRaises(ValueError):
            self.fleet.normalized({**spec,'supervisor':{'effort':'invalid'}},self.source.parent)

    def test_shared_supervisor_review_receives_effort(self):
        run=self.review_run('review-effort')
        run._active_supervisor_settings['effort']='high'
        run.invoke=mock.Mock(return_value=(0,self.review_output(),''))
        self.assertTrue(run._review(run.state(),[]))
        self.assertIn('model_reasoning_effort="high"',run.invoke.call_args.args[0])

    def test_native_handoff_skips_its_keeper_but_keeps_other_agents_scheduled(self):
        import fcntl
        spec=json.loads(self.fleet.manifest.read_text());spec['tasks'][0]['terminal_requested']=True
        self.fleet.manifest.write_text(json.dumps(spec))
        run=self.run_for('one')
        with run.path('keeper.lock').open('a') as owner:
            fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with mock.patch.object(SupervisedRun,'step',autospec=True) as step:
                self.fleet.run(once=True)
                step.assert_called_once()
                self.assertEqual(step.call_args.args[0].id,self.run_for('two').id)

    def request_terminal(self, ident='one'):
        spec=json.loads(self.fleet.manifest.read_text())
        next(t for t in spec['tasks'] if t['id']==ident)['terminal_requested']=True
        self.fleet.manifest.write_text(json.dumps(spec))

    def requested(self, ident='one'):
        spec=json.loads(self.fleet.manifest.read_text())
        return next(t for t in spec['tasks'] if t['id']==ident)['terminal_requested']

    def test_cross_repo_option_must_be_a_boolean(self):
        spec={**self.spec,'tasks':[{**self.spec['tasks'][0],'options':{'cross_repo':'yes'}}]}
        with self.assertRaises(ValueError):
            self.fleet.normalized(spec,self.source.parent)
        spec['tasks'][0]['options']={'cross_repo':True}
        self.assertTrue(self.fleet.normalized(spec,self.source.parent)['tasks'][0]['options']['cross_repo'])

    def test_message_takes_back_a_terminal_request_that_never_opened(self):
        self.request_terminal()
        self.fleet.note('one','Please continue')
        with mock.patch.object(SupervisedRun,'step',autospec=True) as step:
            self.fleet.run(once=True)
        self.assertFalse(self.requested())
        self.assertIn(self.run_for('one').dir,[call.args[0].dir for call in step.call_args_list])
        self.assertIn('terminal_request_withdrawn',self.run_for('one').path('journal.jsonl').read_text())

    def test_message_leaves_an_open_terminal_alone(self):
        self.request_terminal()
        run=self.run_for('one')
        state=run.state();state['interactive_session']={'id':'live'};run.save_state(state)
        self.fleet.note('one','Please continue')
        with mock.patch.object(SupervisedRun,'step',autospec=True) as step:
            self.fleet.run(once=True)
        self.assertTrue(self.requested())
        self.assertNotIn(run.dir,[call.args[0].dir for call in step.call_args_list])

    def leave_dead_terminal_record(self, run):
        from loop.worker import terminal_owner_path
        marker=terminal_owner_path(run.dir,run.worktree)
        marker.parent.mkdir(parents=True,exist_ok=True)
        marker.write_text('{}')
        return marker

    def test_message_clears_a_dead_terminals_record_when_nothing_runs_in_the_worktree(self):
        self.request_terminal()
        run=self.run_for('one');self.leave_dead_terminal_record(run)
        self.fleet.note('one','Please continue')
        with mock.patch('loop.supervise.TmuxSession') as tmux, \
                mock.patch('loop.supervise.worktree_has_processes',return_value=False), \
                mock.patch.object(SupervisedRun,'step',autospec=True):
            tmux.recover.return_value.alive.return_value=False
            self.fleet.run(once=True)
        tmux.recover.return_value.reconcile.assert_called_once_with(verified_no_writers=True)
        self.assertFalse(self.requested())

    def test_message_keeps_a_dead_terminals_record_while_a_process_runs_in_the_worktree(self):
        self.request_terminal()
        run=self.run_for('one');marker=self.leave_dead_terminal_record(run)
        self.fleet.note('one','Please continue')
        with mock.patch('loop.supervise.TmuxSession') as tmux, \
                mock.patch('loop.supervise.worktree_has_processes',return_value=True), \
                mock.patch.object(SupervisedRun,'step',autospec=True):
            tmux.recover.return_value.alive.return_value=False
            self.fleet.run(once=True)
        tmux.recover.return_value.reconcile.assert_not_called()
        self.assertTrue(marker.exists())
        self.assertTrue(self.requested())

    def test_terminal_request_stays_without_a_waiting_message(self):
        self.request_terminal()
        with mock.patch.object(SupervisedRun,'step',autospec=True):
            self.fleet.run(once=True)
        self.assertTrue(self.requested())

    def run_for(self, ident='one'):
        return next(run for task, run in self.fleet.runs() if task['id'] == ident)

    def review_run(self, name):
        run=SupervisedRun(self.repos[0],name,limits=lambda _: {},scribe=Scribe(),out=lambda _:None)
        run.create('Set value to 2',options={'workers':['codex']})
        run._active_supervisor_settings={'provider':'codex','model':'strong-review-model',
            'timeout_seconds':2,'max_interventions':6,'_home':str(self.fleet.home)}
        state=run.state();run._setup(state)
        return run

    def review_output(self, verdict='pass', problems=None):
        return '\n'.join(json.dumps(e) for e in [
            {'type':'thread.started','thread_id':'review-session'},
            {'item':{'type':'agent_message','text':json.dumps({'verdict':verdict,'problems':problems or []})}},
            {'type':'turn.completed','usage':{'input_tokens':30,'output_tokens':4}}])

    def test_command_is_optional_and_automatic_review_accepts_complete_work(self):
        spec=json.loads(self.source.read_text())
        spec['tasks'][0].pop('acceptance')
        spec['tasks'][1]['acceptance']=[]
        normalized=self.fleet.normalized(spec,self.source.parent)
        self.assertEqual([t['acceptance'] for t in normalized['tasks']],[[],[]])
        run=self.review_run('auto-review')
        (run.worktree/'app.py').write_text('value = 2\n')
        run.invoke=mock.Mock(return_value=(0,self.review_output(),''))
        state=run.state();state['attempt']='one'
        run._consume(state,{'status':'ok','response':'Implemented value 2.','usage':{}})
        self.assertEqual(run.state()['state'],'complete')
        self.assertEqual(run.state()['review_evidence']['model'],'strong-review-model')
        self.assertEqual(run.state()['decisions'][-1]['action'],'review')
        from loop.usage import role_usage
        self.assertEqual(role_usage(run.state(),'supervisor')['total_tokens'],34)
        argv=run.invoke.call_args.args[0]
        self.assertIn('strong-review-model',argv)
        self.assertTrue(any('read-only' in a for a in argv))

    def test_review_rejection_repairs_with_feedback_before_accepting(self):
        run=self.review_run('review-repair')
        (run.worktree/'app.py').write_text('value = 3\n')
        run.invoke=mock.Mock(return_value=(0,self.review_output('fail',['Set value to 2, not 3.']),''))
        state=run.state();state['attempt']='one'
        run._consume(state,{'status':'ok','response':'Updated value.','usage':{}})
        state=run.state()
        self.assertEqual(state['state'],'ready')
        self.assertIn('Set value to 2',run._prompt(state))
        self.assertEqual(git(run.worktree,'rev-parse','HEAD'),state['accepted_commit'])
        (run.worktree/'app.py').write_text('value = 2\n')
        run.invoke.return_value=(0,self.review_output(),'')
        state['attempt']='two'
        run._consume(state,{'status':'ok','response':'Fixed value.','usage':{}})
        self.assertEqual(run.state()['state'],'complete')
        self.assertEqual(run.state()['review_feedback'],[])

    def test_reviewed_no_change_result_can_finish_without_an_invented_commit(self):
        run=self.review_run('review-no-changes')
        before=git(run.worktree,'rev-parse','HEAD')
        run.invoke=mock.Mock(return_value=(0,self.review_output(),''))
        state=run.state();state['attempt']='one'
        run._consume(state,{'status':'ok','response':'Goal is already satisfied; no edits needed.','usage':{}})
        self.assertEqual(run.state()['state'],'complete')
        self.assertEqual(git(run.worktree,'rev-parse','HEAD'),before)

    def test_invalid_review_retries_are_bounded(self):
        run=self.review_run('review-invalid')
        (run.worktree/'app.py').write_text('value = 2\n')
        run.invoke=mock.Mock(return_value=(0,self.review_output('unclear'),''))
        run._active_supervisor_settings={k:v for k,v in run._active_supervisor_settings.items() if k!='_home'}
        state=run.state();state['verification_attempt']='one'
        for _ in range(3):
            run._review(state,['app.py'])
            state=run.state()
        self.assertEqual(state['state'],'waiting')
        self.assertEqual(state['retry_at'],0)
        self.assertEqual(state['review_errors'],3)

    def test_busy_supervisor_queues_review_without_relaunching_worker(self):
        import fcntl
        run=self.review_run('review-queued')
        (run.worktree/'app.py').write_text('value = 2\n')
        run.invoke=mock.Mock(return_value=(0,self.review_output(),''))
        run.clock=lambda:100
        with (self.fleet.home/'supervisor.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            state=run.state();state['attempt']='one'
            run._consume(state,{'status':'ok','response':'Implemented.','usage':{}})
        self.assertTrue(run.state()['review_pending'])
        run.invoke.assert_not_called()
        run.clock=lambda:102
        with mock.patch.object(run,'_launch') as launch:
            run.step(run._active_supervisor_settings)
            run.step(run._active_supervisor_settings)
            launch.assert_not_called()
        self.assertEqual(run.state()['state'],'complete')

    def test_single_supervisor_reuses_one_serialized_conversation(self):
        settings={'provider':'codex','timeout_seconds':2,'max_interventions':4,
                  '_home':str(self.fleet.home)}
        for ident in ('one','two'):
            run=self.run_for(ident)
            state=run.state();state['needs_help']=True
            run._wait(state,'Should I use value 2?')
            decision=json.dumps({'action':'continue','message':'Use value 2.'})
            output='\n'.join(json.dumps(e) for e in [
                {'type':'thread.started','thread_id':'shared-supervisor-session'},
                {'item':{'type':'agent_message','text':decision}},
                {'type':'turn.completed','usage':{'input_tokens':100,'cached_input_tokens':60,'output_tokens':20}}])
            run.invoke=mock.Mock(return_value=(0,output,''))
            run.help(settings)
            argv=run.invoke.call_args.args[0]
            if ident=='two':
                self.assertEqual(argv[:3],['codex','exec','resume'])
                self.assertIn('shared-supervisor-session',argv)
        from loop.usage import role_usage, combine
        total=combine(role_usage(self.run_for(ident).state(),'supervisor') for ident in ('one','two'))
        self.assertEqual(total['total_tokens'],240)
        self.assertEqual(total['cached_tokens'],120)
        self.assertEqual(total['reported_calls'],2)
        shared=json.loads((self.fleet.home/'supervisor-state.json').read_text())
        self.assertEqual(shared['session_id'],'shared-supervisor-session')
        self.assertIsNone(shared['busy'])

    def test_optional_keeper_does_not_spawn_or_download_when_unavailable(self):
        from loop.supervise import LocalKeeper
        client=LocalKeeper('installed-model')
        with mock.patch.object(client,'_post',side_effect=OSError('not running')), \
             mock.patch('subprocess.Popen') as spawn:
            self.assertFalse(client.ensure())
            spawn.assert_not_called()

    def test_supervised_files_have_no_allowlist_or_protected_paths(self):
        run = self.run_for()
        run.save('config.json', {**run.config, 'scope':['app.py'], 'protected':['other.py']})
        self.assertEqual(run.config['scope'], ['.'])
        self.assertEqual(run.config['protected'], [])
        state = run.state()
        run._setup(state)
        (run.worktree/'app.py').write_text('value = 2\n')
        (run.worktree/'other.py').write_text('additional = True\n')
        state['attempt'] = 'all-project-files'
        run._consume(state, {'status':'ok','response':'Updated both files.','usage':{}})
        self.assertEqual(run.state()['state'],'complete')
        self.assertIn('other.py',git(run.worktree,'ls-files'))

    def test_native_subagent_setting_changes_cli_flags_and_prompt(self):
        from loop.adapters import continuing_worker_command, command
        for provider in ('codex','claude','antigravity','muse'):
            allowed = continuing_worker_command(provider,'Task',workspace='/w',allow_subagents=True)
            denied = continuing_worker_command(provider,'Task',workspace='/w',allow_subagents=False)
            self.assertTrue(any('You may delegate to native subagents' in a for a in allowed))
            self.assertFalse(any('Do not delegate, spawn' in a for a in allowed))
            self.assertTrue(any('Do not delegate, spawn' in a for a in denied))
            if provider == 'codex':
                for feature in ('multi_agent','multi_agent_v2'):
                    self.assertEqual(allowed[allowed.index(feature)-1],'--enable')
                    self.assertEqual(denied[denied.index(feature)-1],'--disable')
            if provider == 'claude':
                self.assertNotIn('--disallowed-tools',allowed)
                self.assertIn('Agent',denied)
        supervisor = command('codex','Plan',worker=False,allow_subagents=True)
        self.assertNotIn('--enable',supervisor)

    def limited_result(self, run, until=1000, status='rate_limited'):
        state = run.state()
        state['attempt'] = 'limited-attempt'
        run._consume(state, {'status': status, 'error': 'usage limit',
                             'retry_at': until, 'session_id': 'preserved-session', 'usage': {}})
        return run.state()

    def test_quota_wait_survives_restart_and_resumes_same_model_and_session(self):
        run = self.run_for()
        run.clock = lambda: 100
        run.limits = lambda _: {}
        state = self.limited_result(run)
        self.assertEqual(state['state'], 'waiting')
        self.assertEqual(state['retry_at'], 1000)
        self.assertEqual(state['session_id'], 'preserved-session')
        fresh = self.run_for()
        fresh.clock = lambda: 999
        fresh.limits = lambda _: {}
        with mock.patch.object(fresh, '_spawn') as spawn:
            fresh.step({'max_turns': 10})
            spawn.assert_not_called()
        fresh.clock = lambda: 1001
        fresh.step({'max_turns': 10})
        with mock.patch.object(fresh, '_setup'), mock.patch.object(fresh, '_spawn') as spawn:
            fresh.step({'max_turns': 10})
            spawn.assert_called_once()
        state = fresh.state()
        attempt = json.loads(fresh._attempt_path(state).read_text())
        self.assertEqual(attempt['session_id'], 'preserved-session')
        self.assertNotIn('codex', state['provider_holds'])

    def test_longer_exhausted_window_extends_short_reset(self):
        run = self.run_for()
        run.clock = lambda: 100
        run.limits = lambda _: {'primary': {'usedPercent': 100, 'resetsAt': 200},
                                'secondary': {'usedPercent': 100, 'resetsAt': 5000}}
        state = self.limited_result(run, until=200)
        self.assertEqual(state['retry_at'], 5005)
        # A fresh weekly-limit observation after the recorded deadline delays admission again.
        run.clock = lambda: 5010
        run.limits = lambda _: {'usedPercent': 100, 'resetsAt': 8000}
        state.update(state='ready')
        run.save_state(state)
        with mock.patch.object(run, '_spawn') as spawn:
            run._launch(state)
            spawn.assert_not_called()
        self.assertEqual(run.state()['retry_at'], 8005)

    def test_fallback_models_and_return_to_primary_restore_provider_sessions(self):
        run = self.run_for()
        run.save('config.json', {**run.config, 'workers': ['codex', 'claude'],
                                'worker_model': 'custom-codex',
                                'worker_models': {'claude': 'custom-claude'}})
        state = run.state()
        state.update(model='custom-codex', sessions={'claude': 'claude-session'})
        run.save_state(state)
        run.clock = lambda: 100
        run.limits = lambda _: {}
        state = self.limited_result(run)
        self.assertEqual((state['state'], state['provider'], state['model'], state['session_id']),
                         ('ready', 'claude', 'custom-claude', 'claude-session'))
        run.clock = lambda: 1001
        self.assertTrue(run._select_provider(state))
        self.assertEqual((state['provider'], state['model'], state['session_id']),
                         ('codex', 'custom-codex', 'preserved-session'))

    def test_unknown_reset_has_bounded_retry_and_auth_needs_note(self):
        run = self.run_for()
        run.clock = lambda: 100
        run.limits = mock.Mock(side_effect=OSError('no telemetry'))
        state = self.limited_result(run, until=0)
        self.assertEqual(state['retry_at'], 100 + run.config['unknown_retry_seconds'])
        state = self.limited_result(run, status='auth_required')
        self.assertEqual(state['retry_at'], 0)
        self.assertTrue(state['provider_holds']['codex']['auth'])
        self.fleet.note('one', 'Login repaired; retry')
        run._read_notes(state)
        self.assertEqual(state['state'], 'ready')
        self.assertFalse(state['provider_holds'])

    def test_supervisor_quota_retry_is_timed_and_does_not_spend_intervention_allowance(self):
        run = self.run_for()
        run.clock = lambda: 100
        run.limits = lambda _: {}
        state = run.state()
        state['needs_help'] = True
        run._wait(state, 'Specific question?')
        reply = json.dumps({'type': 'error', 'message': 'usage limit'})
        run.invoke = mock.Mock(return_value=(1, reply, ''))
        settings = {'provider': 'codex', 'max_interventions': 1, 'timeout_seconds': 1}
        run.help(settings)
        state = run.state()
        self.assertEqual(state['interventions'], 0)
        self.assertGreater(state['supervisor_retry_at'], 100)
        self.assertTrue(state['needs_help'])
        run.help(settings)
        self.assertEqual(run.invoke.call_count, 1)
        run.clock = lambda: state['supervisor_retry_at'] + 1
        run.limits = lambda _: {'usedPercent': 100, 'resetsAt': 5000}
        run.help(settings)
        self.assertEqual(run.invoke.call_count, 1)
        self.assertEqual(run.state()['supervisor_retry_at'], 5005)
        run.clock = lambda: 5006
        run.limits = lambda _: {}
        decision = json.dumps({'action': 'continue', 'message': 'Use existing conventions.'})
        reply = json.dumps({'item': {'type': 'agent_message', 'text': decision}})
        run.invoke.return_value = (0, reply + '\n' + json.dumps({'type': 'turn.completed'}), '')
        run.help(settings)
        self.assertEqual(run.state()['state'], 'ready')
        self.assertEqual(run.state()['interventions'], 1)

    def test_all_provider_holds_wait_for_earliest_candidate(self):
        run = self.run_for()
        run.save('config.json', {**run.config, 'workers': ['codex', 'claude']})
        run.clock = lambda: 100
        run.limits = lambda _: {}
        state = self.limited_result(run, until=1000)
        self.assertEqual(state['provider'], 'claude')
        state = self.limited_result(run, until=500)
        self.assertEqual(state['state'], 'waiting')
        self.assertEqual(state['retry_at'], 500)
        run.clock = lambda: 501
        self.assertTrue(run._select_provider(state))
        self.assertEqual(state['provider'], 'claude')
        self.assertIn('codex', state['provider_holds'])

    def test_non_codex_worker_runs_with_its_model_and_effort(self):
        env = self.fake_cli()
        cli = self.root / 'bin' / 'claude'
        cli.write_text('''#!/usr/bin/env python3
import json, sys
from pathlib import Path
assert sys.argv[sys.argv.index('--model')+1] == 'custom-claude-model'
assert sys.argv[sys.argv.index('--effort')+1] == 'high'
Path('app.py').write_text('value = 2\\n')
print(json.dumps({'type':'result','is_error':False,'result':'Implemented',
                  'session_id':'claude-session','usage':{'input_tokens':10}}))
''')
        cli.chmod(0o755)
        run = self.run_for()
        run.save('config.json', {**run.config, 'workers': ['claude'], 'worker_effort': 'high',
                                'worker_models': {'claude': 'custom-claude-model'}})
        state = run.state()
        state.update(provider='claude', model='custom-claude-model')
        run.save_state(state)
        with mock.patch.dict(os.environ, env):
            result = self.fleet.run()
        self.assertTrue(all(s['state'] == 'complete' for s in result))
        self.assertEqual(self.run_for().state()['session_id'], 'claude-session')

    def test_context_reset_cannot_restore_oversized_session_after_fallback(self):
        run = self.run_for()
        run.save('config.json', {**run.config, 'reset_at_tokens': 20})
        state = run.state()
        state['attempt'] = 'large-context'
        run._consume(state, {'status': 'ok', 'response': 'Which convention should I use?',
                             'session_id': 'oversized', 'usage': {'input_tokens': 30}})
        state = run.state()
        self.assertIsNone(state['session_id'])
        self.assertNotIn('codex', state['sessions'])
        self.assertTrue(state['needs_help'])
        self.assertIn('convention', state['handoff'])

    def test_manifest_accepts_arbitrary_supported_model_ids(self):
        spec = json.loads(self.source.read_text())
        spec['supervisor'] = {'provider': 'muse', 'model': 'custom-supervisor'}
        spec['tasks'][0]['options'] = {'workers': ['claude', 'codex', 'antigravity', 'muse'],
                                      'worker_models': {'claude': 'custom-model-2027'},
                                      'worker_effort': 'high'}
        self.source.write_text(json.dumps(spec))
        fleet = Fleet(self.root / 'custom-fleet')
        fleet.create(self.source)
        run = next(fleet.runs())[1]
        self.assertEqual(run.state()['provider'], 'claude')
        self.assertEqual(run.state()['model'], 'custom-model-2027')
        with mock.patch.object(run, '_spawn'):
            state = run.state()
            run._launch(state)
        attempt = json.loads(run._attempt_path(run.state()).read_text())
        self.assertEqual(attempt['task']['effort'], 'high')
        self.assertEqual(attempt['task']['model'], 'custom-model-2027')

    def test_executable_wrapper_works_outside_agent_loop_checkout(self):
        wrapper = Path(__file__).resolve().parents[1] / 'scripts' / 'supervise'
        result = subprocess.run([str(wrapper), 'status', str(self.fleet.home)],
                                cwd=self.repos[0], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)), 2)

    def test_question_preserves_changes_and_session_and_gets_specific_answer(self):
        run = self.run_for()
        state = run.state()
        run._setup(state)
        (run.worktree / 'app.py').write_text('value = 3\n')
        state['attempt'] = 'first'
        run._consume(state, {'status': 'ok', 'response': 'Should I use value 2 or 3?',
                             'session_id': 'session-one', 'usage': {}})
        state = run.state()
        self.assertTrue(state['needs_help'])
        self.assertEqual(state['session_id'], 'session-one')
        reply = json.dumps({'item': {'type': 'agent_message', 'text': json.dumps(
            {'action': 'continue', 'message': 'Use value 2 as the objective specifies.'})}})
        run.invoke = lambda *a, **kw: (0, reply + '\n' + json.dumps({'type': 'turn.completed'}), '')
        run.help(self.spec.get('supervisor', {'provider': 'codex', 'timeout_seconds': 5,
                                             'max_interventions': 3}))
        state = run.state()
        self.assertEqual(state['state'], 'ready')
        self.assertIn('Use value 2', run._prompt(state))
        self.assertEqual((run.worktree / 'app.py').read_text(), 'value = 3\n')
        self.assertEqual(state['interventions'], 1)

    def test_limit_bad_reply_escalation_and_operator_note(self):
        run = self.run_for()
        settings = {'provider': 'codex', 'timeout_seconds': 1, 'max_interventions': 1}
        state = run.state()
        state['needs_help'] = True
        run._wait(state, 'Need help')
        run.invoke = lambda *a, **kw: (0, 'garbage', '')
        run.help(settings)
        self.assertIn('Supervisor unavailable', run.state()['wait_reason'])
        state = run.state()
        state['needs_help'] = True
        run.save_state(state)
        with mock.patch.object(run, 'invoke') as invoke:
            run.help(settings)
            invoke.assert_not_called()
        self.assertIn('limit', run.state()['wait_reason'])
        self.fleet.note('one', 'Use the repository default and continue')
        state = run.state()
        run._read_notes(state)
        self.assertEqual(state['state'], 'ready')
        self.assertFalse(state['needs_help'])

    def test_reject_duplicate_id_and_invalid_acceptance_before_worktrees(self):
        for update in ('duplicate', 'acceptance'):
            spec = json.loads(json.dumps(self.spec))
            if update == 'duplicate':
                spec['tasks'][1]['id'] = 'one'
            else:
                spec['tasks'][1]['acceptance'] = ['']
            self.source.write_text(json.dumps(spec))
            destination = self.root / update
            with self.assertRaises(ValueError):
                Fleet(destination).create(self.source)
            self.assertFalse(destination.exists())

    def fake_cli(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        cli = bindir / 'codex'
        cli.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
supervisor = any('You supervise an independent' in a for a in sys.argv)
resumed = 'resume' in sys.argv
with open(os.environ['FAKE_EVENTS'], 'a') as f:
    f.write(json.dumps({'at':time.time(),'cwd':os.getcwd(),'supervisor':supervisor,'resumed':resumed,'subagents':'--enable' in sys.argv})+'\\n')
print(json.dumps({'type':'thread.started','thread_id':'test-session'}))
if supervisor:
    text = json.dumps({'action':'continue','message':'Use value 2.'})
elif not resumed:
    time.sleep(0.5)
    text = 'Should I use value 2 or 3?'
else:
    Path('app.py').write_text('value = 2\\n')
    text = 'Implemented value 2.'
print(json.dumps({'item':{'type':'agent_message','text':text}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':11,'output_tokens':5}}))
''')
        cli.chmod(0o755)
        return {'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
                'FAKE_EVENTS': str(self.root / 'events.jsonl')}

    def test_real_detached_workers_parallel_questions_resume_and_completion(self):
        run = self.run_for('one')
        run.save('config.json',{**run.config,'allow_subagents':True})
        for other in ('two',):
            other_run = self.run_for(other)
            other_run.save('config.json',{**other_run.config,'allow_subagents':False})
        with mock.patch.dict(os.environ, self.fake_cli()):
            # One scheduling pass launches real detached workers and returns.
            self.fleet.run(once=True)
            self.assertTrue(all(s['attempt'] for s in self.fleet.status()))
            # A new manager object must reconcile rather than launch duplicates.
            result = Fleet(self.fleet.home).run()
        self.assertTrue(all(s['state'] == 'complete' for s in result))
        events = [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]
        first = [e for e in events if not e['supervisor'] and not e['resumed']]
        self.assertEqual(len(first), 2)
        self.assertLess(abs(first[0]['at'] - first[1]['at']), 0.5)
        self.assertEqual(sum(e['supervisor'] for e in events), 2)
        self.assertEqual(sum(e['resumed'] for e in events), 2)
        for event in events:
            if not event['supervisor']:
                self.assertEqual(event['subagents'],event['cwd'].endswith('-one'))
        for task, run in self.fleet.runs():
            self.assertEqual(git(run.worktree, 'status', '--porcelain'), '')
            self.assertEqual((run.worktree / 'app.py').read_text(), 'value = 2\n')
            self.assertEqual((run.repo / 'app.py').read_text(), 'value = 1\n')
            self.assertEqual(run.state()['interventions'], 1)
            from loop.usage import role_usage
            self.assertEqual(role_usage(run.state(),'worker')['total_tokens'],32)
            self.assertEqual(role_usage(run.state(),'worker')['reported_calls'],2)

    def test_max_parallel_one_serializes_worker_lifetimes(self):
        spec = json.loads(self.fleet.manifest.read_text())
        spec['max_parallel'] = 1
        self.fleet.manifest.write_text(json.dumps(spec))
        with mock.patch.dict(os.environ, self.fake_cli()):
            result = self.fleet.run()
        self.assertTrue(all(s['state'] == 'complete' for s in result))
        events = [json.loads(line) for line in (self.root / 'events.jsonl').read_text().splitlines()]
        starts = [e for e in events if not e['supervisor'] and not e['resumed']]
        self.assertEqual(len(starts), 2)
        # Every initial worker stays alive for at least half a second.
        self.assertGreaterEqual(starts[1]['at'] - starts[0]['at'], 0.5)

    def test_operator_escalation_does_not_resume_worker(self):
        run = self.run_for()
        state = run.state()
        state['needs_help'] = True
        run._wait(state, 'May I publish?')
        decision = {'action': 'escalate', 'message': 'Publishing requires operator direction.'}
        reply = json.dumps({'item': {'type': 'agent_message', 'text': json.dumps(decision)}})
        run.invoke = lambda *a, **kw: (0, reply + '\n' + json.dumps({'type': 'turn.completed'}), '')
        run.help({'provider': 'codex', 'timeout_seconds': 1, 'max_interventions': 1})
        self.assertEqual(run.state()['state'], 'waiting')
        self.assertFalse(run.state()['needs_help'])
        self.assertIn('Publishing', run.state()['decisions'][0]['message'])

    def test_existing_keeper_lock_prevents_manager_takeover(self):
        import fcntl
        run = self.run_for()
        with run.path('keeper.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.fleet.run(once=True)
        self.assertTrue(all(s['attempt'] is None for s in self.fleet.status()))

    def test_worker_turn_and_token_admission_limits(self):
        run = self.run_for()
        state = run.state()
        state['turns'] = 2
        run.save_state(state)
        with mock.patch.object(run, '_launch') as launch:
            run.step({'max_turns': 2})
            launch.assert_not_called()
        self.assertIn('turn limit', run.state()['wait_reason'])
        state = run.state()
        state.update(state='ready', turns=0, tokens={'codex': 100})
        run.save_state(state)
        run.save('config.json', {**run.config, 'max_worker_tokens': 50})
        with mock.patch.object(run, '_launch') as launch:
            run.step({'max_turns': 2})
            launch.assert_not_called()
        self.assertIn('token limit', run.state()['wait_reason'])

    def test_blocked_task_does_not_stop_another_and_manager_exits(self):
        run = self.run_for()
        state = run.state()
        run._wait(state, 'Authentication required')
        with mock.patch.dict(os.environ, self.fake_cli()):
            result = self.fleet.run()
        self.assertEqual([s['state'] for s in result], ['waiting', 'complete'])


if __name__ == '__main__':
    unittest.main()
