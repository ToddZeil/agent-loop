try { document.documentElement.dataset.theme = localStorage.getItem('loop-theme') || 'dark'; } catch (_) { document.documentElement.dataset.theme = 'dark'; }
'use strict';
const $ = id => document.getElementById(id);
const loadedUIRevision = document.querySelector('meta[name=loop-ui-revision]')?.content || '';
$('ui-build').textContent = `UI ${loadedUIRevision || 'unknown'}`;
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let fleets = [], selectedFleet = localStorage.getItem('loop-fleet'), selectedTask = new URLSearchParams(location.search).get('agent') || localStorage.getItem('loop-task');
let events = null, snapshotEpoch = null, snapshotRevision = -1;
let activeTab = 'terminal', refreshing = false, taskNumber = 0;
let savedProjects = [], savedFolders = [];
let terminal = null, fit = null, socket = null, terminalKey = null, toastTimer;
let terminalMode = null, nativeAutoAttach = null, nativeReconnectAttempts = 0, nativeReturning = false, nativeTakeover = null;
function toast(message, error = false) {
  $('toast').textContent = message; $('toast').classList.toggle('error', error); $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, error ? 8000 : 4000);
}
async function api(path, body) {
  const options = {method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type':'application/json','X-Loop-Request':'1'},
    body: body === undefined ? undefined : JSON.stringify(body)};
  let response = await fetch('/api' + path, options);
  if(response.status === 401) {
    await fetch('/', {cache:'no-store'}); // Renew the local cookie after a server restart.
    response=await fetch('/api'+path,options);
  }
  const text = await response.text();
  let data; try { data = JSON.parse(text); } catch { data = {error: text}; }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
let modelCatalog = {}, modelCatalogPromise, modelsLoading = false, modelLoadError = '';
async function loadModels() {
  if(!modelCatalogPromise) {
    modelsLoading=true; modelLoadError='';
    modelCatalogPromise=api('/models').then(data=>{modelCatalog=data.providers;})
      .catch(error=>{modelLoadError=error.message;throw error;})
      .finally(()=>{
        modelsLoading=false;modelCatalogPromise=null;
        document.querySelectorAll('[data-model-picker]').forEach(select=>select._reload());
      });
    document.querySelectorAll('[data-model-picker]').forEach(select=>select._reload());
  }
  return modelCatalogPromise;
}
function effortPicker(input, select, value = select.value || 'medium') {
  input._effort = select;
  const provider = input._picker?._provider || 'codex';
  const model = modelCatalog[provider]?.models.find(m => m.id === input.value);
  const levels = model?.efforts?.length ? model.efforts : ['low','medium','high'];
  const chosen = levels.includes(value) ? value : levels.includes('medium') ? 'medium' : levels[0];
  select.innerHTML = levels.map(e => `<option value="${escapeHTML(e)}">${escapeHTML(e === 'xhigh' ? 'Extra high' : e[0].toUpperCase()+e.slice(1))}</option>`).join('');
  select.value = chosen;
}
function modelPicker(input, provider, value = input.value) {
  if (!input._picker) {
    input.type = 'hidden';
    const select = document.createElement('select'); select.dataset.modelPicker = '';
    const custom = document.createElement('input'); custom.placeholder = 'Exact model ID';
    custom.setAttribute('aria-label', 'Custom model ID');
    const controls=document.createElement('div'); controls.className='model-picker-controls';
    const refresh=document.createElement('button'); refresh.type='button'; refresh.className='model-refresh';
    refresh.textContent='↻'; refresh.title='Refresh model catalogue'; refresh.setAttribute('aria-label','Refresh models');
    refresh.onclick=async()=>{refresh.disabled=true;try{await loadModels();toast('Model catalogue refreshed.');}catch(e){toast(e.message,true);}finally{refresh.disabled=false;}};
    const status=document.createElement('small');status.className='model-picker-status';status.setAttribute('aria-live','polite');select._status=status;
    controls.append(select,refresh); input.after(controls,custom,status);
    input._picker = select; select._input = input; select._custom = custom;
    select.onchange = () => {
      custom.hidden = select.value !== '__custom';
      input.value = custom.hidden ? select.value : custom.value.trim();
      if(input._effort) effortPicker(input,input._effort);
    };
    custom.oninput = () => { input.value = custom.value.trim(); };
    select._reload = () => modelPicker(input, select._provider, input.value);
  }
  const select = input._picker, custom = select._custom;
  select._provider = provider;
  const models = modelCatalog[provider]?.models || [];
  const waiting=modelsLoading && !modelCatalog[provider];
  const providerLabel={codex:'Codex',claude:'Claude',antigravity:'Antigravity',muse:'Muse'}[provider] || 'Provider';
  select.setAttribute('aria-label',`${providerLabel} model`);
  select.disabled=waiting;
  select._status.textContent=waiting ? `Loading ${providerLabel} models…` : modelLoadError ? `Model refresh failed: ${modelLoadError}` :
    models.length ? `${models.length} model ${models.length===1 ? 'choice' : 'choices'} · open the dropdown to select` : 'No saved model list. Use provider default or enter a custom ID.';
  select.innerHTML = `<option value="">${waiting ? 'Loading models…' : models.length ? `Provider default · ${models.length} ${models.length===1?'model':'models'}` : 'Provider default'}</option>` + models.map(m =>
    `<option value="${escapeHTML(m.id)}">${escapeHTML(m.label)} · ${escapeHTML(m.id)}${m.custom ? ' (saved custom)' : m.previously_seen ? ' (previously seen)' : ''}</option>`
  ).join('') + '<option value="__custom">Custom model ID…</option>';
  const known = models.some(m => m.id === value);
  select.value = !value || known ? value : '__custom';
  custom.hidden = select.value !== '__custom'; custom.value = value;
  input.value = value;
  if(input._effort) effortPicker(input,input._effort);
}
async function saveModel(provider, model) {
  if (model && !modelCatalog[provider]?.models.some(m => m.id === model)) {
    modelCatalog = (await api('/models', {provider,model})).providers;
  }
}
function agentName(task) {
  return /^task-\d+$/.test(task.id) && task.goal
    ? task.goal.trim().split('\n')[0].slice(0, 72) : task.id;
}
function currentFleet() { return fleets.find(f => f.id === selectedFleet); }
function currentTask() { return currentFleet()?.tasks.find(t => t.id === selectedTask); }
function taskURL() { return `/fleets/${encodeURIComponent(selectedFleet)}/tasks/${encodeURIComponent(selectedTask)}`; }
function pill(state) { return `<span class="pill ${escapeHTML(state)}">${escapeHTML(state)}</span>`; }
function countdown(timestamp) {
  const seconds = Math.max(0, Math.ceil(timestamp - Date.now()/1000));
  const hours = Math.floor(seconds/3600), minutes = Math.floor(seconds%3600/60);
  return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${seconds%60}s` : `${seconds}s`;
}
function chooseTask(id) {
  if (id !== selectedTask) { disconnectTerminal(); $('worker-terminal-output').textContent = 'Loading worker output…'; }
  selectedTask = id;localStorage.setItem('loop-task',id);
  selectTab(['native','changes'].includes(localStorage.getItem('loop-tab')) ? localStorage.getItem('loop-tab') : 'terminal'); render(); loadLogs();
  if (matchMedia('(max-width:1120px)').matches) document.querySelector('.inspector').scrollIntoView({block:'start',behavior:'smooth'});
}
function chooseFleet(id) {
  disconnectTerminal(); selectedFleet = id; selectedTask = null;
  localStorage.setItem('loop-fleet', id); render(); loadLogs();
}
function render() {
  if (!currentFleet() && fleets.length) selectedFleet = fleets[0].id;
  const fleet = currentFleet(), allTasks = fleet?.tasks || [], tasks = allTasks.filter(t => t.enabled !== false || t.attempt || t.delete_requested);
  renderArchived(allTasks.filter(t => t.enabled === false && !t.attempt && !t.delete_requested));
  if (!tasks.some(t => t.id === selectedTask)) selectedTask = tasks[0]?.id || null;
  $('fleet-count').textContent = fleets.length;
  $('fleet-list').innerHTML = fleets.map(f => `<button class="fleet-link ${f.id === selectedFleet ? 'active' : ''}" data-fleet="${escapeHTML(f.id)}"><span>${escapeHTML(f.name)}</span><small>${f.tasks.length}</small></button>`).join('');
  $('fleet-list').querySelectorAll('button').forEach(b => b.onclick = () => chooseFleet(b.dataset.fleet));
  $('title').textContent = 'Agent workspace';
  $('fleet-label').textContent = 'SUPERVISOR';
  $('subtitle').textContent = fleet?.error || '';
  $('subtitle').title = fleet?.home || '';
  $('stat-total').textContent = tasks.length;
  $('stat-active').textContent = tasks.filter(t => t.state === 'working' || t.state === 'verifying').length;
  $('stat-waiting').textContent = tasks.filter(t => ['waiting','blocked'].includes(t.state)).length;
  $('stat-done').textContent = tasks.filter(t => t.state === 'complete').length;
  $('stat-waiting').closest('div').classList.toggle('has-items', Number($('stat-waiting').textContent) > 0);
  renderTokenMonitor(fleet);
  $('worker-count').textContent = String(tasks.length);
  document.body.classList.toggle('no-agents', tasks.length === 0);
  $('empty').hidden = tasks.length > 0; $('workspace').hidden = tasks.length === 0;
  $('fleet-actions').hidden = !fleet;
  if (fleet) {
    $('manager-state').textContent = fleet.running ? 'Manager running' : 'Manager stopped';
    $('manager-state').className = 'pill' + (fleet.running ? ' working' : '');
    $('stop').disabled = !fleet.running; $('resume').title = tasks.length ? '' : 'Add an agent first'; $('resume').disabled = !tasks.length || fleet.running || allTasks.some(t => t.terminal_open && t.terminal_mode === 'shell');
    $('keeper-status').textContent = fleet.keeper?.enabled ? `Optional keeper: ${fleet.keeper.model}` : 'Local keeper: off (deterministic recovery)';
    $('supervisor-label').textContent = `Supervisor: ${fleet.supervisor?.provider || 'unknown'} / ${fleet.supervisor?.model || 'default model'}`;
  }
  const query = $('worker-search').value.trim().toLowerCase();
  const status = $('worker-filter').value;
  const visibleTasks = tasks.filter(t => {
    const matchesQuery = !query || `${t.id} ${t.repo} ${t.goal}`.toLowerCase().includes(query);
    const matchesStatus = !status || (status === 'attention' ? ['waiting','blocked'].includes(t.state)
      : status === 'working' ? ['working','verifying'].includes(t.state) : t.state === status);
    return matchesQuery && matchesStatus;
  });
  const openAgentMenu = $('workers').querySelector('details[open]')?.dataset.agentMenu;
  $('workers').innerHTML = visibleTasks.map(t => {
    return `<article class="worker-card ${t.id === selectedTask ? 'selected' : ''}" data-task="${escapeHTML(t.id)}"><button class="worker-select" aria-label="Open agent ${escapeHTML(agentName(t))}"><div class="worker-top"><strong>${escapeHTML(agentName(t))}</strong>${pill(t.signal || t.state)}</div><span class="repo" title="${escapeHTML(t.repo)}">${escapeHTML(t.repo.split(/[\\/]/).filter(Boolean).pop())}</span><div class="goal">${escapeHTML(t.delete_requested ? (t.delete_error ? 'Cleanup failed: '+t.delete_error : 'Deletion queued') : t.state === 'waiting' && t.wait_reason ? t.wait_reason : t.goal || t.next_action)}</div></button><details class="agent-card-menu" data-agent-menu="${escapeHTML(t.id)}" ${openAgentMenu === t.id ? 'open' : ''}><summary aria-label="Options for ${escapeHTML(agentName(t))}">⋯</summary><div class="agent-card-actions"><button data-archive="${escapeHTML(t.id)}" ${t.delete_requested || t.terminal_requested ? 'disabled' : ''}>Archive agent</button><button class="danger" data-delete="${escapeHTML(t.id)}" ${t.delete_requested && !t.delete_error ? 'disabled' : ''}>${t.delete_error ? 'Retry cleanup' : 'Delete &amp; clean up'}</button></div></details></article>`;
  }).join('') || '<p class="list-empty">No matching agents.</p>';
  $('workers').querySelectorAll('[data-task]').forEach(card => {
    card.querySelector('.worker-select').onclick = () => chooseTask(card.dataset.task);
  });
  $('workers').querySelectorAll('[data-delete]').forEach(button => button.onclick = () => deleteAgent(button.dataset.delete));
  $('workers').querySelectorAll('[data-archive]').forEach(button => button.onclick = () => archiveAgent(button.dataset.archive));
  const task = currentTask();
  if (!task) return;
  if (!$('full-access').disabled) $('full-access').checked = task.full_access !== false;
  if (!$('allow-subagents').disabled) $('allow-subagents').checked = !!task.allow_subagents;
  const modelKey = `${selectedTask}/${task.provider}/${task.model || ''}`;
  if ($('agent-model').dataset.key !== modelKey) {
    $('agent-model').dataset.key = modelKey;
    modelPicker($('agent-model'), task.provider, task.model || '');
    loadModels().catch(() => {});
  }
  if ($('agent-effort').dataset.key !== modelKey + '/' + task.worker_effort) {
    $('agent-effort').dataset.key = modelKey + '/' + task.worker_effort;
    effortPicker($('agent-model'), $('agent-effort'), task.worker_effort || 'medium');
  }
  $('save-agent-model').disabled = !!currentFleet()?.running || !!task.attempt;
  $('worker-output-status').textContent = `${task.signal || task.state} · read-only · updates live`;
  $('task-title').textContent = agentName(task); $('task-title').title = task.id; $('task-status').className = `pill ${task.state}`; $('task-status').textContent = task.signal || task.state;
  const deployment=fleet.deployment;
  $('deploy-agent').hidden=!deployment || task.repo !== deployment.checkout;
  const deploymentBusy=deployment?.job && !['complete','failed'].includes(deployment.job.stage);
  $('deploy-agent').disabled=!!deploymentBusy;
  $('deploy-status').hidden=!deployment?.job || task.repo !== deployment.checkout;
  $('deploy-log').hidden=!deployment?.job || task.repo !== deployment.checkout;
  if(deployment?.job) $('deploy-log').href='/api/deploy/jobs/'+encodeURIComponent(deployment.job.id)+'/log';
  $('deploy-status').textContent=deployment?.job ? `Deploy ${deployment.job.id.slice(0,6)}: ${deployment.job.stage}${deployment.job.error ? ' · '+deployment.job.error : ''}` : '';
  $('delete-agent').disabled=!!task.delete_requested && !task.delete_error;
  $('delete-agent').textContent=task.delete_error ? 'Retry cleanup' : task.delete_requested ? 'Deletion queued…' : 'Delete & clean up';
  $('delete-agent').title='Close this agent’s terminal and delete it after its current turn finishes.';
  $('remove-agent').disabled = task.enabled === false || !!task.delete_requested || !!task.terminal_requested;
  $('task-meta').textContent = `Agent ID: ${task.id}\n${task.branch}\n${task.worktree}`;
  $('task-meta').style.whiteSpace = 'pre-wrap';
  renderAgentUsage(task);
  const nativeConnected=socket?.readyState === WebSocket.OPEN && terminalMode === 'agent';
  const nativeConfigKey=`${selectedFleet}/${selectedTask}/${JSON.stringify(task.native_config)}`;
  if($('native-program').dataset.key !== nativeConfigKey) {
    $('native-program').dataset.key=nativeConfigKey;
    $('native-program').value=task.native_provider === 'custom' ? 'custom' : 'agent';
    $('native-command').value=task.native_config?.command_text || '';
    $('native-resume').value=task.native_config?.resume_text || '';
    $('native-session').value=task.native_session_id || '';
    $('native-custom').hidden=$('native-program').value !== 'custom';
  }
  for(const id of ['native-program','native-command','native-resume']) $(id).disabled=!!task.terminal_requested;
  $('open-native').disabled=!!task.delete_requested || socket?.readyState === WebSocket.CONNECTING;
  $('restart-native').disabled=!task.terminal_requested || !!task.attempt || task.terminal_backend === 'tmux';
  $('open-native').textContent=nativeConnected ? 'Show terminal' : task.terminal_open && task.terminal_mode !== 'shell' ? 'Reconnect here' : task.terminal_requested && !task.native_ready ? 'Waiting for current turn…' : task.native_session_id ? 'Resume agent chat' : 'Open Agent terminal';
  $('return-native').disabled=!task.terminal_requested;
  const recovery=task.native_session_id ? 'chat saved for recovery' : ['codex','claude'].includes(task.native_provider) ? 'chat ID pending' : 'save the chat ID for restart recovery';
  $('native-status').textContent=task.terminal_recovery_required ? 'Terminal stopped · worktree remains reserved until recovery' : nativeConnected ? `Connected · ${task.native_provider} · ${task.terminal_backend === 'tmux' ? 'session survives server restart · ' : ''}${recovery} · direct keyboard input` : task.terminal_requested && !task.native_ready && !task.terminal_open ? 'Waiting for the current worker turn to finish…' : task.terminal_requested ? `Agent paused · ${recovery}` : 'Direct CLI access';
  if(task.terminal_recovery_required) $('open-native').disabled=true;
  const ph=$('native-placeholder');
  if(ph && !nativeConnected) {
    const waiting=task.terminal_requested && !task.native_ready && !task.terminal_open, starting=task.terminal_requested && !waiting && !socket;
    ph.querySelector('h3').textContent=task.terminal_recovery_required ? 'Worktree reserved' : waiting ? 'Waiting for the current turn to finish…' : starting ? 'Starting the CLI…' : 'Talk directly to your CLI.';
    ph.querySelector('p').textContent=task.terminal_recovery_required ? 'Use Return to supervisor after checking that terminal processes have exited.' : waiting ? 'The terminal opens automatically as soon as the worker reaches a turn boundary.' : starting ? 'Connecting to the interactive session.' : 'Opens after the current turn and resumes the saved session when available.';
  }
  if(!nativeReturning && (nativeAutoAttach === `${selectedFleet}/${selectedTask}` || (activeTab === 'native' && task.terminal_requested && !socket)) && (task.native_ready || (task.terminal_open && task.terminal_mode !== 'shell'))) {
    nativeAutoAttach=null; const takeover=nativeTakeover === `${selectedFleet}/${selectedTask}`;nativeTakeover=null;connectTerminal('agent',takeover).catch(e=>toast(e.message,true));
  }
  $('note').disabled = !!task.delete_requested;
  $('note-form').querySelector('button[type=submit]').disabled = !!task.delete_requested;
  $('open-terminal').disabled = !!task.delete_requested || fleet.running || !!task.attempt || ['working','verifying','commit_pending'].includes(task.state) || socket?.readyState === WebSocket.OPEN;
  $('open-terminal').textContent = task.terminal_open ? 'Reconnect shell' : 'Open terminal';
  $('close-terminal').disabled = !task.terminal_open && !terminalKey;
  const decisions = task.decisions || [];
  $('decisions-panel').innerHTML = decisions.length ? decisions.map(d => `<article class="decision"><small>${d.action === 'review' ? '✓ SUPERVISOR REVIEW' : d.action === 'continue' ? '↳ SUPERVISOR DIRECTION' : '◷ OPERATOR REQUIRED'} · ${new Date(d.at*1000).toLocaleTimeString()}</small><p>${escapeHTML(d.message)}</p></article>`).join('') : '<p class="muted">No decisions yet.</p>';
}
function tokenNumber(value) { return value == null ? '—' : Number(value).toLocaleString(); }
function usageTable(usage) {
  const rows = usage?.by_model || [];
  if (!rows.length) return '<p class="muted">No model-level token reports yet.</p>';
  return `<div class="usage-table-wrap"><table class="usage-table"><thead><tr><th>Provider / model</th><th>Input</th><th>Output</th><th>Cached</th><th>Total</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${escapeHTML(row.provider)}<small>${escapeHTML(row.historical_tokens && !row.reported_calls ? 'Earlier usage (model unavailable)' : row.model || 'Provider default')}</small></td><td>${tokenNumber(row.input_tokens)}</td><td>${tokenNumber(row.output_tokens)}</td><td>${tokenNumber(row.cached_tokens)}</td><td>${tokenNumber(row.total_tokens)}</td></tr>`).join('')}</tbody></table></div>`;
}
function renderTokenMonitor(supervisor) {
  const usage = supervisor?.usage;
  $('all-token-total').textContent = tokenNumber(usage?.total.total_tokens ?? (supervisor?.tasks.length ? null : 0));
  for (const [prefix, data] of [['agents',usage?.agents],['supervisor',usage?.supervisor]]) {
    for (const [suffix,key] of [['total','total_tokens'],['input','input_tokens'],['output','output_tokens'],['cached','cached_tokens']]) {
      $(prefix+'-token-'+suffix).textContent = tokenNumber(data?.[key] ?? (supervisor?.tasks.length ? null : 0));
    }
  }
  const missing = usage?.total.unreported_calls || 0;
  $('token-report-status').textContent = 'Reported usage only; updates when each CLI reports tokens, usually at turn end. Cached tokens are included in input.' +
    (missing ? ` ${missing} earlier or unreported ${missing === 1 ? 'call has' : 'calls have'} no detailed usage.` : '') +
    (usage?.total.historical_tokens ? ' Earlier agent totals are retained; detailed breakdowns start with monitoring.' : '');
  $('supervisor-usage-models').innerHTML = usageTable(usage?.supervisor);
}
function renderAgentUsage(task) {
  const usage = task.usage, agent = usage?.agent;
  const fields = [['Reported total','total_tokens','total'],['Input (including cache)','input_tokens','input'],['Output','output_tokens','output'],['Cache read','cached_tokens','cached']];
  let html = `<div class="agent-token-grid">${fields.map(([label,key,id])=>`<div><span class="stat-label">${label}</span><strong id="agent-usage-${id}">${tokenNumber(agent?.[key])}</strong></div>`).join('')}</div>`;
  if (usage) {
    html += `<div class="usage-meta"><span>${usage.active_turn ? '● Turn running · waiting for CLI usage reports' : 'Usage saved across turns and session resets'}</span><span>${agent.reported_calls} reported · ${agent.unreported_calls} without details</span></div>`;
    if (usage.worker_budget) {
      const used = usage.worker.total_tokens || 0, percentage = Math.min(100,used/usage.worker_budget*100);
      html += `<label class="usage-budget">Worker budget: ${tokenNumber(used)} / ${tokenNumber(usage.worker_budget)}<progress max="100" value="${percentage}"></progress></label>`;
    }
    html += `<p class="muted">Context estimate: ${tokenNumber(usage.context_tokens)}${usage.context_reset_at ? ' / '+tokenNumber(usage.context_reset_at)+' rotation threshold' : ''}. Context resets do not reset usage totals.</p>`;
    html += '<h3>Agent model usage</h3>' + usageTable(agent);
    html += `<p class="muted">Verification tokens: ${tokenNumber(usage.reviewer.total_tokens)} · Supervisor assistance: ${tokenNumber(usage.supervisor.total_tokens)} (counted in the shared supervisor total).</p>`;
    if (agent.partial) html += '<p class="muted">Some earlier usage or breakdowns are unavailable. Values shown are reported counts.</p>';
  }
  $('usage-panel').innerHTML = html;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await api('/supervisor'); applySupervisor(data.supervisor);
    $('connection').textContent = events?.readyState === EventSource.OPEN ? 'Live stream connected' : 'Connected to localhost';
    render(); await loadLogs(); $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  } catch (e) { $('connection').textContent = 'Connection interrupted'; $('updated').textContent = e.message; }
  finally { refreshing = false; }
}
function readableLogs(data) {
  const lines = [];
  for (const line of (data.output || '').split('\n')) {
    if (!line.trim()) continue;
    try {
      const e = JSON.parse(line), item = e.item || {};
      if (item.text) lines.push(item.text);
      else if (item.command) lines.push(`$ ${item.command}\n${item.aggregated_output || ''}`);
      else if (e.type === 'turn.completed') lines.push('✓ Worker turn completed');
      else if (e.type === 'thread.started') lines.push('↳ Session connected');
      else if (e.message) lines.push(typeof e.message === 'string' ? e.message : JSON.stringify(e.message));
      else if (e.result && typeof e.result === 'string') lines.push(e.result);
      else if (e.payload?.text) lines.push(e.payload.text);
    } catch { lines.push(line); }
  }
  if (data.stderr) lines.push(data.stderr);
  if (!lines.length) for (const line of (data.journal || '').split('\n')) {
    try { const e = JSON.parse(line); lines.push(`[${new Date(e.at*1000).toLocaleTimeString()}] ${e.kind}${e.reason ? ': '+e.reason : ''}${e.message ? ': '+e.message : ''}`); } catch {}
  }
  return lines.join('\n\n') || 'No output yet. Start the supervisor to launch agents.';
}
function conversationItems(output) {
  const items = [], byId = new Map();
  const add = (item,id) => {
    if (id && byId.has(id)) Object.assign(items[byId.get(id)],item);
    else { if(id) byId.set(id,items.length); items.push(item); }
  };
  for (const line of (output || '').split('\n')) {
    if(!line.trim()) continue;
    try {
      const e=JSON.parse(line), item=e.item || {};
      if(item.command) add({kind:'command',text:item.command,output:item.aggregated_output || '',status:item.status || (item.exit_code === 0 ? 'completed' : 'running')},item.id);
      else if(item.text) add({kind:'message',text:item.text},item.id);
      else if(e.type === 'assistant' && Array.isArray(e.message?.content)) {
        for(const block of e.message.content) {
          if(block.type === 'text') add({kind:'message',text:block.text});
          if(block.type === 'tool_use') add({kind:'command',text:block.input?.command || block.name,output:'',status:'running'},block.id);
        }
      } else if(e.type === 'user' && Array.isArray(e.message?.content)) {
        for(const block of e.message.content) if(block.type === 'tool_result') {
          const text=typeof block.content === 'string' ? block.content : JSON.stringify(block.content);
          const index=byId.get(block.tool_use_id);
          if(index !== undefined) Object.assign(items[index],{output:text,status:block.is_error ? 'failed' : 'completed'});
        }
      } else if(typeof e.result === 'string' && !items.some(i=>i.text===e.result)) add({kind:'message',text:e.result});
      else if(e.payload?.text) add({kind:'message',text:e.payload.text});
      else if(e.type === 'turn.completed') add({kind:'status',text:'Turn completed'});
      else if(e.type === 'error' || e.error) add({kind:'error',text:typeof e.message === 'string' ? e.message : JSON.stringify(e.error || e)});
      else if(typeof e.message === 'string') add({kind:'message',text:e.message});
    } catch { if(!/^\s*[\[{]/.test(line)) add({kind:'message',text:line}); /* truncated JSON stays in the Run log */ }
  }
  return items;
}
function formatMessage(text) {
  return String(text).split(/(```[\s\S]*?(?:```|$))/g).map(part => {
    if(part.startsWith('```')) return `<pre class="message-code">${escapeHTML(part.replace(/^```[^\n]*\n?/,'').replace(/```$/,''))}</pre>`;
    return escapeHTML(part).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/^- \[x\] /gm,'✓ ').replace(/^- \[ \] /gm,'○ ');
  }).join('');
}
function renderConversation(data) {
  const panel=$('worker-terminal-output');
  const opened=new Set([...panel.querySelectorAll('details[open]')].map(el=>el.dataset.key));
  const message=(role,text,status='')=>`<article class="conversation-entry ${role === 'You' ? 'user-message' : ''}"><div class="conversation-role">${escapeHTML(role)}${status ? `<span>${escapeHTML(status)}</span>` : ''}</div><div class="conversation-text">${formatMessage(text)}</div></article>`;
  const entries=(data.notes || []).map(note=>({at:note.at,html:message('You',note.text,note.delivery)}));
  for(const turn of data.turns || [{id:'latest',at:0,output:data.output}]) {
    let html='';
    for(const [index,item] of conversationItems(turn.output).entries()) {
      const key=turn.id+'/'+index;
      if(item.kind === 'command') html+=`<details class="command-card" data-key="${escapeHTML(key)}" ${opened.has(key)?'open':''}><summary><span class="command-icon">⌘</span><code>${escapeHTML(item.text)}</code><span class="command-status">${escapeHTML(item.status)}</span></summary><pre>${escapeHTML(item.output || 'Waiting for command output…')}</pre></details>`;
      else if(item.kind === 'status') html+=`<div class="conversation-turn-status">✓ ${escapeHTML(item.text)}</div>`;
      else html+=message(item.kind === 'error' ? 'Error' : 'Worker',item.text);
    }
    if(turn.stderr && !/^Reading additional input from stdin\.\.\.\s*$/.test(turn.stderr)) html+=`<details class="command-card"><summary>Provider diagnostics</summary><pre>${escapeHTML(turn.stderr)}</pre></details>`;
    entries.push({at:turn.at,html:`<section class="conversation-turn">${html}</section>`});
  }
  entries.sort((a,b)=>a.at-b.at);
  const html=message('You',currentTask()?.goal || '')+entries.map(e=>e.html).join('');
  if(panel.dataset.content !== html) { panel.innerHTML=html; panel.dataset.content=html; }
}
let changesKey = '', changesPath = '', changesLoading = false, changesGeneration = 0;
async function loadChanges() {
  if (!selectedTask || !selectedFleet || activeTab !== 'changes') return;
  const key = taskURL();
  if (changesKey !== key) {
    changesKey = key; changesPath = ''; changesLoading = false; changesGeneration++;
    $('changes-files').textContent = 'Loading changes…';
    $('changes-diff').replaceChildren(); $('changes-path').textContent = 'Select a file to inspect its diff.';
    $('changes-warning').hidden = true;
  }
  if (changesLoading) return;
  changesLoading = true;
  const generation = changesGeneration;
  try {
    const data = await api(key + '/changes');
    if (key !== taskURL() || generation !== changesGeneration) return;
    const files = data.files;
    $('changes-summary').textContent = `${files.length} changed file${files.length === 1 ? '' : 's'} · since agent started (${data.base.slice(0,8)}) · includes uncommitted edits`;
    const list = $('changes-files');
    const html = files.map(f => `<button class="change-file ${f.path === changesPath ? 'selected' : ''}" data-path="${escapeHTML(f.path)}"><span>${escapeHTML(f.path)}</span><small>${f.status === 'New' ? 'New file' : f.added === null ? 'Binary' : `<span class="diff-added">+${f.added}</span> <span class="diff-removed">−${f.removed}</span>`}</small></button>`).join('') || '<p class="muted">No file changes yet.</p>';
    if (list.innerHTML !== html) list.innerHTML = html;
    list.querySelectorAll('[data-path]').forEach(b => b.onclick = () => {
      changesPath = b.dataset.path; changesGeneration++; changesLoading = false;
      $('changes-path').textContent = changesPath; $('changes-diff').textContent = 'Loading diff…';
      $('changes-warning').hidden = true; loadChanges();
    });
    if (!files.some(f => f.path === changesPath)) changesPath = files[0]?.path || '';
    if (!changesPath) {
      $('changes-path').textContent = 'No file changes yet.'; $('changes-diff').replaceChildren(); $('changes-warning').hidden = true; return;
    }
    list.querySelectorAll('[data-path]').forEach(b => b.classList.toggle('selected', b.dataset.path === changesPath));
    const path = changesPath;
    const result = await api(key + '/changes?path=' + encodeURIComponent(path));
    if (key !== taskURL() || generation !== changesGeneration || path !== changesPath) return;
    $('changes-path').textContent = path;
    $('changes-warning').hidden = !result.truncated;
    $('changes-warning').textContent = 'Large diff: showing the first 256 KB.';
    const htmlDiff = result.diff.split('\n').map(line => `<span class="${line.startsWith('+') ? 'diff-added' : line.startsWith('-') ? 'diff-removed' : line.startsWith('@@') ? 'diff-hunk' : ''}">${escapeHTML(line)}</span>`).join('\n');
    if ($('changes-diff').innerHTML !== htmlDiff) $('changes-diff').innerHTML = htmlDiff;
  } catch(e) {
    if (key === taskURL() && generation === changesGeneration) {
      $('changes-warning').hidden = false; $('changes-warning').textContent = e.message;
    }
  } finally { if (generation === changesGeneration) changesLoading = false; }
}
$('refresh-changes').onclick = () => loadChanges();
async function loadLogs() {
  if (activeTab === 'changes') return loadChanges();
  if (!selectedFleet || !selectedTask || !['activity','terminal'].includes(activeTab)) return;
  const key = `${selectedFleet}/${selectedTask}`;
  try {
    const data = await api(taskURL() + '/logs');
    if (key !== `${selectedFleet}/${selectedTask}`) return;
    const panel = $(activeTab === 'terminal' ? 'worker-terminal-output' : 'activity'), atBottom = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 30;
    if(activeTab === 'terminal') renderConversation(data);
    else { const text=readableLogs(data); if(panel.textContent !== text) panel.textContent=text; }
    if(atBottom) panel.scrollTop=panel.scrollHeight;
  } catch (e) { if (key === `${selectedFleet}/${selectedTask}`) $(activeTab === 'terminal' ? 'worker-terminal-output' : 'activity').textContent = e.message; }
}
async function fleetAction(action) {
  const button = $(action); button.disabled = true;
  try { await api(`/supervisor/${action}`, {}); toast(action === 'stop' ? 'Stop requested. Active workers finish their current turn.' : 'Supervisor started. Agents can join while it runs.'); await refresh(); }
  catch(e) { toast(e.message, true); render(); }
}
$('worker-search').oninput = () => render();
$('worker-filter').onchange = () => { syncStatTiles(); render(); };
// Summary tiles double as one-click filters for the agent list.
function syncStatTiles() {
  document.querySelectorAll('.stats>div[data-filter]').forEach(tile => {
    const on = $('worker-filter').value === tile.dataset.filter && tile.dataset.filter !== '';
    tile.classList.toggle('is-active', on); tile.setAttribute('aria-pressed', String(on));
  });
}
document.querySelectorAll('.stats>div').forEach((tile, i) => {
  const filter = ['', 'working', 'attention', 'complete'][i] || '';
  tile.dataset.filter = filter;
  if (!filter) return;
  tile.tabIndex = 0; tile.setAttribute('role', 'button'); tile.title = 'Show only these agents · click again to clear';
  const toggle = () => { $('worker-filter').value = $('worker-filter').value === filter ? '' : filter; syncStatTiles(); render(); };
  tile.addEventListener('click', toggle);
  tile.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
});
$('resume').onclick = () => fleetAction('resume'); $('stop').onclick = () => fleetAction('stop');
$('deploy-agent').onclick=async()=>{
  const button=$('deploy-agent');button.disabled=true;
  try {
    const result=await api('/deploy',{source:currentTask().worktree});
    toast(`Deployment ${result.id.slice(0,6)} queued. Workers will reopen in their saved chats.`);await refresh();
  } catch(e){toast(e.message,true);}finally{render();}
};
$('full-access').onchange = async event => {
  const checkbox=event.currentTarget,value=checkbox.checked;checkbox.disabled=true;
  try {await api(taskURL()+'/settings',{full_access:value});toast('Permissions saved. Applies next worker turn or after reopening its terminal.');await refresh();}
  catch(e){checkbox.checked=!value;toast(e.message,true);}
  finally{checkbox.disabled=false;}
};
$('allow-subagents').onchange = async event => {
  const checkbox = event.currentTarget, value = checkbox.checked;
  checkbox.disabled = true;
  try { await api(taskURL()+'/settings',{allow_subagents:value}); toast('Subagent setting saved for the next worker turn.'); await refresh(); }
  catch(e) { checkbox.checked = !value; toast(e.message,true); }
  finally { checkbox.disabled = false; }
};
$('save-agent-model').onclick = async () => {
  try {
    await api(taskURL()+'/settings', {model:$('agent-model').value});
    await loadModels(); await refresh(); toast('Model saved. Start the supervisor to retry.');
  } catch(e) { toast(e.message,true); }
};
$('save-agent-effort').onclick = async () => {
  try { await api(taskURL()+'/settings',{worker_effort:$('agent-effort').value}); await refresh(); toast('Effort saved for the next worker turn.'); }
  catch(e) { toast(e.message,true); }
};
$('note').onkeydown = event => { if(event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); if(!$('note-form').querySelector('button').disabled) $('note-form').requestSubmit($('note-form').querySelector('button')); } };
$('note-form').onsubmit = async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true;
  try {
    const result = await api(taskURL() + '/note', {text:$('note').value}); $('note').value = '';
    toast(result.start_error ? `Direction saved, but the supervisor could not start: ${result.start_error}` :
      result.started ? 'Direction saved. Supervisor started for the next worker turn.' : 'Direction saved for the next worker turn.', !!result.start_error);
    await refresh();
  }
  catch(e) { toast(e.message, true); } finally { button.disabled = false; }
};
function selectTab(tab) {
  activeTab = tab;localStorage.setItem('loop-tab',tab);
  if(selectedTask) {localStorage.setItem('loop-task',selectedTask);const url=new URL(location.href);url.searchParams.set('agent',selectedTask);url.searchParams.set('tab',tab);history.replaceState(null,'',url.href);}
  document.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === tab); b.setAttribute('aria-selected', String(b.dataset.tab === tab)); b.tabIndex = b.dataset.tab === tab ? 0 : -1; });
  for (const name of ['native','activity','terminal','changes','usage','decisions']) $(name+'-panel').hidden = name !== tab;
  if (['activity','terminal','changes'].includes(tab)) loadLogs();
  $('note-form').hidden = tab === 'native';
  if (['native','terminal'].includes(tab)) requestAnimationFrame(() => fit?.fit());
  if(tab === 'native' && currentTask()) render();
}
document.querySelectorAll('[data-tab]').forEach(b => {
  b.onclick = () => selectTab(b.dataset.tab);
  b.onkeydown = event => {
    const tabs = [...document.querySelectorAll('[data-tab]')], index = tabs.indexOf(b);
    const next = {ArrowRight: (index + 1) % tabs.length, ArrowLeft: (index + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1}[event.key];
    if (next === undefined) return;
    event.preventDefault(); tabs[next].focus(); selectTab(tabs[next].dataset.tab);
  };
});
function disconnectTerminal() {
  if (socket) { socket.onopen = null; socket.onclose = null; socket.onmessage = null; socket.onerror = null; socket.close(); socket = null; }
  terminal?.dispose(); terminal = null; fit = null; terminalKey = null; terminalMode=null;
  $('native-terminal').hidden=true; $('native-terminal').replaceChildren(); $('native-placeholder').hidden=false;
  $('terminal').hidden = true; $('terminal').replaceChildren(); $('terminal-state').textContent = 'Local worktree shell';
}
async function connectTerminal(mode='shell',takeover=false) {
  if (socket?.readyState === WebSocket.OPEN && !takeover) return;
  disconnectTerminal();
  if (!window.Terminal || !window.FitAddon) { toast('Terminal assets are missing. Run scripts/setup-supervise-web.', true); return; }
  terminalMode=mode;
  const mount=mode === 'agent' ? $('native-terminal') : $('terminal'); mount.hidden=false;
  if(mode === 'agent') $('native-placeholder').hidden=true;
  terminal = new Terminal({cursorBlink:true,fontFamily:'Menlo, ui-monospace, monospace',fontSize:12,scrollback:3000,theme:{background:'#0b0e0c',foreground:'#d2e0ce',cursor:'#c5f47b',selectionBackground:'#5b784755'}});
  fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open(mount); fit.fit();
  terminalKey = `${selectedFleet}/${selectedTask}`;
  if(mode === 'agent') terminal.writeln('Starting agent CLI…');
  socket = new WebSocket(`ws://${location.host}/api${taskURL()}/terminal?mode=${mode}${takeover ? '&takeover=1' : ''}`); socket.binaryType = 'arraybuffer';
  const ws = socket;
  ws.onopen = () => { nativeReconnectAttempts=0; $('terminal-state').textContent = mode === 'agent' ? '● Connected · agent' : '● Connected · local shell'; ws.send(JSON.stringify({type:'resize',cols:terminal.cols,rows:terminal.rows})); terminal.focus(); refresh(); };
  ws.onmessage = event => {
    if (typeof event.data !== 'string') terminal?.write(new Uint8Array(event.data));
    else { try { const e = JSON.parse(event.data); if(e.type === 'detached') {ws.detached=true;toast(e.message);terminal?.writeln('\r\n['+e.message+']');} if (e.type === 'exit') { ws.exited=true; terminal?.writeln(`\r\n[${mode === 'agent' ? 'Agent' : 'Shell'} exited: ${e.code}]`); refresh(); } } catch {} }
  };
  ws.onerror = () => toast(mode === 'agent' ? 'Cannot attach to the agent yet. Wait for the worker turn to finish, and close any other tab attached to this terminal.' : 'Cannot attach. Stop the manager, reconcile workers, and close any other terminal tab first.', true);
  ws.onclose = () => {
    $('terminal-state').textContent = 'Disconnected · terminal may still be running';
    if(mode === 'agent' && !ws.exited && !ws.detached && socket === ws && nativeReconnectAttempts < 3) {nativeReconnectAttempts++;socket=null;}
    refresh();
  };
  terminal.onData(data => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'input',data})); });
  terminal.onResize(({cols,rows}) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'resize',cols,rows})); });
}
$('open-terminal').onclick=()=>connectTerminal('shell');
function nativeBody() {
  const body={program:$('native-program').value};
  if(body.program === 'custom') {body.command=$('native-command').value;body.resume_command=$('native-resume').value;}
  if($('native-session').value.trim()) body.session_id=$('native-session').value.trim();
  return body;
}
$('native-program').onchange=()=>{ $('native-custom').hidden=$('native-program').value !== 'custom'; $('native-session').value=''; };
$('save-native-session').onclick=async()=>{
  try {
    if(!$('native-session').value.trim()) throw new Error('Enter the exact chat ID shown by your CLI.');
    await api(taskURL()+'/native/session',nativeBody());await refresh();toast('Chat ID saved for recovery.');
  } catch(e) {toast(e.message,true);}
};
$('open-native').onclick=async()=>{
  if(socket?.readyState === WebSocket.OPEN && terminalMode === 'agent') {
    document.querySelector('.native-options').open=false;
    requestAnimationFrame(()=>{fit?.fit();terminal?.focus();$('native-terminal').scrollIntoView({block:'nearest'});});
    return;
  }
  try {
    nativeReconnectAttempts=0;nativeReturning=true;selectTab('native');
    const key=`${selectedFleet}/${selectedTask}`;
    const result=await api(taskURL()+'/native/request',currentTask()?.terminal_requested ? {} : nativeBody());
    if(key !== `${selectedFleet}/${selectedTask}`) return;
    document.querySelector('.native-options').open=false;
    nativeTakeover=key;nativeReturning=false;nativeAutoAttach=key;
    if(result.native_ready) { nativeAutoAttach=null; nativeTakeover=null; await connectTerminal('agent',true); }
    await refresh();
  } catch(e) { nativeAutoAttach=null; toast(e.message,true); } finally {nativeReturning=false;}
};
$('restart-native').onclick=async()=>{
  nativeReturning=true;nativeAutoAttach=null;
  const key=`${selectedFleet}/${selectedTask}`,path=taskURL();
  try {
    disconnectTerminal();render();await api(path+'/terminal/close',{});await api(path+'/native/request',{});
    if(key === `${selectedFleet}/${selectedTask}`){nativeReturning=false;nativeReconnectAttempts=0;nativeTakeover=`${selectedFleet}/${selectedTask}`;nativeAutoAttach=key;await refresh();}
    toast('CLI restarted using its saved chat when available.');
  } catch(e){toast(e.message,true);} finally{nativeReturning=false;}
};
$('return-native').onclick=async()=>{
  nativeReturning=true;
  try {
    let result=await api(taskURL()+'/native/release',{});
    if(result.needs_verification) {
      nativeAutoAttach=null;disconnectTerminal();
      await refresh();
      if(!confirm(`The terminal was stopped, but detached processes may still be running in ${result.worktree}. Inspect them and confirm that no terminal process can write to this worktree before returning control to the supervisor. Have you verified this?`)) {
        toast('Worktree remains reserved until you verify that terminal processes have exited.',true);
        return;
      }
      result=await api(taskURL()+'/native/release',{verified_no_writers:true});
    }
    nativeAutoAttach=null;disconnectTerminal();await refresh();selectTab('terminal');
    toast(result.start_error ? `Terminal closed; automatic work is waiting: ${result.start_error}` : 'Interactive terminal closed. Agent returned to the supervisor.', !!result.start_error);
  } catch(e) { toast(e.message,true); } finally {nativeReturning=false;}
};
$('close-terminal').onclick = async () => {
  try { await api(taskURL()+'/terminal/close',{}); disconnectTerminal(); toast('Local shell closed. Workers can resume.'); await refresh(); }
  catch(e) { toast(e.message,true); }
};
const terminalResizeObserver = new ResizeObserver(() => { if (['native','terminal'].includes(activeTab)) { try { fit?.fit(); } catch {} } });
terminalResizeObserver.observe($('terminal'));
terminalResizeObserver.observe($('native-terminal'));
window.addEventListener('beforeunload', () => socket?.close());
function agentField(row,field) { return row.querySelector(`[data-field="${field}"]`) || row._options?.querySelector(`[data-field="${field}"]`); }
function goalAgentId(goal, used) {
  const base = goal.normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase()
    .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48).replace(/-+$/g, '') || 'goal-agent';
  let id = base, suffix = 2;
  while (used.has(id)) id = `${base}-${suffix++}`;
  used.add(id);
  return id;
}
function updateAgentNames() {
  const rows = [...$('task-fields').children];
  const used = new Set((currentFleet()?.tasks || []).map(t => t.id));
  rows.forEach(row => { const id = agentField(row, 'id').value.trim(); if (id) used.add(id); });
  rows.forEach(row => {
    const goal = agentField(row, 'goal').value.trim();
    row.dataset.defaultId = agentField(row, 'id').value.trim() || goalAgentId(goal, used);
    agentField(row, 'id').placeholder = row.dataset.defaultId;
    row.querySelector('.agent-name-preview').textContent = goal ? `Name: ${agentField(row, 'id').value.trim() || row.dataset.defaultId}` : 'Name is generated from your goal.';
  });
}
function addTask() {
  taskNumber++;
  const row = document.createElement('section'); row.className = 'task-fields';
  row.innerHTML = `<div class="task-field-head"><span>AGENT ${String(taskNumber).padStart(2,'0')}</span><button type="button" class="icon-button remove-task" aria-label="Remove agent form">×</button></div><label>Saved project<select data-field="project" aria-label="Saved project"><option value="">Choose a saved repository, or enter a path…</option></select></label><label data-manual-repo>Repository root<input data-field="repo" placeholder="/Users/you/projects/app" required></label><label>Goal<textarea data-field="goal" rows="3" placeholder="What would you like this agent to do?" required></textarea></label><div class="form-row"><label>Worker provider<select data-field="provider"><option>codex</option><option>claude</option><option>antigravity</option><option>muse</option></select></label><label>Model<input data-field="model" placeholder="Provider default"></label></div><label>Worker effort<select data-field="effort" aria-label="Worker effort"><option>medium</option></select></label><label class="subagent-option"><input type="checkbox" data-field="full-access" checked> Full access · no permission prompts</label><label class="subagent-option"><input type="checkbox" data-field="allow-subagents"> Allow subagents</label><small class="muted agent-name-preview" aria-live="polite">Name is generated from your goal.</small>`;
  const options = document.createElement('section'); options.className = 'task-options'; row._options = options;
  options.innerHTML = `<h4>Agent ${String(taskNumber).padStart(2,'0')}</h4><label>Agent ID<input data-field="id" pattern="[a-z0-9](?:[a-z0-9]|-){0,60}" placeholder="Generated automatically"></label><label>Verification command (optional)<input data-field="check" placeholder="For example: npm test"></label><small class="muted">Leave empty to use supervisor review.</small><div class="form-row"><label>Fallback provider<select data-field="fallback"><option value="">Wait for quota reset</option><option>codex</option><option>claude</option><option>antigravity</option><option>muse</option></select></label><label>Fallback model<input data-field="fallback-model" placeholder="Provider default"></label></div>`;
  row.querySelector('.remove-task').onclick = () => { if ($('task-fields').children.length > 1) { options.remove(); row.remove(); updateAgentNames(); } else toast('Add at least one agent.', true); };
  row.querySelectorAll('.subagent-option').forEach(label => options.append(label));
  $('task-fields').append(row); $('task-options').append(options);
  row.querySelector('[data-field=project]').onchange = event => {
    row.querySelector('[data-field=repo]').value = event.target.value;
    row.querySelector('[data-manual-repo]').hidden = !!event.target.value;
  };
  row.querySelector('[data-field=repo]').oninput = () => { row.querySelector('[data-field=project]').value = row.querySelector('[data-field=repo]').value; };
  const provider = agentField(row, 'provider');
  const fallback = agentField(row, 'fallback');
  modelPicker(agentField(row, 'model'), provider.value);
  effortPicker(agentField(row, 'model'),agentField(row, 'effort'));
  modelPicker(agentField(row, 'fallback-model'), fallback.value || 'codex');
  provider.onchange = () => modelPicker(agentField(row, 'model'), provider.value, '');
  fallback.onchange = () => modelPicker(agentField(row, 'fallback-model'), fallback.value || 'codex', '');
  agentField(row, 'goal').oninput = updateAgentNames;
  agentField(row, 'id').oninput = updateAgentNames;
  updateProjectPickers();
  updateAgentNames();
}
function openCreate() { loadModels().catch(e => toast(e.message,true)); loadProjects().catch(e => toast(e.message,true)); if (!$('task-fields').children.length) addTask(); $('create-error').textContent = ''; $('create-dialog').showModal(); }
$('new-fleet').onclick = openCreate; $('empty-create').onclick = openCreate; $('add-task').onclick = addTask;
$('import-fleet').onclick = () => { $('import-error').textContent=''; $('import-dialog').showModal(); };
document.querySelectorAll('[data-close]').forEach(b => b.onclick = () => $(b.dataset.close).close());
$('create-form').onsubmit = async event => {
  event.preventDefault(); const form = event.currentTarget, button = event.submitter;
  if (!new FormData(form).get('manifest').trim() && !form.checkValidity()) {
    if (form.querySelector(':invalid')?.closest('#optional-settings')) $('optional-settings').open = true;
    form.reportValidity(); return;
  }
  button.disabled = true; $('create-error').textContent = '';
  try {
    const fields = new FormData(form);
    let tasks;
    if (fields.get('manifest').trim()) tasks = JSON.parse(fields.get('manifest')).tasks;
    else {
      updateAgentNames();
      tasks = [...$('task-fields').children].map(row => {
        const get = field => agentField(row,field).value.trim();
        const provider = get('provider'), fallback = get('fallback'), workers = [provider], worker_models = {};
        if (get('model')) worker_models[provider] = get('model');
        if (fallback && fallback !== provider) { workers.push(fallback); if (get('fallback-model')) worker_models[fallback] = get('fallback-model'); }
        return {id:get('id') || row.dataset.defaultId,repo:get('repo'),goal:get('goal'),acceptance:get('check') ? [get('check')] : [],options:{workers,worker_models,worker_effort:get('effort'),full_access:agentField(row,'full-access').checked,allow_subagents:agentField(row,'allow-subagents').checked}};
      });
    }
    for (const task of tasks) for (const [provider, model] of Object.entries(task.options?.worker_models || {})) await saveModel(provider, model);
    const result = await api('/supervisor/agents',{tasks,start:button.value === 'start'});
    $('create-dialog').close(); await loadProjects(); await refresh(); chooseTask(result.added[0]);
    $('task-fields').replaceChildren(); $('task-options').replaceChildren(); form.reset(); $('optional-settings').open = false;
    toast(result.start_error ? `Agents added. ${result.start_error}` : 'Agents added to your supervisor.', !!result.start_error);
  } catch(e) { $('create-error').textContent = e.message; } finally { button.disabled = false; }
};
$('import-form').onsubmit = async event => {
  event.preventDefault(); const fields = new FormData(event.currentTarget), button = event.submitter; button.disabled = true;
  try { const result = await api('/supervisor/import',{home:fields.get('home')}); $('import-dialog').close(); await loadProjects(); await refresh(); toast(`${result.added} agents imported.`); }
  catch(e) { $('import-error').textContent=e.message; } finally { button.disabled=false; }
};
function applySupervisor(supervisor) {
  if (supervisor.snapshot_epoch !== snapshotEpoch) {
    snapshotEpoch = supervisor.snapshot_epoch; snapshotRevision = -1;
  }
  if ((supervisor.snapshot_revision ?? 0) < snapshotRevision) return;
  snapshotRevision = supervisor.snapshot_revision ?? 0;
  fleets = [supervisor]; selectedFleet = supervisor.id;
  $('ui-update').hidden = !loadedUIRevision || !supervisor.ui_revision || supervisor.ui_revision === loadedUIRevision;
  $('connection').textContent = 'Live stream connected';
}
function renderArchived(tasks) {
  $('removed-agents').hidden = tasks.length === 0;
  $('archived-agents').innerHTML = tasks.length ? tasks.map(t=>`<div class="archived-row"><button class="button quiet" data-restore="${escapeHTML(t.id)}" title="Restore ${escapeHTML(agentName(t))}"><span class="archived-name">${escapeHTML(agentName(t))}</span><span class="archived-restore">Restore</span></button><button class="archived-delete" data-delete="${escapeHTML(t.id)}" aria-label="Delete &amp; clean up ${escapeHTML(agentName(t))}" title="Delete &amp; clean up">Delete</button></div>`).join('') : '<small class="muted">None</small>';
  $('archived-agents').querySelectorAll('[data-delete]').forEach(button => button.onclick = () => deleteAgent(button.dataset.delete));
  $('archived-agents').querySelectorAll('[data-restore]').forEach(button=>button.onclick=async()=>{
    try { await api(`/supervisor/agents/${encodeURIComponent(button.dataset.restore)}/restore`,{}); await refresh(); toast('Agent restored.'); } catch(e) { toast(e.message,true); }
  });
}
// In-page confirmation: native confirm() is blocked or invisible in some embedded browsers.
function confirmDialog(title, message, okLabel) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = title; $('confirm-message').textContent = message; $('confirm-ok').textContent = okLabel;
  return new Promise(resolve => {
    const finish = value => { dialog.removeEventListener('close', onClose); if (dialog.open) dialog.close(); resolve(value); };
    const onClose = () => finish(false);
    $('confirm-cancel').onclick = () => finish(false);
    $('confirm-form').onsubmit = event => { event.preventDefault(); finish(true); };
    dialog.addEventListener('close', onClose); dialog.showModal(); $('confirm-cancel').focus();
  });
}
async function deleteAgent(id) {
  if (!await confirmDialog(`Delete agent "${id}"?`, `This discards all uncommitted files and commits on its worker branch, its worktree, and saved conversation/run history. This cannot be undone. Its open terminal will close; any current worker turn will finish before cleanup. Other agents keep running. The project repository is kept.`, 'Delete & clean up')) return;
  try {
    const result = await api(`/supervisor/agents/${encodeURIComponent(id)}/delete`, {confirm:true});
    await refresh(); if(result.cleanup_error) { toast('Cleanup failed: '+result.cleanup_error,true); return; }
    toast(result.queued ? 'Deletion queued. This agent will be cleaned up after its current turn finishes.' : 'Agent deleted; worktree, worker branch and saved history cleaned up.');
  } catch(e) { toast(e.message,true); }
}
$('delete-agent').onclick = () => deleteAgent(selectedTask);
async function archiveAgent(id) {
  try { await api(`/supervisor/agents/${encodeURIComponent(id)}/remove`,{}); await refresh(); toast('Agent archived. Its current turn finishes; files are kept.'); }
  catch(e) { toast(e.message,true); }
}
$('remove-agent').onclick = () => archiveAgent(selectedTask);
$('supervisor-settings').onclick = () => {
  const supervisor = currentFleet(); if(!supervisor) return;
  const form = $('supervisor-form'); form.elements.provider.value = supervisor.supervisor.provider;
  form.elements.model.value = supervisor.supervisor.model || ''; form.elements.parallel.value = supervisor.max_parallel ?? 3;
  form.elements['parallel-unlimited'].checked = supervisor.max_parallel === null;
  form.elements.parallel.disabled = form.elements['parallel-unlimited'].checked;
  form.elements['parallel-unlimited'].onchange = () => {form.elements.parallel.disabled = form.elements['parallel-unlimited'].checked;};
  form.elements['keeper-enabled'].checked = !!supervisor.keeper?.enabled;
  form.elements['keeper-model'].value = supervisor.keeper?.model || 'qwen2.5-coder:7b';
  modelPicker(form.elements.model, form.elements.provider.value);
  effortPicker(form.elements.model,form.elements.effort,supervisor.supervisor.effort || 'medium');
  form.elements.provider.onchange = () => modelPicker(form.elements.model, form.elements.provider.value, '');
  loadModels().catch(e => toast(e.message,true));
  $('supervisor-error').textContent = ''; $('supervisor-dialog').showModal();
};
$('supervisor-form').onsubmit = async event => {
  event.preventDefault();
  const form=event.currentTarget,data=new FormData(form),button=event.submitter;
  const settings={supervisor:{...currentFleet().supervisor,provider:data.get('provider'),model:data.get('model').trim() || null,effort:data.get('effort')},
    max_parallel:form.elements['parallel-unlimited'].checked ? null : Number(data.get('parallel')),
    keeper:{enabled:form.elements['keeper-enabled'].checked,model:data.get('keeper-model').trim() || 'qwen2.5-coder:7b'}};
  button.disabled=true;
  try {
    await saveModel(data.get('provider'),data.get('model').trim());await api('/supervisor/settings',settings);
    applySupervisor((await api('/supervisor')).supervisor);render();$('supervisor-dialog').close();toast('Supervisor settings saved.');
  } catch(e){$('supervisor-error').textContent=e.message;}finally{button.disabled=false;}
};

function connectEvents() {
  events = new EventSource('/api/events');
  events.addEventListener('state',event=>{try{applySupervisor(JSON.parse(event.data));render();$('updated').textContent=`Live · ${new Date().toLocaleTimeString()}`;}catch(e){toast(e.message,true);}});
  events.addEventListener('tick',()=>{document.querySelectorAll('[data-until]').forEach(el=>el.textContent=countdown(Number(el.dataset.until)));loadLogs();});
  events.onopen=()=>{$('connection').textContent='Live stream connected';};
  events.onerror=()=>{$('connection').textContent='Stream reconnecting…';refresh();};
}
function updateProjectPickers() {
  document.querySelectorAll('[data-field=project]').forEach(select => {
    const path = select.closest('.task-fields').querySelector('[data-field=repo]').value;
    select.innerHTML = '<option value="">Choose a saved repository…</option>' + savedProjects.map(p =>
      `<option value="${escapeHTML(p.path)}" ${p.available ? '' : 'disabled'}>${escapeHTML(p.name)} — ${escapeHTML(p.path)}${p.available ? '' : ' (unavailable)'}</option>`).join('');
    select.value = path;
  });
}
function renderProjects() {
  $('project-count').textContent = savedProjects.length;
  const query = $('project-search').value.toLowerCase();
  const visible = savedProjects.filter(p => `${p.name} ${p.path}`.toLowerCase().includes(query));
  $('saved-projects').innerHTML = visible.length ? visible.map(p => `<div class="project-row"><div><strong>${escapeHTML(p.name)}</strong>${p.available ? '' : '<span class="muted"> · unavailable</span>'}<code>${escapeHTML(p.path)}</code></div><button class="button small" type="button" data-remove-project="${escapeHTML(p.path)}" aria-label="Remove ${escapeHTML(p.name)} from library">Remove</button></div>`).join('') : '<p class="muted">' + (query ? 'No matching repositories.' : 'No saved repositories yet. Scan a projects folder or add paths above.') + '</p>';
  $('saved-projects').querySelectorAll('[data-remove-project]').forEach(button => button.onclick = async () => {
    try { await api('/projects',{action:'remove',path:button.dataset.removeProject}); await loadProjects(); }
    catch(e) { $('projects-error').textContent = e.message; }
  });
  $('saved-folders').textContent = savedFolders.length ? 'Saved scan folders: ' + savedFolders.map(f => f.path).join(' · ') : '';
  $('rescan-projects').disabled = !savedFolders.length;
}
async function loadProjects() {
  const library = await api('/projects'); savedProjects = library.projects; savedFolders = library.folders;
  renderProjects(); updateProjectPickers();
}
async function openProjects() {
  $('projects-error').textContent = '';
  try { await loadProjects(); $('projects-dialog').showModal(); } catch(e) { toast(e.message,true); }
}
$('projects').onclick = openProjects; $('manage-task-projects').onclick = openProjects;
$('project-search').oninput = renderProjects;
async function changeProjects(body,button) {
  $('projects-error').textContent = ''; button.disabled = true;
  const previousLabel = button.textContent; button.textContent = body.action === 'add' ? 'Saving…' : 'Scanning…';
  try {
    const result = await api('/projects',body); await loadProjects();
    toast(`${result.added} ${result.added === 1 ? 'repository' : 'repositories'} added.` + (result.truncated ? ' Scan reached its directory limit; scan a smaller folder for the rest.' : ''));
  } catch(e) { $('projects-error').textContent = e.message; }
  finally { button.textContent = previousLabel; button.disabled = false; renderProjects(); }
}
$('scan-projects').onsubmit = event => { event.preventDefault(); const data = new FormData(event.currentTarget); changeProjects({action:'scan',folder:data.get('folder'),depth:Number(data.get('depth'))},event.submitter); };
$('rescan-projects').onclick = event => changeProjects({action:'rescan'},event.currentTarget);
$('add-projects').onsubmit = event => { event.preventDefault(); const data = new FormData(event.currentTarget); changeProjects({action:'add',paths:data.get('paths').split('\n').map(s=>s.trim()).filter(Boolean)},event.submitter); };
loadModels().catch(e => toast(e.message,true));
loadProjects().catch(e => toast(e.message,true));
const initialTab=new URLSearchParams(location.search).get('tab') || localStorage.getItem('loop-tab') || 'terminal';
selectTab(['native','activity','terminal','changes','usage','decisions'].includes(initialTab) ? initialTab : 'terminal');
$('reload-interface').onclick = () => {
  if ($('note').value) sessionStorage.setItem('loop-ui-draft', JSON.stringify({task:selectedTask,text:$('note').value}));
  const url=new URL(location.href);
  if(selectedTask) url.searchParams.set('agent',selectedTask);
  url.searchParams.set('tab',activeTab); url.searchParams.set('refresh',String(Date.now()));
  location.assign(url.href);
};
refresh().then(() => {
  try {
    const draft=JSON.parse(sessionStorage.getItem('loop-ui-draft') || 'null');
    if(draft?.task === selectedTask) { $('note').value=draft.text; sessionStorage.removeItem('loop-ui-draft'); }
  } catch {}
  connectEvents();
});
window.addEventListener('beforeunload',()=>events?.close());

new ResizeObserver(()=>{if(activeTab === 'native') {try{fit?.fit();}catch{}}}).observe($('native-terminal'));

// Keep the display preference local to this browser.
const themeToggle = $('theme-toggle');
function updateThemeToggle() {
  const light = document.documentElement.dataset.theme === 'light';
  themeToggle.textContent = light ? '☾ Dark mode' : '☀ Light mode';
  themeToggle.setAttribute('aria-label', light ? 'Switch to dark mode' : 'Switch to light mode');
}
themeToggle.addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('loop-theme', theme); } catch (_) {}
  updateThemeToggle();
});
updateThemeToggle();

// Dismiss workspace settings after choosing an action or clicking elsewhere.
document.addEventListener('click', event => {
  const menu = document.querySelector('.settings-menu');
  if (menu.open && (!menu.contains(event.target) || event.target.closest('.settings-dropdown button'))) menu.open = false;
});
document.addEventListener('keydown', event => {
  const menu = document.querySelector('.settings-menu');
  if (event.key === 'Escape' && menu.open) { menu.open = false; menu.querySelector('summary').focus(); }
});

// UX pass 1: dismiss popovers on outside click; menu closes after choosing an item.
document.addEventListener('click', event => {
  const menu = document.querySelector('.settings-menu');
  if (menu?.open && (!menu.contains(event.target) || event.target.closest('.settings-dropdown button'))) menu.open = false;
  document.querySelectorAll('.agent-card-menu[open]').forEach(m => { if (!m.contains(event.target)) m.open = false; });
});
// UX pass 2: Send stays disabled until there is direction to send; textarea grows with content.
(function noteComposer() {
  const note = $('note'), send = $('note-form').querySelector('button[type=submit]');
  const sync = () => {
    note.style.height = 'auto'; note.style.height = Math.min(note.scrollHeight, 150) + 'px';
    send.dataset.empty = note.value.trim() ? '' : '1';
  };
  note.addEventListener('input', sync);
  $('note-form').addEventListener('submit', event => { if (!note.value.trim()) { event.stopImmediatePropagation(); event.preventDefault(); note.focus(); } }, true);
  new MutationObserver(sync).observe(note, {attributes: true, attributeFilter: ['disabled']});
  setInterval(sync, 500); sync();
})();

// Always-visible connection state in the top bar (the full text lives in Settings).
(() => {
  const source = $('connection'), badge = $('bar-status');
  if (!source || !badge) return;
  const sync = () => {
    const text = source.textContent.trim();
    const state = /^Live|^Connected/.test(text) ? 'ok' : /interrupted/i.test(text) ? 'error' : 'pending';
    badge.dataset.state = state; badge.title = text;
    badge.querySelector('.bar-status-text').textContent = state === 'ok' ? 'Live' : state === 'error' ? 'Offline' : 'Connecting';
  };
  new MutationObserver(sync).observe(source, {childList: true, characterData: true, subtree: true});
  sync();
})();
