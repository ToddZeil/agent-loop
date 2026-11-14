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
