async function workspaceSettings(page) { if(await page.locator('.settings-menu').getAttribute('open') === null) await page.locator('.settings-menu > summary').click(); }
const {test,expect} = require('@playwright/test');
const path = require('node:path');
async function optionalSettings(page) { if(await page.locator('#optional-settings').getAttribute('open') === null) await page.locator('#optional-settings > summary').click(); }

async function agentSettings(page) { if(await page.locator('#agent-settings').getAttribute('open') === null) await page.locator('#agent-settings > summary').click(); }

test('one supervisor: add an agent, use a real terminal, send direction, and complete',async ({page,request}) => {
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await expect(page.locator('#removed-agents')).toBeHidden();
  await page.getByRole('button',{name:'Add your first agent'}).click();
  await optionalSettings(page);
  await page.getByLabel('Agent ID',{exact:true}).fill('browser-fix');
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Set app.py value to 2.');
  await page.getByLabel('Verification command (optional)',{exact:true}).fill(fixture.acceptance);
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#title')).toHaveText('Agent workspace');
  await expect(page.locator('#stat-total')).toHaveText('1');
  await expect(page.locator('#task-status')).toHaveText('ready');
  await page.getByRole('tab',{name:'Conversation',exact:true}).click();
  await page.locator('#shell-settings > summary').click();
  await page.getByRole('button',{name:'Open terminal',exact:true}).click();
  await expect(page.locator('#terminal-state')).toContainText('Connected');
  await page.locator('.xterm-helper-textarea').focus();
  await page.keyboard.type("printf '__BROWSER_PTY__%s\\n' $((3+4))");
  await page.keyboard.press('Enter');
  await page.waitForFunction(()=>{
    if(!terminal) return false;
    const buffer=terminal.buffer.active;
    return Array.from({length:buffer.length},(_,i)=>buffer.getLine(i).translateToString()).join('\n').includes('__BROWSER_PTY__7');
  });
  await expect(page.getByRole('button',{name:'Start supervisor',exact:true})).toBeDisabled();
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-desktop.png'),fullPage:true});
  await page.getByRole('button',{name:'Close shell',exact:true}).click();
  await expect(page.getByRole('button',{name:'Start supervisor',exact:true})).toBeEnabled();
  await page.getByLabel('Give this worker direction',{exact:true}).fill('Preserve the existing file format.');
  await page.getByRole('button',{name:'Send',exact:false}).click();
  await expect(page.locator('#toast')).toContainText('Direction saved');
  await expect(page.locator('#manager-state')).toContainText('running');
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await expect(page.locator('#stat-done')).toHaveText('1');
  await page.getByRole('tab',{name:'Run log',exact:true}).click();
  await expect(page.locator('#activity')).toContainText('Updated app.py');
  await expect(page.locator('#manager-state')).toContainText('running');
  await page.getByRole('tab',{name:'Conversation',exact:true}).click();
  await expect(page.locator('#worker-terminal-output')).toContainText('Updated app.py');
  await expect(page.locator('#worker-terminal-output .conversation-text strong').first()).toHaveText('Updated app.py');
  const command = page.locator('#worker-terminal-output .command-card').filter({hasText:'python3 -c check_value'}).first();
  await command.locator('summary').click();
  await expect(command.locator('pre')).toBeVisible();
  await expect(command.locator('pre')).toContainText('Value check passed');
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-conversation.png'),fullPage:true});
  await expect(page.locator('#open-terminal')).toBeDisabled();
  await expect(page.locator('#terminal')).toBeHidden();
  await page.goto('/?agent=browser-fix&tab=terminal');
  await expect(page.locator('#task-title')).toHaveText('browser-fix');
  await expect(page.locator('#worker-terminal-output')).toBeVisible();
  await expect(page.locator('#worker-terminal-output')).toContainText('Updated app.py');
  await expect(page.locator('#connection')).toHaveText('Live stream connected');
  expect(errors).toEqual([]);
});

test('add another agent to the running supervisor through advanced manifest; mobile fits',async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.setViewportSize({width:390,height:844});
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await optionalSettings(page);
  await optionalSettings(page);
  await page.locator('textarea[name="manifest"]').fill(JSON.stringify({tasks:[{id:'manifest-worker',repo:fixture.repo,goal:'Set value to 2',acceptance:[fixture.acceptance]}]}));
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#task-title')).toHaveText('manifest-worker');
  await expect(page.locator('#stat-total')).toHaveText('2');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  const cards = page.locator('.worker-card');
  await expect(cards).toHaveCount(2);
  const [first,second] = await cards.evaluateAll(elements=>elements.map(element=>{const rect=element.getBoundingClientRect();return {x:rect.x,y:rect.y,height:rect.height};}));
  expect(second.y).toBeGreaterThanOrEqual(first.y + first.height);
  expect(Math.abs(first.x - second.x)).toBeLessThan(1);
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-mobile.png'),fullPage:true});
});

test('scan repositories, choose one, persist subagent toggle, and remove/restore an agent',async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await workspaceSettings(page);
  await page.locator('#projects').click();
  await page.getByLabel('Projects folder',{exact:true}).fill(fixture.projectsFolder);
  await page.getByRole('button',{name:'Scan & add repositories',exact:true}).click();
  await expect(page.locator('#saved-projects')).toContainText('another-project');
  await expect(page.locator('#project-count')).toHaveText('2');
  await page.getByRole('button',{name:'Close projects',exact:true}).click();
  await page.locator('#new-fleet').click();
  await optionalSettings(page);
  await page.getByLabel('Agent ID',{exact:true}).fill('selected-repo');
  await page.getByLabel('Saved project',{exact:true}).selectOption(fixture.secondRepo);
  await expect(page.getByLabel('Repository root',{exact:true})).toHaveValue(fixture.secondRepo);
  await page.getByLabel('Goal',{exact:true}).fill('Set value to 2');
  await page.getByLabel('Verification command (optional)',{exact:true}).fill(fixture.acceptance);
  await page.locator('[data-field="allow-subagents"]').check();
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#task-title')).toHaveText('selected-repo');
  await agentSettings(page);
  await expect(page.locator('#allow-subagents')).toBeChecked();
  await expect(page.locator('#full-access')).toBeChecked();
  await page.locator('#full-access').uncheck();
  await expect(page.locator('#toast')).toContainText('Permissions saved');
  await expect(page.locator('.worker-policy')).toContainText('All project files');
  await page.locator('#allow-subagents').uncheck();
  await expect(page.locator('#toast')).toContainText('Subagent setting saved');
  await page.reload();
  await page.locator('[data-task="selected-repo"]').click();
  await agentSettings(page);
  await expect(page.locator('#allow-subagents')).not.toBeChecked();
  await expect(page.locator('#full-access')).not.toBeChecked();
  await page.locator('#full-access').check();
  await expect(page.locator('#toast')).toContainText('Permissions saved');
  await page.locator('#remove-agent').click();
  await workspaceSettings(page);
  await expect(page.locator('[data-restore="selected-repo"]')).toBeVisible({timeout:15000});
  await expect(page.locator('[data-task="selected-repo"]')).toHaveCount(0);
  await page.locator('[data-restore="selected-repo"]').click();
  await expect(page.locator('[data-task="selected-repo"]')).toBeVisible();
  await workspaceSettings(page);
  await page.locator('#projects').click();
  await expect(page.locator('#saved-projects')).toContainText('another-project');
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-projects.png'),fullPage:true});
});

test('delete and clean up current and archived agents, with cancellation', async ({page,request}) => {
  const fixture = await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.waitForFunction(()=>typeof currentTask==='function' && currentTask());
  const wasRunning=await page.locator('#stop').isEnabled();
  if(wasRunning) await page.locator('#stop').click();
  await expect(page.locator('#manager-state')).toContainText('stopped',{timeout:10000});
  await page.evaluate(async repo => {
    await api('/supervisor/agents', {tasks:[{id:'delete-ui',repo,goal:'Cleanup test'}]});
    await refresh();
  }, fixture.repo);
  await page.locator('[data-task="delete-ui"]').click();
  await agentSettings(page);
  page.once('dialog', dialog => dialog.dismiss());
  await page.locator('#delete-agent').click();
  await expect(page.locator('[data-task="delete-ui"]')).toBeVisible();
  await page.locator('#remove-agent').click();
  await workspaceSettings(page);
  await expect(page.locator('[data-delete="delete-ui"]')).toBeVisible();
  page.once('dialog', async dialog => {
    expect(dialog.message()).toContain('cannot be undone');
    await dialog.accept();
  });
  await page.locator('[data-delete="delete-ui"]').click();
  await expect(page.locator('[data-delete="delete-ui"]')).toHaveCount(0);
  await expect(page.locator('#toast')).toContainText('Agent deleted');
  await page.reload();
  await expect(page.locator('[data-task="delete-ui"]')).toHaveCount(0);
  await expect(page.locator('[data-restore="delete-ui"]')).toHaveCount(0);
  await page.evaluate(async repo => {
    await api('/supervisor/agents', {tasks:[{id:'delete-ui',repo,goal:'Cleanup test'}]});
    await refresh();
  }, fixture.repo);
  await page.locator('[data-task="delete-ui"]').click();
  await agentSettings(page);
  page.once('dialog', dialog => dialog.accept());
  await page.locator('#delete-agent').click();
  await expect(page.locator('[data-task="delete-ui"]')).toHaveCount(0);
  await expect(page.locator('#toast')).toContainText('Agent deleted');
  if(wasRunning) {
    await page.locator('#resume').click();
    await expect(page.locator('#manager-state')).toContainText('running');
  }
});

test('tile options delete a working agent while the supervisor and other agents stay running', async ({page,request}) => {
  const fixture = await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.evaluate(async ({repo,acceptance}) => {
    await api('/supervisor/agents', {start:true, tasks:[
      {id:'menu-keep',repo,goal:'Set value to 2',acceptance:[acceptance]},
      {id:'menu-delete',repo,goal:'WATCH_WORKER_OUTPUT',acceptance:['true']}
    ]});
    await refresh();
  }, fixture);
  await page.locator('[data-task="menu-delete"]').click();
  await expect(page.locator('#task-status')).toHaveText('working', {timeout:15000});
  await page.locator('[data-task="menu-keep"]').click();
  const card = page.locator('[data-task="menu-delete"]');
  expect((await card.boundingBox()).height).toBe(100);
  await card.locator('summary').click();
  await expect(page.locator('#task-title')).toHaveText('menu-keep');
  await expect(card.getByRole('button', {name:'Delete & clean up',exact:true})).toBeEnabled();
  page.once('dialog', dialog => dialog.dismiss());
  await card.getByRole('button', {name:'Delete & clean up',exact:true}).click();
  await expect(card).toBeVisible();
  page.once('dialog', dialog => dialog.accept());
  await card.getByRole('button', {name:'Delete & clean up',exact:true}).click();
  await expect(card).toContainText('Deletion queued');
  await page.reload();
  await expect(card).toHaveCount(0, {timeout:20000});
  await expect(page.locator('[data-task="menu-keep"]')).toBeVisible();
  await expect(page.locator('#manager-state')).toContainText('running');
  await page.setViewportSize({width:390,height:844});
  await page.locator('[data-task="menu-keep"] summary').click();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
  // Exercise Archive from the same tile menu, preserving its worktree for restore.
  await page.locator('[data-task="menu-keep"] [data-archive]').click();
  await workspaceSettings(page);
  await expect(page.locator('[data-restore="menu-keep"]')).toBeVisible({timeout:10000});
  await page.locator('[data-restore="menu-keep"]').click();
  await expect(page.locator('[data-task="menu-keep"]')).toBeVisible();
  await page.evaluate(async () => { await api('/supervisor/agents/menu-keep/delete',{confirm:true}); });
  await expect(page.locator('[data-task="menu-keep"]')).toHaveCount(0,{timeout:10000});
});

test('local keeper is optional and can be enabled or disabled without replacing the supervisor',async ({page}) => {
  await page.goto('/');
  await workspaceSettings(page);
  await page.locator('#supervisor-settings').click();
  await expect(page.getByLabel('Enable optional local keeper')).not.toBeChecked();
  await page.getByLabel('Enable optional local keeper').check();
  await page.getByLabel('Local keeper model',{exact:true}).fill('installed-local-model');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('#keeper-status')).toContainText('installed-local-model');
  await workspaceSettings(page);
  await page.locator('#supervisor-settings').click();
  await page.getByLabel('Enable optional local keeper').uncheck();
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('#keeper-status')).toContainText('off');
  await page.getByRole('button',{name:'Stop scheduling',exact:true}).click();
  await expect(page.locator('#manager-state')).toContainText('stopped',{timeout:10000});
});


test('agent and supervisor token reports update live and survive refresh',async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await optionalSettings(page);
  await page.getByLabel('Agent ID',{exact:true}).fill('token-worker');
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('ASK_TOKEN_QUESTION: Set the value to 2 after answering the question.');
  await page.getByLabel('Verification command (optional)',{exact:true}).fill(fixture.acceptance);
  await page.locator('#create-form button[name=start]').click();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await expect(page.locator('#supervisor-token-total')).toHaveText('130');
  await expect(page.locator('#supervisor-token-input')).toHaveText('100');
  await expect(page.locator('#supervisor-token-output')).toHaveText('30');
  await expect(page.locator('#supervisor-token-cached')).toHaveText('20');
  // A slower HTTP snapshot must not overwrite a newer event-stream token total.
  await page.evaluate(()=>{
    const stale=structuredClone(currentFleet());
    stale.snapshot_revision -= 1;
    stale.usage.supervisor.total_tokens=999999;
    applySupervisor(stale);render();
  });
  await expect(page.locator('#supervisor-token-total')).toHaveText('130');
  await page.getByRole('tab',{name:'Tokens',exact:true}).click();
  await expect(page.locator('#agent-usage-total')).toHaveText('40');
  await expect(page.locator('#agent-usage-input')).toHaveText('24');
  await expect(page.locator('#agent-usage-output')).toHaveText('16');
  await expect(page.locator('#agent-usage-cached')).toHaveText('8');
  await expect(page.locator('#usage-panel')).toContainText('Supervisor assistance: 130');
  await page.locator('#token-details > summary').click();
  await page.getByText('Supervisor usage by model',{exact:true}).click();
  await expect(page.locator('#supervisor-usage-models')).toContainText('130');
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-tokens.png'),fullPage:true});
  await page.reload();
  await page.locator('[data-task="token-worker"]').click();
  await expect(page.locator('#supervisor-token-total')).toHaveText('130');
  await page.getByRole('tab',{name:'Tokens',exact:true}).click();
  await expect(page.locator('#agent-usage-total')).toHaveText('40');
  await page.getByRole('button',{name:'Stop scheduling',exact:true}).click();
  await expect(page.locator('#manager-state')).toContainText('stopped',{timeout:10000});
});


test('simple setup needs only a repository and goal; supervisor reviews the result',async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await expect(page.getByLabel('Verification command (optional)',{exact:true})).toBeHidden();
  await expect(page.getByLabel('Agent ID',{exact:true})).toBeHidden();
  await expect(page.locator('textarea[name="manifest"]')).toBeHidden();
  await page.getByLabel('Saved project',{exact:true}).selectOption(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Set app.py value to 2 and verify the result.');
  await page.locator('#create-form button[name=start]').click();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await page.getByRole('tab',{name:'Decisions',exact:true}).click();
  await expect(page.locator('#decisions-panel')).toContainText('SUPERVISOR REVIEW');
  await expect(page.locator('#decisions-panel')).toContainText('Result review passed.');
  const data=await (await page.request.get('/api/supervisor')).json();
  const selected=await page.locator('#task-title').textContent();
  const agent=data.supervisor.tasks.find(t=>t.id===selected);
  expect(agent.review_evidence.verdict.verdict).toBe('pass');
  expect(agent.usage.supervisor.total_tokens).toBe(60);
  expect(agent.usage.worker.total_tokens).toBe(20);
  await page.locator('#new-fleet').click();
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-simple-setup.png'),fullPage:true});
  await page.getByRole('button',{name:'Close dialog',exact:true}).click();
  await page.getByRole('button',{name:'Stop scheduling',exact:true}).click();
  await expect(page.locator('#manager-state')).toContainText('stopped',{timeout:10000});
});


test('cached exact model IDs are selectable for workers and supervisor; existing models can be repaired', async ({page}) => {
  await page.goto('/');
  await page.locator('#new-fleet').click();
  const picker=page.locator('#task-fields [data-model-picker]');
  await expect(picker.first().locator('option[value="fixture-model"]')).toHaveText('Fixture Model · fixture-model');
  await picker.first().selectOption('fixture-model');
  await expect(page.locator('#task-fields [data-field=model]')).toHaveValue('fixture-model');
  await page.locator('#create-dialog [data-close]').click();
  await workspaceSettings(page);
  await page.locator('#supervisor-settings').click();
  await expect(page.locator('#supervisor-dialog [data-model-picker] option[value="fixture-model"]')).toBeAttached();
  await page.locator('#supervisor-dialog [data-close]').click();
  if (await page.locator('#stop').isEnabled()) await page.locator('#stop').click();
  await agentSettings(page);
  await expect(page.locator('#save-agent-model')).toBeEnabled();
  await page.locator('.agent-model-settings [data-model-picker]').selectOption('fixture-model');
  await page.locator('#save-agent-model').click();
  await expect(page.locator('#toast')).toContainText('Model saved');
  await expect(page.locator('.agent-model-settings [data-model-picker]')).toHaveValue('fixture-model');
  await page.locator('.agent-model-settings [data-model-picker]').selectOption('__custom');
  await page.locator('.agent-model-settings input[aria-label="Custom model ID"]').fill('saved-custom-model');
  await page.locator('#save-agent-model').click();
  await page.reload();
  await page.locator('.worker-card').last().click();
  await expect(page.locator('.agent-model-settings [data-model-picker] option[value="saved-custom-model"]')).toBeAttached();
});


test('terminal shows live worker output during an active turn without opening a shell', async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('WATCH_WORKER_OUTPUT: set value to 2.');
  await page.getByRole('button',{name:'Add & start supervisor'}).click();
  await page.getByRole('tab',{name:'Conversation',exact:true}).click();
  await expect(page.locator('#worker-terminal-output')).toContainText('Worker is running; live output is available.');
  await expect(page.locator('#task-status')).toHaveText('working');
  await expect(page.locator('#open-terminal')).toBeDisabled();
  await expect(page.locator('#terminal')).toBeHidden();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await expect(page.locator('#worker-terminal-output')).toContainText('Updated app.py');
  await page.locator('#stop').click();
});


test('sending follow-up to a completed worker starts a stopped supervisor and resumes the worker', async ({page}) => {
  await page.goto('/');
  await expect(page.locator('#manager-state')).toContainText('stopped',{timeout:10000});
  const taskId=await page.locator('#task-title').textContent();
  const before=(await (await page.request.get('/api/supervisor')).json()).supervisor.tasks.find(t=>t.id===taskId).turns;
  await page.getByLabel('Give this worker direction',{exact:true}).fill('Recheck that app.py value is 2; no changes are needed.');
  await page.getByRole('button',{name:'Send',exact:false}).click();
  await expect(page.locator('#manager-state')).toContainText('running');
  await expect.poll(async () => (await (await page.request.get('/api/supervisor')).json()).supervisor.tasks.find(t=>t.id===taskId).turns).toBeGreaterThan(before);
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await page.locator('#stop').click();
});


test('effort is selectable for new and existing agents and supervisor; parallel cap is optional', async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  for(const provider of ['claude','antigravity','muse','codex']) {
    await page.locator('#task-fields [data-field=provider]').selectOption(provider);
    await expect(page.getByLabel('Worker effort',{exact:true})).toBeVisible();
    await page.getByLabel('Worker effort',{exact:true}).selectOption('high');
  }
  await page.locator('#task-fields [data-model-picker]').selectOption('fixture-model');
  await page.getByLabel('Worker effort',{exact:true}).selectOption('xhigh');
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Verify effort controls.');
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await agentSettings(page);
  await expect(page.locator('#agent-effort')).toHaveValue('xhigh');
  await page.locator('#agent-effort').selectOption('low');
  await page.locator('#save-agent-effort').click();
  const agentId=await page.locator('#task-title').textContent();
  await expect.poll(async()=> (await (await page.request.get('/api/supervisor')).json()).supervisor.tasks.find(t=>t.id===agentId)?.worker_effort).toBe('low');
  await workspaceSettings(page);
  await page.locator('#supervisor-settings').click();
  await page.getByLabel('Supervisor effort',{exact:true}).selectOption('high');
  await page.getByLabel('Unlimited parallel workers',{exact:true}).check();
  await expect(page.locator('#supervisor-form [name=parallel]')).toBeDisabled();
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  const settings=(await (await page.request.get('/api/supervisor')).json()).supervisor;
  expect(settings.max_parallel).toBeNull();
  expect(settings.supervisor.effort).toBe('high');
  await workspaceSettings(page);
  await page.locator('#supervisor-settings').click();
  await expect(page.getByLabel('Unlimited parallel workers',{exact:true})).toBeChecked();
  await page.locator('#supervisor-dialog [data-close]').click();
  await page.getByRole('tab',{name:'Conversation',exact:true}).click();
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-conversation-effort.png'),fullPage:true});
});


test('open tabs report their UI revision and offer safe reload when files change',async ({page,request})=>{
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Verify interface reload.');
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#ui-build')).toHaveText(/^UI [a-f0-9]{10}$/);
  await expect(page.locator('#ui-update')).toBeHidden();
  const url=await page.locator('script[src*="app.js"]').getAttribute('src');
  expect(url).toMatch(/app\.js\?v=[a-f0-9]{10}/);
  const state=(await (await page.request.get('/api/supervisor')).json()).supervisor;
  const agentId=await page.locator('#task-title').textContent();
  await page.route('**/api/events',route=>route.fulfill({status:200,contentType:'text/event-stream',body:'event: state\ndata: '+JSON.stringify({...state,snapshot_revision:state.snapshot_revision+1,ui_revision:'different'})+'\n\n'}));
  await page.goto('/?agent='+encodeURIComponent(agentId));
  await expect(page.getByRole('button',{name:'Reload interface',exact:true})).toBeVisible();
  await page.getByLabel('Give this worker direction',{exact:true}).fill('Keep this unsent draft.');
  await page.unroute('**/api/events');
  await page.getByRole('button',{name:'Reload interface',exact:true}).click();
  await expect(page.getByLabel('Give this worker direction',{exact:true})).toHaveValue('Keep this unsent draft.');
  await expect(page.locator('#ui-update')).toBeHidden();
});


test('desktop opens directly to a readable conversation with its composer in view',async ({page,request})=>{
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await expect(page.locator('#connection')).toHaveText('Live stream connected');
  const initialWorkerCount = await page.locator('.worker-card').count();
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-redesign-empty.png'),fullPage:true});
  await page.locator('#new-fleet').click();
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Ready for design review.');
  await page.locator('#add-task').click();
  await page.getByLabel('Repository root',{exact:true}).nth(1).fill(fixture.secondRepo);
  await page.getByLabel('Goal',{exact:true}).nth(1).fill('Review a second project.');
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#create-dialog')).toBeHidden();
  await expect(page.locator('#worker-terminal-output')).toContainText('Ready for design review.');
  await expect(page.getByRole('tab',{name:'Conversation',exact:true})).toHaveAttribute('aria-selected','true');
  const conversationTab = page.getByRole('tab',{name:'Conversation',exact:true});
  await expect(page.getByRole('tab').first()).toHaveText('Conversation');
  await conversationTab.focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.getByRole('tab',{name:'Run log',exact:true})).toBeFocused();
  await expect(page.getByRole('tabpanel',{name:'Run log',exact:true})).toBeVisible();
  await page.keyboard.press('End');
  await expect(page.getByRole('tab',{name:'Decisions',exact:true})).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(conversationTab).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(page.getByRole('tab',{name:'Decisions',exact:true})).toBeFocused();
  await page.keyboard.press('Home');
  await expect(conversationTab).toBeFocused();
  await expect(page.getByRole('tabpanel',{name:'Conversation',exact:true})).toBeVisible();
  expect(await page.locator('#agent-settings').getAttribute('open')).toBeNull();
  await expect(page.locator('.agent-model-settings [data-model-picker]')).toBeHidden();
  await expect(page.locator('#agent-effort')).toBeHidden();
  await agentSettings(page);
  await expect(page.locator('.agent-model-settings [data-model-picker]')).toBeVisible();
  await expect(page.locator('#agent-effort')).toBeVisible();
  await page.locator('.agent-model-settings .model-refresh').click();
  await expect(page.locator('#toast')).toContainText('Model catalogue refreshed.');
  await page.locator('#agent-settings > summary').click();
  expect(await page.locator('#token-details').getAttribute('open')).toBeNull();
  const composer=await page.locator('#note-form').boundingBox();
  expect(composer.y+composer.height).toBeLessThanOrEqual(1000);
  await expect(page.locator('.worker-card')).toHaveCount(initialWorkerCount + 2);
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-redesign-desktop.png'),fullPage:true});
  await page.getByLabel('Find agents',{exact:true}).fill('Ready for design review');
  await expect(page.locator('.worker-card')).toHaveCount(1);
  await page.getByLabel('Find agents',{exact:true}).clear();
  await expect(page.locator('.worker-card')).toHaveCount(initialWorkerCount + 2);
  for (const width of [390, 320]) {
    await page.setViewportSize({width,height:844});
    await expect(page.locator('#worker-terminal-output')).toBeVisible();
    const agentList = await page.locator('.agent-list').boundingBox();
    const inspector = await page.locator('.inspector').boundingBox();
    expect(inspector.y, `Inspector follows the agent list at ${width}px`).toBeGreaterThanOrEqual(agentList.y + agentList.height);
    expect(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth)).toBe(false);
    await page.screenshot({path:path.resolve(__dirname,`../../.agent-loop/web-redesign-mobile-${width}.png`),fullPage:true});
    await agentSettings(page);
    await expect(page.locator('#agent-effort')).toBeVisible();
    await page.screenshot({path:path.resolve(__dirname,`../../.agent-loop/web-redesign-settings-${width}.png`),fullPage:true});
    await page.locator('#agent-settings > summary').click();
  }
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({width,height:width === 1440 ? 1000 : 844});
    for (const [trigger, dialog] of [['new-fleet','create-dialog'], ['supervisor-settings','supervisor-dialog'], ['projects','projects-dialog']]) {
      if (trigger !== 'new-fleet') await workspaceSettings(page);
      await page.locator('#'+trigger).click();
      const modal = page.locator('#'+dialog);
      await expect(modal).toBeVisible();
      expect(await modal.evaluate(element=>element.scrollWidth > element.clientWidth)).toBe(false);
      await page.screenshot({path:path.resolve(__dirname,`../../.agent-loop/web-redesign-${dialog}-${width}.png`),fullPage:true});
      const action = modal.locator('.dialog-foot button').last();
      if (await action.count()) {
        await action.scrollIntoViewIfNeeded();
        const box = await action.boundingBox();
        expect(box.y).toBeGreaterThanOrEqual(0);
        expect(box.y + box.height).toBeLessThanOrEqual(page.viewportSize().height);
        await page.screenshot({path:path.resolve(__dirname,`../../.agent-loop/web-redesign-${dialog}-actions-${width}.png`)});
      }
      await modal.locator('[data-close]').click();
      await expect(modal).toBeHidden();
    }
  }
});


test('Add agents clearly loads the Codex model list before selection',async ({page})=>{
  let release;
  const gate=new Promise(resolve=>{release=resolve;});
  await page.route('**/api/models',async route=>{await gate;await route.continue();});
  await page.goto('/',{waitUntil:'domcontentloaded'});
  await page.locator('#new-fleet').click();
  const picker=page.locator('#task-fields [data-model-picker]').first();
  await expect(picker).toBeDisabled();
  await expect(page.locator('#task-fields .model-picker-status').first()).toContainText('Loading Codex models');
  release();
  await expect(picker).toBeEnabled();
  await expect(picker.locator('option[value="fixture-model"]')).toBeAttached();
  const count=await picker.locator('option').count()-2;
  await expect(page.locator('#task-fields .model-picker-status').first()).toContainText(`${count} model choice`);
  await picker.selectOption('fixture-model');
  await expect(page.locator('#task-fields [data-field=model]')).toHaveValue('fixture-model');
  await page.locator('#create-dialog [data-close]').click();
});


test('idle agent terminal connects while its status refresh is delayed', async ({page}) => {
  await page.goto('/');
  await page.waitForFunction(() => typeof currentTask === 'function' && currentTask());
  await page.getByRole('tab',{name:'Agent terminal',exact:true}).click();
  let release, refreshStarted;
  const gate = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { refreshStarted = resolve; });
  await page.route('**/api/supervisor', async route => {
    refreshStarted();
    await gate;
    await route.continue();
  });
  try {
    await page.locator('#open-native').click();
    await started;
    await expect(page.locator('#native-status')).toContainText('Connected',{timeout:10000});
    await page.locator('#native-terminal .xterm-helper-textarea').focus();
    await page.keyboard.type('before refresh');
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: before refresh'));
  } finally {
    release();
    await page.unrouteAll({behavior:'wait'});
  }
  await page.locator('#return-native').click();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await page.locator('#stop').click();
});

test('active worker yields its terminal only after the current turn', async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('WATCH_WORKER_OUTPUT: set value to 2.');
  await page.getByRole('button',{name:'Add & start supervisor'}).click();
  await page.getByRole('tab',{name:'Conversation',exact:true}).click();
  await expect(page.locator('#worker-terminal-output')).toContainText('Worker is running; live output is available.',{timeout:20000});
  await expect(page.locator('#task-status')).toHaveText('working');
  await page.getByRole('tab',{name:'Agent terminal',exact:true}).click();
  const response=page.waitForResponse(response => response.url().endsWith('/native/request'));
  await page.locator('#open-native').click();
  expect((await (await response).json()).native_ready).toBe(false);
  await expect(page.locator('#native-status')).toContainText('Waiting for the current worker turn');
  expect(await page.evaluate(() => !!socket)).toBe(false);
  await expect(page.locator('#native-placeholder')).toBeVisible();
  await expect(page.locator('#native-placeholder h3')).toHaveText('Waiting for the current turn to finish…');
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await expect(page.locator('#native-placeholder')).toBeHidden();
  await page.waitForFunction(() => terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Starting agent CLI…'));
  await page.locator('#native-terminal .xterm-helper-textarea').focus();
  await page.keyboard.type('after handoff');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: after handoff'));
  await page.locator('#return-native').click();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await page.locator('#stop').click();
});

test('actual interactive terminal supports direct typing, reconnect, and other supervised workers',async ({page,request})=>{
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.waitForFunction(()=>typeof currentTask === 'function' && currentTask());
  const nativeAgent=await page.locator('#task-title').textContent();
  await page.getByRole('tab',{name:'Agent terminal',exact:true}).click();
  await page.locator('#open-native').click();
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.locator('#native-terminal .xterm-helper-textarea').focus();
  await page.keyboard.type('hello native');await page.keyboard.press('Enter');
  await expect.poll(()=>page.evaluate(()=>terminal.buffer.active.getLine(terminal.buffer.active.cursorY)?.translateToString() || '')).not.toBeNull();
  await page.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: hello native'));
  await page.reload();
  await expect(page.getByRole('tab',{name:'Agent terminal',exact:true})).toHaveAttribute('aria-selected','true');
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: hello native'));
  await page.locator('#resume').click();
  await expect(page.locator('#manager-state')).toContainText('running');
  await page.locator('#new-fleet').click();
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Set value to 2 while another agent is interactive.');
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#create-dialog')).toBeHidden();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await page.locator(`[data-task="${nativeAgent}"]`).click();
  await page.getByRole('tab',{name:'Agent terminal',exact:true}).click();
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: hello native'));
  await page.locator('#native-terminal .xterm-helper-textarea').focus();
  await page.keyboard.type('second message');await page.keyboard.press('Enter');
  await page.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: second message'));
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/native-codex-terminal.png'),fullPage:false});
  await page.locator('#return-native').click();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await page.locator('#stop').click();
});

test('custom CLI accepts direct input and resumes its explicitly saved chat',async({page,request})=>{
  const fixture=await(await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.waitForFunction(()=>typeof currentTask === 'function' && currentTask());
  await page.getByRole('tab',{name:'Agent terminal',exact:true}).click();
  await page.locator('.native-options summary').click();
  await page.locator('#native-program').selectOption('custom');
  await page.locator('#native-command').fill(fixture.nativeCommand);
  await page.locator('#native-resume').fill(fixture.nativeCommand+' resume {session_id}');
  await page.locator('#open-native').click();
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.locator('#native-terminal .xterm-helper-textarea').focus();
  await page.keyboard.type('custom input');await page.keyboard.press('Enter');
  await page.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: custom input'));
  await expect(page.locator('#open-native')).toHaveText('Show terminal');
  await page.locator('.native-options summary').click();
  await page.locator('#open-native').click();
  await expect(page.locator('.native-options')).not.toHaveAttribute('open','');
  await page.locator('.native-options summary').click();
  await page.locator('#native-session').fill('custom-browser-chat');
  if(await page.locator('.native-options').getAttribute('open') === null) await page.locator('.native-options summary').click();
  await page.locator('#save-native-session').click();
  await expect(page.locator('#native-status')).toContainText('chat saved for recovery');
  await page.locator('#native-terminal .xterm-helper-textarea').focus();
  await page.keyboard.type('/exit');await page.keyboard.press('Enter');
  await page.waitForFunction(()=>socket?.exited && socket.readyState === WebSocket.CLOSED);
  await expect(page.locator('#open-native')).toBeEnabled();
  await page.locator('#open-native').click();
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Resume=custom-browser-chat'));
  const other=await page.context().newPage();
  await other.goto(page.url());
  await expect(other.locator('#open-native')).toBeEnabled();
  await other.locator('#open-native').click();
  await expect(other.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.waitForFunction(()=>socket?.detached && socket.readyState === WebSocket.CLOSED);
  await other.locator('#native-terminal .xterm-helper-textarea').focus();
  await other.keyboard.type('after moving tabs');await other.keyboard.press('Enter');
  await other.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: after moving tabs'));
  await other.locator('#restart-native').click();
  await expect(other.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await other.waitForFunction(()=>terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Resume=custom-browser-chat'));
  await other.close();
  await page.locator('#open-native').click();
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.locator('#return-native').click();
  await page.locator('#stop').click();
});

test('goal names stay unique, update with edits, and respect custom IDs', async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await page.getByLabel('Goal',{exact:true}).fill('Simplify café navigation!');
  await expect(page.locator('.agent-name-preview')).toHaveText('Name: simplify-cafe-navigation');
  await page.locator('#add-task').click();
  await page.getByLabel('Goal',{exact:true}).nth(1).fill('Simplify café navigation!');
  await expect(page.locator('.agent-name-preview').nth(1)).toHaveText('Name: simplify-cafe-navigation-2');
  await optionalSettings(page);
  await page.getByLabel('Agent ID',{exact:true}).first().fill('custom-navigation');
  await expect(page.locator('.agent-name-preview').first()).toHaveText('Name: custom-navigation');
  await expect(page.locator('.agent-name-preview').nth(1)).toHaveText('Name: simplify-cafe-navigation');
  await page.getByLabel('Repository root',{exact:true}).first().fill(fixture.repo);
  await page.getByLabel('Repository root',{exact:true}).nth(1).fill(fixture.secondRepo);
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#create-dialog')).toBeHidden();
  await expect(page.locator('#task-title')).toHaveText('custom-navigation');
  await page.getByLabel('Find agents',{exact:true}).fill('café navigation');
  await expect(page.locator('.worker-card')).toHaveCount(2);
  await page.getByLabel('Filter agents by status').selectOption('complete');
  await expect(page.locator('.worker-card')).toHaveCount(0);
  await expect(page.locator('#workers')).toHaveText('No matching agents.');
  await page.getByLabel('Filter agents by status').selectOption('ready');
  await expect(page.locator('.worker-card')).toHaveCount(2);
  await page.locator('#new-fleet').click();
  await page.getByLabel('Goal',{exact:true}).fill('Simplify café navigation!');
  await expect(page.locator('.agent-name-preview')).toHaveText('Name: simplify-cafe-navigation-2');
  expect(await page.evaluate(() => agentName({id:'task-6',goal:'Simplify navigation'}))).toBe('Simplify navigation');
  const generated = await page.evaluate(() => {
    const used = new Set();
    return ['你好', '你好', 'a'.repeat(200)].map(goal => goalAgentId(goal, used));
  });
  expect(generated).toEqual(['goal-agent','goal-agent-2','a'.repeat(48)]);
});

test('merge and redeploy submits the selected worktree and exposes job progress',async({page})=>{
  let job=null,posted;
  await page.route('**/api/events',route=>route.abort());
  await page.route('**/api/supervisor',async route=>{
    const response=await route.fetch(),data=await response.json();
    const task=data.supervisor.tasks.find(t=>t.enabled !== false);
    data.supervisor.deployment={checkout:task.repo,branch:'main',url:'http://127.0.0.1:8776',job};
    await route.fulfill({response,json:data});
  });
  await page.route('**/api/deploy',async route=>{
    posted=route.request().postDataJSON();job={id:'0123456789abcdef',stage:'checking'};
    await route.fulfill({json:{id:job.id,url:'http://127.0.0.1:8776'}});
  });
  await page.goto('/');
  await page.waitForFunction(()=>typeof currentTask === 'function' && currentTask());
  const source=await page.evaluate(()=>currentTask().worktree);
  await agentSettings(page);
  await expect(page.locator('#deploy-agent')).toBeVisible();
  await page.locator('#deploy-agent').click();
  await expect(page.locator('#toast')).toContainText('Deployment 012345');
  expect(posted).toEqual({source});
  await expect(page.locator('#deploy-agent')).toBeDisabled();
  await expect(page.locator('#deploy-status')).toContainText('checking');
  job={...job,stage:'failed',error:'Browser tests failed: delete and clean up agents'};
  await page.reload();
  await page.waitForFunction(()=>typeof currentTask==='function' && currentTask());
  await agentSettings(page);
  await expect(page.locator('#deploy-agent')).toBeEnabled();
  await expect(page.locator('#deploy-status')).toContainText('delete and clean up agents');
  await expect(page.locator('#deploy-log')).toHaveAttribute('href','/api/deploy/jobs/0123456789abcdef/log');
});

test('theme switch persists and remains available on mobile', async ({page}) => {
  await page.goto('/');
  const toggle = page.locator('#theme-toggle');
  await workspaceSettings(page);
  await expect(toggle).toHaveAccessibleName('Switch to light mode');
  await toggle.click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await workspaceSettings(page);
  await expect(toggle).toHaveAccessibleName('Switch to dark mode');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
  await page.locator('#new-fleet').click();
  await expect(page.locator('#create-dialog')).toBeVisible();
  await page.locator('[data-close="create-dialog"]').click();
  await page.setViewportSize({width: 390, height: 844});
  await workspaceSettings(page);
  await expect(toggle).toBeVisible();
  await toggle.click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('Claude models can be selected when creating an agent', async ({page,request}) => {
  const fixture = await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.locator('#new-fleet').click();
  await page.locator('#task-fields [data-field=provider]').selectOption('claude');
  const picker = page.locator('#task-fields [data-model-picker]');
  await expect(picker.locator('option[value="sonnet"]')).toBeAttached();
  await picker.selectOption('sonnet');
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Create a Claude agent for this project.');
  await page.getByRole('button',{name:'Add & start supervisor'}).click();
  await expect(page.locator('#create-dialog')).toBeHidden();
  await expect.poll(async () => (await (await page.request.get('/api/supervisor')).json()).supervisor.tasks.find(t => t.goal === 'Create a Claude agent for this project.')?.provider).toBe('claude');
  const task = (await (await page.request.get('/api/supervisor')).json()).supervisor.tasks.find(t => t.goal === 'Create a Claude agent for this project.');
  expect(task.model).toBe('sonnet');
  await expect(page.locator('#task-status')).toHaveText('complete', {timeout:20000});
  await expect(page.locator('#worker-terminal-output')).toContainText('Updated app.py');
});

test('agent terminal shows startup feedback while the connection is delayed', async ({page}) => {
  await page.goto('/');
  await page.waitForFunction(() => typeof currentTask === 'function' && currentTask());
  await page.getByRole('tab',{name:'Agent terminal',exact:true}).click();
  let release; const gate = new Promise(resolve => { release = resolve; });
  await page.routeWebSocket(/\/terminal\?mode=agent/, async ws => {
    await gate;
    const server = ws.connectToServer();
    void server;
  });
  const visible = () => page.evaluate(() => Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n'));
  try {
    await page.locator('#open-native').click();
    await page.waitForFunction(() => window.terminal && socket && socket.readyState === WebSocket.CONNECTING);
    await expect(page.locator('#native-placeholder')).toBeHidden();
    await expect(page.locator('#native-terminal')).toBeVisible();
    await expect(page.locator('#native-terminal .xterm-rows')).toContainText('Starting agent CLI…');
    expect(await visible()).not.toContain('Codex interactive terminal');
    await expect(page.locator('#native-status')).not.toContainText('Connected');
  } finally { release(); }
  await expect(page.locator('#native-status')).toContainText('Connected',{timeout:20000});
  await page.locator('#native-terminal .xterm-helper-textarea').focus();
  await page.keyboard.type('after delay');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => terminal && Array.from({length:terminal.buffer.active.length},(_,i)=>terminal.buffer.active.getLine(i)?.translateToString()||'').join('\n').includes('Codex reply: after delay'));
  await page.locator('#return-native').click();
  await expect(page.locator('#task-status')).toHaveText('complete',{timeout:20000});
  await page.locator('#stop').click();
});
