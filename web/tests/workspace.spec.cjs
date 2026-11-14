const {test,expect} = require('@playwright/test');
const path = require('node:path');
async function optionalSettings(page) { if(await page.locator('#optional-settings').getAttribute('open') === null) await page.locator('#optional-settings > summary').click(); }

test('one supervisor: add an agent, use a real terminal, send direction, and complete',async ({page,request}) => {
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
  await page.getByRole('button',{name:'Add your first agent'}).click();
  await optionalSettings(page);
  await page.getByLabel('Agent ID',{exact:true}).fill('browser-fix');
  await page.getByLabel('Repository root',{exact:true}).fill(fixture.repo);
  await page.getByLabel('Goal',{exact:true}).fill('Set app.py value to 2.');
  await page.getByLabel('Verification command (optional)',{exact:true}).fill(fixture.acceptance);
  await page.getByRole('button',{name:'Add agents',exact:true}).click();
  await expect(page.locator('#title')).toHaveText('Your local supervisor.');
  await expect(page.locator('#stat-total')).toHaveText('1');
  await expect(page.locator('#task-status')).toHaveText('ready');
  await page.getByRole('tab',{name:'Terminal',exact:true}).click();
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
  await page.getByRole('tab',{name:'Activity',exact:true}).click();
  await expect(page.locator('#activity')).toContainText('Updated app.py');
  await expect(page.locator('#manager-state')).toContainText('running');
  await page.locator('#view-worker-terminal').click();
  await expect(page.locator('#worker-terminal-output')).toContainText('Updated app.py');
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
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-mobile.png'),fullPage:true});
});

test('scan repositories, choose one, persist subagent toggle, and remove/restore an agent',async ({page,request}) => {
  const fixture=await (await request.get('/test-fixture')).json();
  await page.goto('/');
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
  await expect(page.locator('#allow-subagents')).toBeChecked();
  await expect(page.locator('.worker-policy')).toContainText('All project files');
  await page.locator('#allow-subagents').uncheck();
  await expect(page.locator('#toast')).toContainText('Subagent setting saved');
  await page.reload();
  await page.locator('[data-task="selected-repo"]').click();
  await expect(page.locator('#allow-subagents')).not.toBeChecked();
  await page.locator('#remove-agent').click();
  await expect(page.locator('[data-restore="selected-repo"]')).toBeVisible({timeout:15000});
  await expect(page.locator('[data-task="selected-repo"]')).toHaveCount(0);
  await page.locator('[data-restore="selected-repo"]').click();
  await expect(page.locator('[data-task="selected-repo"]')).toBeVisible();
  await page.locator('#projects').click();
  await expect(page.locator('#saved-projects')).toContainText('another-project');
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-projects.png'),fullPage:true});
});

test('local keeper is optional and can be enabled or disabled without replacing the supervisor',async ({page}) => {
  await page.goto('/');
  await page.locator('#supervisor-settings').click();
  await expect(page.getByLabel('Enable optional local keeper')).not.toBeChecked();
  await page.getByLabel('Enable optional local keeper').check();
  await page.getByLabel('Local keeper model',{exact:true}).fill('installed-local-model');
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  await expect(page.locator('#keeper-status')).toContainText('installed-local-model');
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
  await page.locator('#supervisor-settings').click();
  await expect(page.locator('#supervisor-dialog [data-model-picker] option[value="fixture-model"]')).toBeAttached();
  await page.locator('#supervisor-dialog [data-close]').click();
  if (await page.locator('#stop').isEnabled()) await page.locator('#stop').click();
  await expect(page.locator('#save-agent-model')).toBeEnabled();
  await page.locator('.agent-model-settings [data-model-picker]').selectOption('fixture-model');
  await page.locator('#save-agent-model').click();
  await expect(page.locator('.worker-card.selected .model')).toContainText('fixture-model');
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
  await page.locator('#view-worker-terminal').click();
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
  await expect(page.locator('#agent-effort')).toHaveValue('xhigh');
  await page.locator('#agent-effort').selectOption('low');
  await page.locator('#save-agent-effort').click();
  const agentId=await page.locator('#task-title').textContent();
  await expect.poll(async()=> (await (await page.request.get('/api/supervisor')).json()).supervisor.tasks.find(t=>t.id===agentId)?.worker_effort).toBe('low');
  await page.locator('#supervisor-settings').click();
  await page.getByLabel('Supervisor effort',{exact:true}).selectOption('high');
  await page.getByLabel('Unlimited parallel workers',{exact:true}).check();
  await expect(page.locator('#supervisor-form [name=parallel]')).toBeDisabled();
  await page.getByRole('button',{name:'Save settings',exact:true}).click();
  const settings=(await (await page.request.get('/api/supervisor')).json()).supervisor;
  expect(settings.max_parallel).toBeNull();
  expect(settings.supervisor.effort).toBe('high');
  await page.locator('#supervisor-settings').click();
  await expect(page.getByLabel('Unlimited parallel workers',{exact:true})).toBeChecked();
  await page.locator('#supervisor-dialog [data-close]').click();
  await page.locator('#view-worker-terminal').click();
  await page.screenshot({path:path.resolve(__dirname,'../../.agent-loop/web-conversation-effort.png'),fullPage:true});
});
