"""Disposable local app instance for headless browser tests; never uses live models."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from aiohttp import web
from loop.web import create_app, APP_KEY

parser = argparse.ArgumentParser()
parser.add_argument('--port',type=int,default=8776)
args = parser.parse_args()
with tempfile.TemporaryDirectory(prefix='agent-loop-ui-') as temporary:
    root = Path(temporary)
    repo = root/'repo';repo.mkdir()
    for argv in [('init','-q'),('config','user.email','test@example.invalid'),('config','user.name','Browser test')]:
        subprocess.run(['git','-C',str(repo),*argv],check=True)
    (repo/'app.py').write_text('value = 1\n')
    subprocess.run(['git','-C',str(repo),'add','.'],check=True)
    subprocess.run(['git','-C',str(repo),'commit','-qm','initial'],check=True)
    second_repo=root/'another-project'
    subprocess.run(['git','clone','-q',str(repo),str(second_repo)],check=True)
    bindir=root/'bin';bindir.mkdir()
    fake=bindir/'codex'
    fake.write_text('''#!/usr/bin/env python3
import json, time, sys
from pathlib import Path
if '--json' not in sys.argv:
 print(chr(27)+'[32mCodex interactive terminal'+chr(27)+'[0m',flush=True)
 print('TTY='+str(sys.stdin.isatty() and sys.stdout.isatty()),flush=True)
 print('Resume='+sys.argv[-1] if 'resume' in sys.argv else 'New chat',flush=True)
 for line in sys.stdin:
  if line.strip() == '/exit': break
  print('Codex reply: '+line.strip(),flush=True)
 sys.exit(0)
if 'WATCH_WORKER_OUTPUT' in sys.argv[-1] and 'Read-only objective review.' not in sys.argv[-1]:
 print(json.dumps({'item':{'type':'agent_message','text':'Worker is running; live output is available.'}}),flush=True)
 time.sleep(6)
time.sleep(.4)
supervisor=any('You supervise an independent' in argument for argument in sys.argv) or 'Read-only objective review.' in sys.argv[-1]
if 'Read-only objective review.' in sys.argv[-1]:
 text=json.dumps({'verdict':'pass','problems':[]})
 usage={'input_tokens':50,'cached_input_tokens':5,'output_tokens':10}
elif supervisor:
 text=json.dumps({'action':'continue','message':'Use value 2 and run acceptance.'})
 usage={'input_tokens':100,'cached_input_tokens':20,'output_tokens':30}
elif 'ASK_TOKEN_QUESTION' in sys.argv[-1] and 'resume' not in sys.argv:
 text='Should I set the value to 2?'
 usage={'input_tokens':12,'cached_input_tokens':4,'output_tokens':8}
else:
 Path('app.py').write_text('value = 2\\n')
 text='**Updated app.py** and verified value 2.'
 usage={'input_tokens':12,'cached_input_tokens':4,'output_tokens':8}
print(json.dumps({'type':'thread.started','thread_id':'browser-supervisor' if supervisor else 'browser-worker'}))
if not supervisor:
 print(json.dumps({'type':'item.completed','item':{'id':'command-check','type':'command_execution','command':'python3 -c check_value','aggregated_output':'Value check passed','status':'completed','exit_code':0}}))
print(json.dumps({'item':{'type':'agent_message','text':text}}))
print(json.dumps({'type':'turn.completed','usage':usage}))
''')
    fake.chmod(0o755)
    codexhome=root/'codex-home';codexhome.mkdir()
    import json
    (codexhome/'models_cache.json').write_text(json.dumps({'models':[
        {'slug':'fixture-model','display_name':'Fixture Model','visibility':'list','supported_reasoning_levels':[{'effort':v} for v in ('low','medium','high','xhigh')]}]}))
    os.environ['CODEX_HOME']=str(codexhome)
    os.environ['PATH']=str(bindir)+os.pathsep+os.environ['PATH']
    os.environ['AGENT_LOOP_REGISTRY']=str(root/'registry.sqlite')
    app=create_app(root/'web')
    app[APP_KEY].shell='/bin/sh'
    async def fixture(request):
        return web.json_response({'repo':str(repo.resolve()),'secondRepo':str(second_repo.resolve()),
            'projectsFolder':str(root.resolve()), 'nativeCommand':__import__('shlex').quote(str(fake)),
            'acceptance':'python3 -B -c "from app import value; assert value == 2"'})
    app.router.add_get('/test-fixture',fixture)
    web.run_app(app,host='127.0.0.1',port=args.port)
