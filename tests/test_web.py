"""Integration tests for the local control API and real PTY lifecycle.

Run with .agent-loop/web-venv/bin/python -m unittest tests.test_web.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

try:
    from aiohttp import CookieJar, WSMsgType, WSServerHandshakeError
    from aiohttp.test_utils import TestClient, TestServer
    from loop.web import create_app, APP_KEY, Terminal, lock_held
    WEB_AVAILABLE = True
except ImportError:
    WEB_AVAILABLE = False

from tests.support import isolate_registry


@unittest.skipUnless(WEB_AVAILABLE, 'Install requirements-web.txt for browser integration tests')
class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        isolate_registry(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        for args in [('init','-q'),('config','user.email','test@example.invalid'),
                     ('config','user.name','Test')]:
            subprocess.run(['git','-C',str(self.repo),*args], check=True)
        (self.repo / 'app.py').write_text('value = 1\n')
        subprocess.run(['git','-C',str(self.repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(self.repo),'commit','-qm','initial'],check=True)
        self.app = create_app(self.root / 'web')
        self.controller = self.app[APP_KEY]
        self.controller.shell = '/bin/sh'
        self.client = TestClient(TestServer(self.app),cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.origin = str(self.client.make_url('/')).rstrip('/')
        response = await self.client.get('/')
        self.assertEqual(response.status,200)
        self.manifest = {'max_parallel': 1, 'tasks': [{'id':'fix','repo':str(self.repo),
                         'goal':'Set value to 2','acceptance':[
                             'python3 -B -c "from app import value; assert value == 2"']} ]}

    async def post(self, path, data):
        return await self.client.post(path,json=data,headers={'X-Loop-Request':'1', 'Origin':self.origin})

    async def test_delete_agent_cleans_worktree_branch_history_and_allows_reuse(self):
        task = self.manifest['tasks'][0]
        response = await self.post('/api/supervisor/agents', {'tasks': [task]})
        self.assertEqual(response.status, 201, await response.text())
        entry = self.controller.supervisor()
        run = self.controller.run_for(entry['id'], 'fix')
        (run.worktree / 'uncommitted.txt').write_text('discard after confirmation')
        url = '/api/supervisor/agents/fix/delete'
        response = await self.post(url, {})
        self.assertEqual(response.status, 400)
        self.assertTrue(run.worktree.exists())
        with mock.patch.object(self.controller, 'running', return_value=True):
            response = await self.post(url, {'confirm': True})
            self.assertEqual(response.status, 400)
        from loop.web import locked
        with locked(run.path('keeper.lock')):
            response = await self.post(url, {'confirm': True})
            self.assertEqual(response.status, 409)
        state = run.state()
        run.save_state({**state, 'attempt': 'active'})
        response = await self.post(url, {'confirm': True})
        self.assertEqual(response.status, 400)
        run.save_state(state)
        await self.post('/api/supervisor/agents/fix/remove', {})
        response = await self.post(url, {'confirm': True})
        self.assertEqual(response.status, 200, await response.text())
        self.assertFalse(run.worktree.exists())
        self.assertFalse(run.dir.exists())
        self.assertTrue((self.repo / 'app.py').exists())
        branches = subprocess.check_output(['git', '-C', str(self.repo), 'branch', '--list', run.branch], text=True)
        self.assertEqual(branches.strip(), '')
        spec = json.loads(self.controller.fleet(entry['id']).manifest.read_text())
        self.assertEqual(spec['tasks'], [])
        response = await self.post('/api/supervisor/agents', {'tasks': [task]})
        self.assertEqual(response.status, 201, await response.text())

    async def test_model_catalog_and_idle_agent_model_correction(self):
        cache=self.root/'codex';cache.mkdir()
        (cache/'models_cache.json').write_text(json.dumps({'fetched_at':'today','models':[
            {'slug':'exact-model-id','display_name':'Display Name','visibility':'list'},
            {'slug':'hidden','visibility':'hide'}]}))
        with mock.patch.dict(os.environ,{'CODEX_HOME':str(cache)}):
            response=await self.client.get('/api/models')
            data=await response.json()
            self.assertEqual(data['providers']['codex']['models'][0]['id'],'exact-model-id')
            self.assertEqual(len(data['providers']['codex']['models']),1)
            response=await self.post('/api/models',{'provider':'claude','model':'custom-claude'})
            self.assertEqual(response.status,200)
            (cache/'models_cache.json').unlink()
            data=await (await self.client.get('/api/models')).json()
            self.assertEqual(data['providers']['codex']['models'][0]['id'],'exact-model-id')
            self.assertEqual(data['providers']['claude']['models'][0]['id'],'custom-claude')
        entry=await self.create()
        run=self.controller.run_for(entry['id'],'fix')
        state=run.state();state.update(state='waiting',retry_at=123,model='bad',session_id='old')
        run.save_state(state)
        response=await self.post(f"/api/fleets/{entry['id']}/tasks/fix/settings",{'model':''})
        self.assertEqual(response.status,200,await response.text())
        self.assertIsNone(run.state()['model'])
        self.assertIsNone(run.state()['session_id'])
        self.assertEqual(run.state()['state'],'ready')
        self.assertIsNone(run.config['worker_models']['codex'])

    async def test_sending_note_autostarts_once_and_preserves_note_on_start_failure(self):
        entry=await self.create()
        url=f"/api/fleets/{entry['id']}/tasks/fix/note"
        fleet=self.controller.fleet(entry['id'])
        with mock.patch.object(self.controller,'resume') as resume:
            result=await (await self.post(url,{'text':'Continue please'})).json()
            self.assertTrue(result['started'])
            resume.assert_called_once_with(entry['id'])
            self.assertTrue(json.loads(fleet.manifest.read_text())['desired_running'])
        with mock.patch.object(self.controller,'running',return_value=True), mock.patch.object(self.controller,'resume') as resume:
            result=await (await self.post(url,{'text':'More detail'})).json()
            self.assertFalse(result['started'])
            resume.assert_not_called()
        with mock.patch.object(self.controller,'resume',side_effect=ValueError('Close the task terminal')):
            result=await (await self.post(url,{'text':'Keep this message'})).json()
            self.assertIn('Close the task terminal',result['start_error'])
            self.assertFalse(json.loads(fleet.manifest.read_text())['desired_running'])
        run=self.controller.run_for(entry['id'],'fix')
        self.assertIn('Keep this message',run.path('inbox.jsonl').read_text())

    async def test_effort_settings_and_unlimited_parallelism(self):
        entry=await self.create()
        response=await self.post(f"/api/fleets/{entry['id']}/tasks/fix/settings",{'worker_effort':'high'})
        self.assertEqual(response.status,200)
        run=self.controller.run_for(entry['id'],'fix')
        self.assertEqual(run.config['worker_effort'],'high')
        response=await self.post('/api/supervisor/settings',{'supervisor':{'provider':'codex','effort':'high'},'max_parallel':None})
        self.assertEqual(response.status,200,await response.text())
        data=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertIsNone(data['max_parallel'])
        self.assertEqual(data['supervisor']['effort'],'high')
        response=await self.post(f"/api/fleets/{entry['id']}/tasks/fix/settings",{'worker_effort':'invalid'})
        self.assertEqual(response.status,400)

    async def test_ui_revision_versions_assets_and_notifies_existing_clients(self):
        from loop.web import ui_revision
        revision=ui_revision()
        response=await self.client.get('/')
        html=await response.text()
        self.assertIn(f'/styles.css?v={revision}',html)
        self.assertIn(f'/app.js?v={revision}',html)
        self.assertIn(f'content="{revision}"',html)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        initial=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        with mock.patch('loop.web.ui_revision',return_value='new-ui-version'):
            updated=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertEqual(updated['ui_revision'],'new-ui-version')
        self.assertGreater(updated['snapshot_revision'],initial['snapshot_revision'])

    async def test_agent_full_access_defaults_and_persistent_opt_out(self):
        entry=await self.create()
        endpoint=f"/api/fleets/{entry['id']}/tasks/fix/settings"
        tasks=self.controller.status()[0]['tasks']
        self.assertTrue(tasks[0]['full_access'])
        response=await self.post(endpoint,{'full_access':False})
        self.assertEqual(response.status,200)
        run=self.controller.run_for(entry['id'],'fix')
        self.assertFalse(run.config['full_access'])
        self.assertFalse(self.controller.status()[0]['tasks'][0]['full_access'])
        response=await self.post(endpoint,{'full_access':'yes'})
        self.assertEqual(response.status,400)
        self.assertFalse(run.config['full_access'])

    async def test_terminal_request_restarts_stopped_manager_to_reconcile_pending_turn(self):
        import sys
        entry=await self.create()
        run=self.controller.run_for(entry['id'],'fix')
        state=run.state();state.update(state='working',attempt='pending-turn');run.save_state(state)
        endpoint=f"/api/fleets/{entry['id']}/tasks/fix/native/request"
        with mock.patch.object(self.controller,'resume') as resume:
            response=await self.post(endpoint,{'program':'custom','command':[sys.executable,'-i']})
        self.assertEqual(response.status,200,await response.text())
        resume.assert_called_once_with(entry['id'])
        spec=json.loads(self.controller.fleet(entry['id']).manifest.read_text())
        self.assertTrue(spec['desired_running'])
        self.assertTrue(spec['tasks'][0]['terminal_requested'])

    async def test_deployment_log_is_available_with_local_auth_and_valid_job_id(self):
        directory=self.root/'deployment';(directory/'jobs').mkdir(parents=True)
        (directory/'jobs/0123456789abcdef.log').write_text('Browser tests failed: delete and clean up agents')
        with mock.patch('loop.web.deploy.target_path',return_value=directory/'target.json'):
            response=await self.client.get('/api/deploy/jobs/0123456789abcdef/log')
            self.assertEqual(response.status,200)
            self.assertIn('delete and clean up agents',await response.text())
            self.assertEqual(response.content_type,'text/plain')
            response=await self.client.get('/api/deploy/jobs/not-a-job/log')
            self.assertEqual(response.status,404)

    async def test_deploy_route_accepts_known_worker_and_rejects_arbitrary_paths(self):
        entry=await self.create()
        run=self.controller.run_for(entry['id'],'fix')
        with mock.patch('loop.web.deploy.common_dir',return_value=self.root), mock.patch('loop.web.deploy.submit',return_value={'id':'queued','url':'http://localhost'}) as submit:
            response=await self.post('/api/deploy',{'source':str(run.worktree)})
            self.assertEqual(response.status,202,await response.text())
            submit.assert_called_once_with(str(run.worktree),'')
            response=await self.post('/api/deploy',{'source':str(self.root/'unknown')})
            self.assertEqual(response.status,400)
            self.assertEqual(submit.call_count,1)

    async def test_native_codex_terminal_has_real_pty_and_returns_control(self):
        bindir=self.root/'bin';bindir.mkdir()
        cli=bindir/'codex'
        cli.write_text('''#!/usr/bin/env python3
import sys
print('Native Codex TTY='+str(sys.stdin.isatty() and sys.stdout.isatty()),flush=True)
print('Resume='+sys.argv[-1],flush=True)
for line in sys.stdin:
 print('Reply: '+line.strip(),flush=True)
''')
        cli.chmod(0o755)
        entry=await self.create()
        run=self.controller.run_for(entry['id'],'fix')
        state=run.state();state['session_id']='saved-native-chat';run.save_state(state)
        endpoint=f"/api/fleets/{entry['id']}/tasks/fix"
        with mock.patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH']}):
            response=await self.post(endpoint+'/native/request',{})
            self.assertEqual(response.status,200)
            ws=await self.client.ws_connect(endpoint+'/terminal?mode=codex',headers={'Origin':self.origin})
            data=await self.output_until(ws,b'Resume=saved-native-chat')
            self.assertIn(b'TTY=True',data)
            await ws.send_json({'type':'input','data':'hello\n'})
            await self.output_until(ws,b'Reply: hello')
            await ws.close()
            ws=await self.client.ws_connect(endpoint+'/terminal?mode=codex',headers={'Origin':self.origin})
            await self.output_until(ws,b'Resume=saved-native-chat')
            self.assertEqual(run.load('native-session.json')['session_id'],'saved-native-chat')
            with mock.patch.object(self.controller,'resume'):
                response=await self.post(endpoint+'/native/release',{})
            self.assertEqual(response.status,200,await response.text())
            await ws.close()
        spec=json.loads(self.controller.fleet(entry['id']).manifest.read_text())
        self.assertFalse(spec['tasks'][0]['terminal_requested'])
        self.assertIn('operator returned',run.path('inbox.jsonl').read_text())
        self.assertEqual(run.load('native-session.json')['session_id'],'saved-native-chat')
        from loop.usage import role_usage
        self.assertEqual(role_usage(run.state(),'worker')['unreported_calls'],1)

    async def test_arbitrary_cli_pty_saves_and_restores_exact_chat(self):
        import shlex
        import sys
        cli=self.root/'custom cli.py'
        cli.write_text("import sys\nprint('TTY='+str(sys.stdin.isatty()),flush=True)\nprint('ARGS='+repr(sys.argv[1:]),flush=True)\nfor line in sys.stdin: print('Custom reply: '+line.strip(),flush=True)\n")
        entry=await self.create()
        run=self.controller.run_for(entry['id'],'fix')
        endpoint=f"/api/fleets/{entry['id']}/tasks/fix"
        command=shlex.join([sys.executable,str(cli)])
        response=await self.post(endpoint+'/native/request',{'program':'custom','command':command,
            'resume_command':command+' --resume "{session_id}"'})
        self.assertEqual(response.status,200,await response.text())
        ws=await self.client.ws_connect(endpoint+'/terminal?mode=agent',headers={'Origin':self.origin})
        await self.output_until(ws,b'TTY=True')
        await ws.send_json({'type':'input','data':'direct custom input\n'})
        await self.output_until(ws,b'Custom reply: direct custom input')
        response=await self.post(endpoint+'/native/session',{'session_id':'custom-exact-chat'})
        self.assertEqual(response.status,200,await response.text())
        await ws.close()
        await self.post(endpoint+'/terminal/close',{})
        self.assertEqual(run.load('native-session.json')['session_id'],'custom-exact-chat')
        self.assertIsNone(run.state().get('session_id'))
        ws=await self.client.ws_connect(endpoint+'/terminal?mode=agent',headers={'Origin':self.origin})
        await self.output_until(ws,b"ARGS=['--resume', 'custom-exact-chat']")
        with mock.patch.object(self.controller,'resume'):
            response=await self.post(endpoint+'/native/release',{})
        self.assertEqual(response.status,200)
        await ws.close()

    async def test_native_reconnect_moves_view_without_restarting_cli_then_reopens_after_exit(self):
        import sys
        cli=self.root/'interactive.py'
        cli.write_text("import os,sys\nprint('PID='+str(os.getpid()),flush=True)\nprint('CHAT='+sys.argv[-1],flush=True)\nfor line in sys.stdin:\n if line.strip() == '/exit': break\n print('Reply: '+line.strip(),flush=True)\n")
        entry=await self.create()
        endpoint=f"/api/fleets/{entry['id']}/tasks/fix"
        body={'program':'custom','command':[sys.executable,str(cli)],'resume_command':[sys.executable,str(cli),'{session_id}'],'session_id':'exact-reopen-chat'}
        self.assertEqual((await self.post(endpoint+'/native/request',body)).status,200)
        first=await self.client.ws_connect(endpoint+'/terminal?mode=agent',headers={'Origin':self.origin})
        await self.output_until(first,b'CHAT=exact-reopen-chat')
        terminal=self.controller.terminals[(entry['id'],'fix')]
        pid=terminal.process.pid
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect(endpoint+'/terminal?mode=agent',headers={'Origin':self.origin})
        second=await self.client.ws_connect(endpoint+'/terminal?mode=agent&takeover=1',headers={'Origin':self.origin})
        detached=await first.receive_json()
        self.assertEqual(detached['type'],'detached')
        await first.close()
        await self.output_until(second,b'CHAT=exact-reopen-chat')
        self.assertEqual(terminal.process.pid,pid)
        await asyncio.sleep(.05)
        self.assertIsNotNone(terminal.ws)
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect(endpoint+'/terminal?mode=agent',headers={'Origin':self.origin})
        await second.send_json({'type':'input','data':'still same CLI\n'})
        await self.output_until(second,b'Reply: still same CLI')
        await second.send_json({'type':'input','data':'/exit\n'})
        await terminal.reaper
        await second.close()
        self.assertEqual((await self.post(endpoint+'/native/request',{})).status,200)
        reopened=await self.client.ws_connect(endpoint+'/terminal?mode=agent',headers={'Origin':self.origin})
        await self.output_until(reopened,b'CHAT=exact-reopen-chat')
        self.assertNotEqual(self.controller.terminals[(entry['id'],'fix')].process.pid,pid)
        await reopened.close()

    async def test_new_native_chat_recovers_exact_id_after_server_restart(self):
        bindir=self.root/'native-bin';bindir.mkdir()
        cli=bindir/'codex'
        cli.write_text(r'''#!/usr/bin/env python3
import os,sys,json
from pathlib import Path
from datetime import datetime,timezone
sid='00000000-0000-4000-8000-000000000001'
if 'resume' in sys.argv:
 print('Resumed='+sys.argv[-1],flush=True)
else:
 path=Path(os.environ['CODEX_HOME'])/'sessions/2026/01/01'
 path.mkdir(parents=True,exist_ok=True)
 (path/'new-native.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'cwd':os.getcwd(),'source':'cli','timestamp':datetime.now(timezone.utc).isoformat()}})+'\n')
 print('New native chat created',flush=True)
for line in sys.stdin:
 print('Reply: '+line.strip(),flush=True)
''')
        cli.chmod(0o755)
        entry=await self.create();endpoint=f"/api/fleets/{entry['id']}/tasks/fix"
        with mock.patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'CODEX_HOME':str(self.root/'native-codex')}):
            await self.post(endpoint+'/native/request',{})
            ws=await self.client.ws_connect(endpoint+'/terminal?mode=codex',headers={'Origin':self.origin})
            await self.output_until(ws,b'New native chat created')
            run=self.controller.run_for(entry['id'],'fix')
            async with asyncio.timeout(5):
                while not run.path('native-session.json').exists(): await asyncio.sleep(.05)
            self.assertEqual(run.load('native-session.json')['session_id'],'00000000-0000-4000-8000-000000000001')
            workspace=str(run.worktree)
            await ws.close()
            await self.client.close()
            self.app=create_app(self.root/'web');self.controller=self.app[APP_KEY]
            self.client=TestClient(TestServer(self.app),cookie_jar=CookieJar(unsafe=True))
            await self.client.start_server();self.addAsyncCleanup(self.client.close)
            self.origin=str(self.client.make_url('/')).rstrip('/')
            await self.client.get('/')
            restored=self.controller.run_for(entry['id'],'fix')
            self.assertEqual(str(restored.worktree),workspace)
            ws=await self.client.ws_connect(endpoint+'/terminal?mode=codex',headers={'Origin':self.origin})
            await self.output_until(ws,b'Resumed=00000000-0000-4000-8000-000000000001')
            with mock.patch.object(self.controller,'resume'):
                await self.post(endpoint+'/native/release',{})
            await ws.close()

    async def test_orphan_native_recovery_checks_registry_and_process_identity(self):
        import signal
        entry=await self.create();fleet=self.controller.fleet(entry['id'])
        spec=json.loads(fleet.manifest.read_text());spec['tasks'][0]['terminal_requested']=True
        fleet.manifest.write_text(json.dumps(spec))
        run=self.controller.run_for(entry['id'],'fix')
        state=run.state();state['interactive_session']={'server_home':str(self.controller.home),'pid':424242,'start_identity':'original-start'}
        run.save_state(state)
        with mock.patch('loop.web.process_identity',return_value='original-start'), mock.patch('loop.web.os.getpgid',return_value=424242), mock.patch('loop.web.os.killpg') as kill:
            await self.controller.recover_native(self.app)
            kill.assert_called_once_with(424242,signal.SIGTERM)
        with mock.patch('loop.web.process_identity',return_value='reused-pid'), mock.patch('loop.web.os.killpg') as kill:
            await self.controller.recover_native(self.app)
            kill.assert_not_called()

    async def create(self):
        response = await self.post('/api/fleets',{'name':'Local test','manifest':self.manifest})
        self.assertEqual(response.status,201,await response.text())
        return await response.json()

    async def ws(self, entry, **kwargs):
        return await self.client.ws_connect(f"/api/fleets/{entry['id']}/tasks/fix/terminal",
                                           headers={'Origin':self.origin}, **kwargs)

    async def output_until(self, ws, expected):
        data = bytearray()
        async with asyncio.timeout(8):
            while expected not in data:
                msg = await ws.receive()
                if msg.type == WSMsgType.BINARY:
                    data.extend(msg.data)
                elif msg.type in (WSMsgType.CLOSED,WSMsgType.ERROR):
                    self.fail('Terminal disconnected before command output: ' + repr(data))
        return bytes(data)

    async def test_agent_and_shared_supervisor_token_totals_are_distinct_and_persistent(self):
        from loop.usage import record
        supervisor=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        response=await self.post('/api/supervisor/agents',{'tasks':[
            {**self.manifest['tasks'][0],'id':ident} for ident in ('tokens-one','tokens-two')]})
        self.assertEqual(response.status,201)
        for ident in ('tokens-one','tokens-two'):
            run=self.controller.run_for(supervisor['id'],ident)
            state=run.state()
            record(state,'worker','codex',{'input_tokens':100,'cached_input_tokens':70,'output_tokens':20},
                   'worker-1',model='worker-model',now=1)
            record(state,'supervisor','codex',{'input_tokens':30,'output_tokens':10},
                   'supervisor-1',model='supervisor-model',now=2)
            run.save_state(state)
        snapshot=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertEqual(snapshot['usage']['agents']['total_tokens'],240)
        self.assertEqual(snapshot['usage']['supervisor']['total_tokens'],80)
        self.assertEqual(snapshot['usage']['total']['total_tokens'],320)
        self.assertEqual(snapshot['tasks'][0]['usage']['agent']['cached_tokens'],70)
        self.assertNotIn('token_usage',snapshot['tasks'][0])  # No internal event-ID ledger in the stream.
        await self.post('/api/supervisor/agents/tokens-one/remove',{})
        await self.post('/api/supervisor/settings',{'supervisor':{'provider':'claude','model':'different-model'}})
        snapshot=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertEqual(snapshot['usage']['supervisor']['total_tokens'],80)
        self.assertEqual(snapshot['usage']['agents']['total_tokens'],240)
        self.assertEqual(snapshot['usage']['supervisor']['by_model'][0]['model'],'supervisor-model')

    async def test_existing_runs_are_adopted_into_one_supervisor_without_new_worktrees(self):
        first=await self.create()
        second=await self.create()
        worktrees={str(run.worktree) for entry in (first,second)
                   for _,run in self.controller.fleet(entry['id']).runs()}
        supervisor=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertEqual(len(supervisor['tasks']),2)
        self.assertEqual({t['worktree'] for t in supervisor['tasks']},worktrees)
        self.assertEqual(len({t['id'] for t in supervisor['tasks']}),2)
        again=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertEqual(again['id'],supervisor['id'])
        self.assertEqual(len(again['tasks']),2)

    async def test_single_supervisor_stream_reports_blocked_agent_and_optional_keeper(self):
        response=await self.client.get('/api/supervisor')
        supervisor=(await response.json())['supervisor']
        self.assertEqual(supervisor['tasks'],[])
        self.assertFalse(supervisor['keeper']['enabled'])
        response=await self.post('/api/supervisor/agents',{'tasks':self.manifest['tasks']})
        self.assertEqual(response.status,201)
        run=self.controller.run_for(supervisor['id'],'fix')
        run._wait(run.state(),'Authentication required')
        stream=await self.client.get('/api/events')
        self.assertEqual(stream.status,200)
        self.assertIn('text/event-stream',stream.headers['Content-Type'])
        async with asyncio.timeout(5):
            while True:
                line=(await stream.content.readline()).decode()
                if line.startswith('data: '):
                    snapshot=json.loads(line[6:])
                    self.assertEqual(snapshot['tasks'][0]['signal'],'blocked')
                    self.assertFalse(snapshot['keeper']['enabled'])
                    break
        stream.close()
        response=await self.post('/api/supervisor/settings',{
            'supervisor':{'provider':'codex','model':'custom-supervisor'},
            'keeper':{'enabled':True,'model':'installed-local-model'},'max_parallel':2})
        self.assertEqual(response.status,200)
        state=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertEqual(state['id'],supervisor['id'])
        self.assertTrue(state['keeper']['enabled'])
        await self.post('/api/supervisor/settings',{'keeper':{'enabled':False,'model':'installed-local-model'}})
        state=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        self.assertFalse(state['keeper']['enabled'])

    async def test_single_supervisor_accepts_agents_while_running_and_survives_completion(self):
        bindir=self.root/'bin';bindir.mkdir()
        cli=bindir/'codex'
        cli.write_text('''#!/usr/bin/env python3
import json, time, sys, os
from pathlib import Path
with open(os.environ['TEST_AGENT_CALLS'],'a') as log:
 log.write(('supervisor' if any('You supervise an independent' in a for a in sys.argv) else 'worker')+'\\n')
if 'ASK_ONLY' in sys.argv[-1]:
 time.sleep(1)
 text='Should I proceed?'
else:
 time.sleep(.3)
 Path('app.py').write_text('value = 2\\n')
 text='Implemented'
print(json.dumps({'item':{'type':'agent_message','text':text}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10}}))
''')
        cli.chmod(0o755)
        supervisor=(await (await self.client.get('/api/supervisor')).json())['supervisor']
        with mock.patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH'], 'TEST_AGENT_CALLS':str(self.root/'calls')}):
            try:
                response=await self.post('/api/supervisor/resume',{})
                self.assertEqual(response.status,200)
                pid=self.controller.processes[supervisor['id']].pid
                for ident in ('first','second'):
                    task={**self.manifest['tasks'][0],'id':ident}
                    response=await self.post('/api/supervisor/agents',{'tasks':[task]})
                    self.assertEqual(response.status,201,await response.text())
                    async with asyncio.timeout(15):
                        while True:
                            state=(await (await self.client.get('/api/supervisor')).json())['supervisor']
                            agent=next(t for t in state['tasks'] if t['id']==ident)
                            if agent['state']=='complete':
                                break
                            await asyncio.sleep(.1)
                    self.assertTrue(state['running'])
                    self.assertEqual(self.controller.processes[supervisor['id']].pid,pid)
                await self.post('/api/supervisor/agents/first/remove',{})
                state=(await (await self.client.get('/api/supervisor')).json())['supervisor']
                self.assertFalse(state['tasks'][0]['enabled'])
                self.assertEqual(state['tasks'][0]['signal'],'removed')
                await self.post('/api/supervisor/agents/first/restore',{})
                state=(await (await self.client.get('/api/supervisor')).json())['supervisor']
                self.assertTrue(state['tasks'][0]['enabled'])
                response=await self.post('/api/supervisor/agents',{'tasks':[
                    {**self.manifest['tasks'][0],'id':'remove-active','goal':'ASK_ONLY'}]})
                self.assertEqual(response.status,201)
                async with asyncio.timeout(10):
                    while self.controller.run_for(supervisor['id'],'remove-active').state()['state']!='working':
                        await asyncio.sleep(.05)
                await self.post('/api/supervisor/agents/remove-active/remove',{})
                async with asyncio.timeout(10):
                    while self.controller.run_for(supervisor['id'],'remove-active').state()['attempt']:
                        await asyncio.sleep(.1)
                state=(await (await self.client.get('/api/supervisor')).json())['supervisor']
                removed=next(t for t in state['tasks'] if t['id']=='remove-active')
                self.assertEqual(removed['signal'],'removed')
                self.assertEqual(removed['turns'],1)
                self.assertNotIn('supervisor',(self.root/'calls').read_text())
                # The deterministic keeper restarts an unexpectedly dead manager, not a new supervisor.
                process=self.controller.processes[supervisor['id']]
                process.kill()
                await asyncio.to_thread(process.wait)
                async with asyncio.timeout(8):
                    while self.controller.processes[supervisor['id']].pid==pid:
                        await asyncio.sleep(.1)
                state=(await (await self.client.get('/api/supervisor')).json())['supervisor']
                self.assertTrue(state['running'])
                self.assertEqual(state['tasks'][0]['state'],'complete')
            finally:
                await self.post('/api/supervisor/stop',{})
                process=self.controller.processes.get(supervisor['id'])
                if process:
                    await asyncio.wait_for(asyncio.to_thread(process.wait),5)

    async def test_project_library_scan_dedup_depth_and_persistence(self):
        response = await self.post('/api/projects',{'paths':[str(self.repo),str(self.repo)]})
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['added'],1)
        ignored=self.root/'node_modules'/'ignored';ignored.mkdir(parents=True)
        subprocess.run(['git','clone','-q',str(self.repo),str(ignored)],check=True)
        nested=self.root/'group'/'nested';nested.parent.mkdir()
        subprocess.run(['git','clone','-q',str(self.repo),str(nested)],check=True)
        (self.root/'repo-alias').symlink_to(self.repo,target_is_directory=True)
        response=await self.post('/api/projects',{'action':'scan','folder':str(self.root),'depth':1})
        self.assertEqual(response.status,200)
        self.assertEqual((await response.json())['added'],0)
        response=await self.post('/api/projects',{'action':'scan','folder':str(self.root),'depth':2})
        self.assertEqual((await response.json())['added'],1)
        library=await (await self.client.get('/api/projects')).json()
        self.assertEqual({p['path'] for p in library['projects']},{str(self.repo.resolve()),str(nested.resolve())})
        self.assertTrue(all(p['available'] for p in library['projects']))
        self.assertEqual(len(library['folders']),1)
        self.assertEqual(library['folders'][0]['depth'],2)
        self.assertEqual(len(json.loads(self.controller.project_index.read_text())['projects']),2)
        response=await self.post('/api/projects',{'action':'remove','path':str(nested)})
        self.assertEqual(response.status,200)
        response=await self.post('/api/projects',{'action':'rescan'})
        self.assertEqual((await response.json())['added'],1)
        response=await self.post('/api/projects',{'paths':[str(self.root/'missing')]})
        self.assertEqual(response.status,400)
        response=await self.post('/api/projects',{'action':'scan','folder':str(self.root),'depth':-1})
        self.assertEqual(response.status,400)

    async def test_subagent_toggle_persists_and_is_captured_at_next_launch(self):
        entry=await self.create()
        endpoint=f"/api/fleets/{entry['id']}/tasks/fix/settings"
        response=await self.post(endpoint,{'allow_subagents':True})
        self.assertEqual(response.status,200)
        run=self.controller.run_for(entry['id'],'fix')
        self.assertTrue(run.config['allow_subagents'])
        self.assertEqual(run.config['scope'],['.'])
        self.assertEqual(run.config['protected'],[])
        response=await self.client.get('/api/state')
        self.assertTrue((await response.json())['fleets'][0]['tasks'][0]['allow_subagents'])
        state=run.state()
        with mock.patch.object(run,'_spawn'):
            run._launch(state)
        record=json.loads(run._attempt_path(run.state()).read_text())
        self.assertTrue(record['task']['allow_subagents'])
        response=await self.post(endpoint,{'allow_subagents':False})
        self.assertEqual(response.status,200)
        self.assertTrue(json.loads(run._attempt_path(run.state()).read_text())['task']['allow_subagents'])
        self.assertFalse(run.config['allow_subagents'])
        response=await self.post(endpoint,{'allow_subagents':'yes'})
        self.assertEqual(response.status,400)

    async def test_output_keeps_bytes_arriving_during_a_slow_socket_send(self):
        from types import SimpleNamespace
        terminal = object.__new__(Terminal)
        terminal.buffer = bytearray(b'first')
        terminal.total = 5
        terminal.event = asyncio.Event()
        terminal.exited = False
        terminal.process = SimpleNamespace(returncode=0)
        chunks = []
        class Socket:
            closed = False
            async def send_bytes(self, chunk):
                chunks.append(chunk)
                if len(chunks) == 1:
                    terminal.buffer.extend(b'second')
                    terminal.total += 6
                    terminal.exited = True
                    terminal.event.set()
                    await asyncio.sleep(0)
            async def send_json(self, data):
                pass
            async def close(self, **kwargs):
                self.closed = True
        await asyncio.wait_for(terminal.output(Socket()),2)
        self.assertEqual(b''.join(chunks), b'firstsecond')

    async def test_create_status_note_import_and_static_assets(self):
        entry = await self.create()
        response = await self.client.get('/api/state')
        fleet = (await response.json())['fleets'][0]
        self.assertFalse(fleet['running'])
        self.assertEqual(fleet['tasks'][0]['goal'],'Set value to 2')
        self.assertIsNone(fleet['tasks'][0]['attempt'])
        with mock.patch.object(self.controller, 'resume'):
            response = await self.post(f"/api/fleets/{entry['id']}/tasks/fix/note",{'text':'Use existing conventions'})
        self.assertEqual(response.status,200)
        run = self.controller.run_for(entry['id'],'fix')
        self.assertIn('Use existing conventions',run.path('inbox.jsonl').read_text())
        response = await self.post('/api/import',{'home':entry['home']})
        self.assertEqual(response.status,400)  # Duplicate registration cannot create another manager.
        for path in ('/app.js','/styles.css','/vendor/xterm.js','/vendor/xterm.css','/vendor/fit.js'):
            response = await self.client.get(path)
            self.assertEqual(response.status,200,path)
            self.assertGreater(len(await response.read()),100)
        response = await self.client.get('/api/fleets/unknown/logs')
        self.assertEqual(response.status,404)

    async def test_origin_host_cookie_and_mutation_boundaries(self):
        response = await self.client.get('/api/state',headers={'Origin':'http://evil.example'})
        self.assertEqual(response.status,403)
        response = await self.client.get('/',headers={'Host':'evil.example'})
        self.assertEqual(response.status,403)
        response = await self.client.post('/api/fleets',json={'manifest':self.manifest})
        self.assertEqual(response.status,403)
        self.client.session.cookie_jar.clear()
        response = await self.client.get('/api/state')
        self.assertEqual(response.status,401)

    async def test_malformed_requests_return_useful_errors(self):
        for body in ([],{}, {'manifest':{}}):
            response = await self.post('/api/fleets',body)
            self.assertEqual(response.status,400)
            self.assertIn('error',await response.json())

    async def test_real_shell_resize_reconnect_ownership_and_close(self):
        entry = await self.create()
        ws = await self.ws(entry)
        await ws.send_json({'type':'resize','cols':90,'rows':24})
        await ws.send_json({'type':'input','data':"printf '__PTY_RESULT__%s\\n' $((2+3)); printf browser > terminal.txt\n"})
        result = await self.output_until(ws,b'__PTY_RESULT__5')
        self.assertIn(b'__PTY_RESULT__5',result)
        terminal = self.controller.terminals[(entry['id'],'fix')]
        pid = terminal.process.pid
        run = self.controller.run_for(entry['id'],'fix')
        self.assertTrue(lock_held(run.path('keeper.lock')))
        response = await self.post(f"/api/fleets/{entry['id']}/resume",{})
        self.assertEqual(response.status,400)
        with self.assertRaises(WSServerHandshakeError):
            await self.ws(entry)
        await ws.close()
        for _ in range(20):
            if terminal.ws is None:
                break
            await asyncio.sleep(.01)
        ws = await self.ws(entry)
        await self.output_until(ws,b'__PTY_RESULT__5')
        self.assertEqual(terminal.process.pid,pid)
        await ws.send_json({'type':'input','data':"printf '__RECONNECTED__%s\\n' $((4+5))\n"})
        await self.output_until(ws,b'__RECONNECTED__9')
        self.assertEqual((run.worktree/'terminal.txt').read_text(),'browser')
        self.assertFalse((self.repo/'terminal.txt').exists())
        response = await self.post(f"/api/fleets/{entry['id']}/tasks/fix/terminal/close",{})
        self.assertEqual(response.status,200)
        self.assertFalse(lock_held(run.path('keeper.lock')))
        self.assertIsNotNone(terminal.process.poll())
        await ws.close()

    async def test_closing_an_exited_shell_never_signals_its_old_pid(self):
        entry = await self.create()
        ws = await self.ws(entry)
        terminal = self.controller.terminals[(entry['id'],'fix')]
        await ws.send_json({'type':'input','data':'exit\n'})
        await asyncio.wait_for(asyncio.shield(terminal.reaper),5)
        with mock.patch('loop.web.os.killpg') as kill:
            response = await self.post(f"/api/fleets/{entry['id']}/tasks/fix/terminal/close",{})
            self.assertEqual(response.status,200)
            kill.assert_not_called()
        await ws.close()

    async def test_manager_launch_and_completion_through_api(self):
        bindir = self.root/'bin';bindir.mkdir()
        cli = bindir/'codex'
        cli.write_text('''#!/usr/bin/env python3
import json
from pathlib import Path
Path('app.py').write_text('value = 2\\n')
print(json.dumps({'item':{'type':'agent_message','text':'Implemented'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10}}))
''')
        cli.chmod(0o755)
        entry = await self.create()
        with mock.patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH']}):
            response = await self.post(f"/api/fleets/{entry['id']}/resume",{})
            self.assertEqual(response.status,200,await response.text())
            # A second request cannot launch a competing manager during startup.
            response = await self.post(f"/api/fleets/{entry['id']}/resume",{})
            self.assertEqual(response.status,400)
        async with asyncio.timeout(15):
            while True:
                response = await self.client.get('/api/state')
                state = (await response.json())['fleets'][0]
                if state['tasks'][0]['state'] == 'complete' and not state['running']:
                    break
                await asyncio.sleep(.1)
        response = await self.client.get(f"/api/fleets/{entry['id']}/tasks/fix/logs")
        logs = await response.json()
        self.assertIn('Implemented',logs['output'])
        self.assertEqual((self.repo/'app.py').read_text(),'value = 1\n')
        response = await self.post(f"/api/fleets/{entry['id']}/stop",{})
        self.assertEqual(response.status,200)


if __name__ == '__main__':
    unittest.main()
