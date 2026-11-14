import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from datetime import datetime, timezone

from loop.native import agent_terminal_command, terminal_config, store_session, codex_terminal_command, discover_session, latest_session


class NativeTerminalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / 'worktree'; self.workspace.mkdir()
        self.state = {'provider': 'codex', 'session_id': 'correct-chat', 'model': 'chosen-model'}
        self.run = SimpleNamespace(worktree=self.workspace, config={'worker_effort': 'high'},
            state=lambda: self.state.copy(), path=lambda name: self.root / name,
            load=lambda name, default=None: json.loads((self.root / name).read_text()) if (self.root / name).exists() else default,
            _prompt=lambda state: 'The existing worker objective')
        self.run.save = lambda name, value: (self.root / name).write_text(json.dumps(value))
        (self.root / 'results').mkdir(); (self.root / 'attempts').mkdir()

    def test_real_tui_resumes_exact_chat_with_selected_worktree_and_permissions(self):
        argv, session = codex_terminal_command(self.run)
        self.assertEqual(session, 'correct-chat')
        self.assertEqual(argv[:2], ['codex', 'resume'])
        self.assertEqual(argv[-1], 'correct-chat')
        self.assertNotIn('--last', argv)
        self.assertNotIn('--json', argv)
        self.assertIn('--no-daemon', argv)
        self.assertEqual(argv[argv.index('--cd')+1], str(self.workspace))
        self.assertEqual(argv[argv.index('--sandbox')+1], 'workspace-write')
        self.assertIn('model_reasoning_effort="high"', argv)

    def test_fresh_chat_and_provider_validation(self):
        self.state['session_id'] = None
        argv, session = codex_terminal_command(self.run)
        self.assertIsNone(session)
        self.assertNotIn('resume', argv)
        self.state['provider'] = 'claude'
        with self.assertRaises(ValueError): codex_terminal_command(self.run)

    def test_saved_native_chat_survives_recreated_run_and_other_provider_results(self):
        self.state['session_id'] = None
        (self.root / 'native-session.json').write_text(json.dumps({'session_id':'saved-chat','saved_at':time.time()+10}))
        (self.root / 'results/other.json').write_text(json.dumps({'attempt_id':'other','result':{'session_id':'claude-chat'}}))
        (self.root / 'attempts/other.json').write_text(json.dumps({'task':{'provider':'claude'}}))
        self.assertEqual(latest_session(self.run), 'saved-chat')
        self.state['interactive_session'] = {'session_id':'active-before-shutdown'}
        self.assertEqual(latest_session(self.run), 'active-before-shutdown')

    def metadata(self, root, name, cwd, session, source='cli'):
        path=root/'sessions/2026/01/01'; path.mkdir(parents=True,exist_ok=True)
        (path/(name+'.jsonl')).write_text(json.dumps({'type':'session_meta','payload':{
            'id':session,'cwd':str(cwd),'source':source,'timestamp':datetime.now(timezone.utc).isoformat()}})+'\n')

    def test_discovery_reads_only_matching_native_session_headers(self):
        with mock.patch.dict(os.environ, {'CODEX_HOME':str(self.root)}):
            start=time.time()
            self.metadata(self.root,'other',self.root/'other','wrong-chat')
            self.metadata(self.root,'exec',self.workspace,'worker-chat',source='exec')
            self.metadata(self.root,'native',self.workspace,'right-chat')
            self.assertEqual(discover_session(self.workspace,start),'right-chat')
            self.metadata(self.root,'concurrent',self.workspace,'ambiguous-chat')
            self.assertIsNone(discover_session(self.workspace,start))
            self.assertEqual(discover_session(self.workspace,start,{'right-chat'}),'ambiguous-chat')

    def test_built_in_interactive_adapters_resume_exact_sessions(self):
        for provider, executable, flag in [('claude','claude','--resume'), ('antigravity','agy','--conversation'), ('muse','muse','resume')]:
            with self.subTest(provider=provider):
                self.state.update(provider=provider,session_id='exact-chat')
                argv, session, actual = agent_terminal_command(self.run)
                self.assertEqual((argv[0],session,actual),(executable,'exact-chat',provider))
                self.assertEqual(argv[argv.index(flag)+1],'exact-chat')
                self.assertNotIn('--json',argv)
                self.assertNotIn('--print',argv)
                self.assertNotIn('--last',argv)

    def test_new_claude_chat_gets_durable_preallocated_id(self):
        self.state.update(provider='claude',session_id=None)
        argv, session, provider = agent_terminal_command(self.run)
        self.assertIn('--session-id',argv)
        store_session(self.run,provider,session,{'provider':provider},fresh=True)
        again, recovered, _ = agent_terminal_command(self.run)
        self.assertEqual(recovered,session)
        self.assertIn('--session-id',again)

    def test_custom_argv_and_recovery_are_scoped_to_program(self):
        config=terminal_config(self.run,{'program':'custom','command':'my-agent --path "{worktree}"',
            'resume_command':'my-agent resume "{session_id}" --path "{worktree}"'})
        argv, session, provider=agent_terminal_command(self.run,config)
        self.assertEqual(argv,['my-agent','--path',str(self.workspace)])
        self.assertIsNone(session)
        store_session(self.run,provider,'exact custom chat;$(echo unsafe)',config,manual=True)
        argv, session, _=agent_terminal_command(self.run,config)
        self.assertEqual(argv,['my-agent','resume','exact custom chat;$(echo unsafe)','--path',str(self.workspace)])
        self.assertEqual(self.state['session_id'],'correct-chat')
        other=terminal_config(self.run,{'program':'custom','command':'other-agent'})
        self.assertIsNone(agent_terminal_command(self.run,other)[1])
        with self.assertRaises(ValueError):
            terminal_config(self.run,{'program':'custom','command':'agent','resume_command':'agent --last'})
