'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let fleets = [], selectedFleet = localStorage.getItem('loop-fleet'), selectedTask = new URLSearchParams(location.search).get('agent');
let events = null, snapshotEpoch = null, snapshotRevision = -1;
let activeTab = 'terminal', refreshing = false, taskNumber = 0;
let savedProjects = [], savedFolders = [];
let terminal = null, fit = null, socket = null, terminalKey = null, toastTimer;
function toast(message, error = false) {
  $('toast').textContent = message; $('toast').classList.toggle('error', error); $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, error ? 8000 : 4000);
}
async function api(path, body) {
  const response = await fetch('/api' + path, {method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : {'Content-Type':'application/json','X-Loop-Request':'1'},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const text = await response.text();
  let data; try { data = JSON.parse(text); } catch { data = {error: text}; }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
let modelCatalog = {}, modelCatalogPromise;
async function loadModels() {
  modelCatalogPromise ||= api('/models').then(data => {
    modelCatalog = data.providers;
    document.querySelectorAll('[data-model-picker]').forEach(select => select._reload());
  }).finally(() => { modelCatalogPromise = null; });
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
    input.after(select, custom);
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
  select.innerHTML = '<option value="">Provider default</option>' + models.map(m =>
    `<option value="${escapeHTML(m.id)}">${escapeHTML(m.label)} · ${escapeHTML(m.id)}${m.custom ? ' (saved custom)' : ''}</option>`
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
  selectedTask = id; selectTab('terminal'); render(); loadLogs();
  if (matchMedia('(max-width:1120px)').matches) document.querySelector('.inspector').scrollIntoView({block:'start',behavior:'smooth'});
}
function chooseFleet(id) {
  disconnectTerminal(); selectedFleet = id; selectedTask = null;
  localStorage.setItem('loop-fleet', id); render(); loadLogs();
}
function render() {
  if (!currentFleet() && fleets.length) selectedFleet = fleets[0].id;
  const fleet = currentFleet(), allTasks = fleet?.tasks || [], tasks = allTasks.filter(t => t.enabled !== false || t.attempt);
  renderArchived(allTasks.filter(t => t.enabled === false && !t.attempt));
  if (!tasks.some(t => t.id === selectedTask)) selectedTask = tasks[0]?.id || null;
  $('fleet-count').textContent = fleets.length;
  $('fleet-list').innerHTML = fleets.map(f => `<button class="fleet-link ${f.id === selectedFleet ? 'active' : ''}" data-fleet="${escapeHTML(f.id)}"><span>${escapeHTML(f.name)}</span><small>${f.tasks.length}</small></button>`).join('');
  $('fleet-list').querySelectorAll('button').forEach(b => b.onclick = () => chooseFleet(b.dataset.fleet));
  $('title').textContent = 'Your local supervisor.';
  $('fleet-label').textContent = 'SUPERVISOR';
  $('subtitle').textContent = fleet?.error || (fleet ? `${tasks.length} ${tasks.length === 1 ? 'agent' : 'agents'} · one persistent supervisor` : 'Add agents whenever you need another pair of hands.');
  $('subtitle').title = fleet?.home || '';
  $('stat-total').textContent = tasks.length;
  $('stat-active').textContent = tasks.filter(t => t.state === 'working' || t.state === 'verifying').length;
  $('stat-waiting').textContent = tasks.filter(t => t.state === 'waiting').length;
  $('stat-done').textContent = tasks.filter(t => t.state === 'complete').length;
  renderTokenMonitor(fleet);
  $('worker-count').textContent = String(tasks.length).padStart(2,'0');
  $('empty').hidden = tasks.length > 0; $('workspace').hidden = tasks.length === 0;
  $('fleet-actions').hidden = !fleet;
  if (fleet) {
    $('manager-state').textContent = fleet.running ? '● Manager running' : 'Manager stopped';
    $('manager-state').className = 'pill' + (fleet.running ? ' working' : '');
    $('stop').disabled = !fleet.running; $('resume').disabled = fleet.running || allTasks.some(t => t.terminal_open);
    $('keeper-status').textContent = fleet.keeper?.enabled ? `Optional keeper: ${fleet.keeper.model}` : 'Local keeper: off (deterministic recovery)';
    $('supervisor-label').textContent = `Supervisor: ${fleet.supervisor?.provider || 'unknown'} / ${fleet.supervisor?.model || 'default model'}`;
  }
  $('workers').innerHTML = tasks.map(t => {
    const hold = Object.values(t.provider_holds || {}).filter(h => h.until > Date.now()/1000).sort((a,b) => a.until-b.until)[0];
    const wait = t.state === 'waiting' ? `<div class="worker-wait">${escapeHTML(t.wait_reason?.slice(0,180))}${hold ? ` · reset in <span data-until="${hold.until}">${countdown(hold.until)}</span>` : t.retry_at ? ` · retry in <span data-until="${t.retry_at}">${countdown(t.retry_at)}</span>` : ''}</div>` : '';
    return `<button class="worker-card ${t.id === selectedTask ? 'selected' : ''}" data-task="${escapeHTML(t.id)}"><div class="worker-top"><strong>${escapeHTML(t.id)}</strong>${pill(t.signal || t.state)}</div><span class="repo">↳ ${escapeHTML(t.repo)}</span><div class="goal">${escapeHTML(t.goal || t.next_action)}</div>${wait}<div class="worker-bottom"><span class="model">${escapeHTML(t.provider)} / ${escapeHTML(t.model || 'default')}</span><span>${t.turns} turns · ${tokenNumber(t.usage ? t.usage.agent.total_tokens : Object.values(t.tokens || {}).reduce((a,b) => a+b,0))} tokens</span></div></button>`;
  }).join('');
  $('workers').querySelectorAll('button').forEach(b => b.onclick = () => chooseTask(b.dataset.task));
  const task = currentTask();
  if (!task) return;
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
  $('task-title').textContent = task.id; $('task-status').className = `pill ${task.state}`; $('task-status').textContent = task.signal || task.state;
  $('remove-agent').disabled = task.enabled === false;
  $('task-meta').textContent = `${task.branch}\n${task.worktree}`;
  $('task-meta').style.whiteSpace = 'pre-wrap';
  renderAgentUsage(task);
  $('open-terminal').disabled = fleet.running || !!task.attempt || ['working','verifying','commit_pending'].includes(task.state) || socket?.readyState === WebSocket.OPEN;
  $('open-terminal').textContent = task.terminal_open ? 'Reconnect shell' : 'Open terminal';
  $('close-terminal').disabled = !task.terminal_open && !terminalKey;
  const decisions = task.decisions || [];
  $('decisions-panel').innerHTML = decisions.length ? decisions.map(d => `<article class="decision"><small>${d.action === 'review' ? '✓ SUPERVISOR REVIEW' : d.action === 'continue' ? '↳ SUPERVISOR DIRECTION' : '◷ OPERATOR REQUIRED'} · ${new Date(d.at*1000).toLocaleTimeString()}</small><p>${escapeHTML(d.message)}</p></article>`).join('') : '<p class="muted">No interventions yet. The supervisor steps in when a worker needs help.</p>';
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
    } catch { add({kind:'message',text:line}); }
  }
  return items;
}
function renderConversation(data) {
  const panel=$('worker-terminal-output');
  const opened=new Set([...panel.querySelectorAll('details[open]')].map(el=>el.dataset.key));
  const message=(role,text,status='')=>`<article class="conversation-entry ${role === 'You' ? 'user-message' : ''}"><div class="conversation-role">${escapeHTML(role)}${status ? `<span>${escapeHTML(status)}</span>` : ''}</div><div class="conversation-text">${escapeHTML(text)}</div></article>`;
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
async function loadLogs() {
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
$('resume').onclick = () => fleetAction('resume'); $('stop').onclick = () => fleetAction('stop');
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
  activeTab = tab;
  document.querySelectorAll('[data-tab]').forEach(b => { b.classList.toggle('active', b.dataset.tab === tab); b.setAttribute('aria-selected', String(b.dataset.tab === tab)); });
  for (const name of ['activity','terminal','usage','decisions']) $(name+'-panel').hidden = name !== tab;
  if (['activity','terminal'].includes(tab)) loadLogs();
  if (tab === 'terminal') requestAnimationFrame(() => fit?.fit());
}
$('view-worker-terminal').onclick = () => { selectTab('terminal'); $('terminal-panel').scrollIntoView({block:'nearest',behavior:'smooth'}); };
document.querySelectorAll('[data-tab]').forEach(b => b.onclick = () => selectTab(b.dataset.tab));
function disconnectTerminal() {
  if (socket) { socket.onclose = null; socket.onmessage = null; socket.onerror = null; socket.close(); socket = null; }
  terminal?.dispose(); terminal = null; fit = null; terminalKey = null;
  $('terminal').hidden = true; $('terminal').replaceChildren(); $('terminal-state').textContent = 'Local worktree shell';
}
$('open-terminal').onclick = async () => {
  if (socket?.readyState === WebSocket.OPEN) return;
  disconnectTerminal();
  if (!window.Terminal || !window.FitAddon) { toast('Terminal assets are missing. Run scripts/setup-supervise-web.', true); return; }
  $('terminal').hidden = false;
  terminal = new Terminal({cursorBlink:true,fontFamily:'Menlo, ui-monospace, monospace',fontSize:12,scrollback:3000,theme:{background:'#0b0e0c',foreground:'#d2e0ce',cursor:'#c5f47b',selectionBackground:'#5b784755'}});
  fit = new FitAddon.FitAddon(); terminal.loadAddon(fit); terminal.open($('terminal')); fit.fit();
  terminalKey = `${selectedFleet}/${selectedTask}`;
  socket = new WebSocket(`ws://${location.host}/api${taskURL()}/terminal`); socket.binaryType = 'arraybuffer';
  const ws = socket;
  ws.onopen = () => { $('terminal-state').textContent = '● Connected · local shell'; ws.send(JSON.stringify({type:'resize',cols:terminal.cols,rows:terminal.rows})); terminal.focus(); refresh(); };
  ws.onmessage = event => {
    if (typeof event.data !== 'string') terminal?.write(new Uint8Array(event.data));
    else { try { const e = JSON.parse(event.data); if (e.type === 'exit') { terminal?.writeln(`\r\n[Shell exited: ${e.code}]`); refresh(); } } catch {} }
  };
  ws.onerror = () => toast('Cannot attach. Stop the manager, reconcile workers, and close any other terminal tab first.', true);
  ws.onclose = () => { $('terminal-state').textContent = 'Disconnected · shell may still be running'; refresh(); };
  terminal.onData(data => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'input',data})); });
  terminal.onResize(({cols,rows}) => { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({type:'resize',cols,rows})); });
};
$('close-terminal').onclick = async () => {
  try { await api(taskURL()+'/terminal/close',{}); disconnectTerminal(); toast('Local shell closed. Workers can resume.'); await refresh(); }
  catch(e) { toast(e.message,true); }
};
new ResizeObserver(() => { if (activeTab === 'terminal') { try { fit?.fit(); } catch {} } }).observe($('terminal'));
window.addEventListener('beforeunload', () => socket?.close());
function agentField(row,field) { return row.querySelector(`[data-field="${field}"]`) || row._options?.querySelector(`[data-field="${field}"]`); }
function addTask() {
  do { taskNumber++; } while(currentFleet()?.tasks.some(t => t.id === `task-${taskNumber}`));
  const row = document.createElement('section'); row.className = 'task-fields'; row.dataset.defaultId = `task-${taskNumber}`;
  row.innerHTML = `<div class="task-field-head"><span>AGENT ${String(taskNumber).padStart(2,'0')}</span><button type="button" class="icon-button remove-task" aria-label="Remove agent form">×</button></div><label>Saved project<select data-field="project" aria-label="Saved project"><option value="">Choose a saved repository, or enter a path…</option></select></label><label data-manual-repo>Repository root<input data-field="repo" placeholder="/Users/you/projects/app" required></label><label>Goal<textarea data-field="goal" rows="3" placeholder="What would you like this agent to do?" required></textarea></label><div class="form-row"><label>Worker provider<select data-field="provider"><option>codex</option><option>claude</option><option>antigravity</option><option>muse</option></select></label><label>Model<input data-field="model" placeholder="Provider default"></label></div><label>Worker effort<select data-field="effort" aria-label="Worker effort"><option>medium</option></select></label><label class="subagent-option"><input type="checkbox" data-field="allow-subagents"> Allow subagents</label><small class="muted">All project files · supervisor reviews the result</small>`;
  const options = document.createElement('section'); options.className = 'task-options'; row._options = options;
  options.innerHTML = `<h4>Agent ${String(taskNumber).padStart(2,'0')}</h4><label>Agent ID<input data-field="id" value="${row.dataset.defaultId}" pattern="[a-z0-9][a-z0-9-]{0,60}" placeholder="Generated automatically"></label><label>Verification command (optional)<input data-field="check" placeholder="For example: npm test"></label><small class="muted">Leave empty to use supervisor review.</small><div class="form-row"><label>Fallback provider<select data-field="fallback"><option value="">Wait for quota reset</option><option>codex</option><option>claude</option><option>antigravity</option><option>muse</option></select></label><label>Fallback model<input data-field="fallback-model" placeholder="Provider default"></label></div>`;
  row.querySelector('.remove-task').onclick = () => { if ($('task-fields').children.length > 1) { options.remove(); row.remove(); } else toast('Add at least one agent.', true); };
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
  updateProjectPickers();
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
    else tasks = [...$('task-fields').children].map(row => {
      const get = field => agentField(row,field).value.trim();
      const provider = get('provider'), fallback = get('fallback'), workers = [provider], worker_models = {};
      if (get('model')) worker_models[provider] = get('model');
      if (fallback && fallback !== provider) { workers.push(fallback); if (get('fallback-model')) worker_models[fallback] = get('fallback-model'); }
      return {id:get('id') || row.dataset.defaultId,repo:get('repo'),goal:get('goal'),acceptance:get('check') ? [get('check')] : [],options:{workers,worker_models,worker_effort:get('effort'),allow_subagents:row.querySelector('[data-field=allow-subagents]').checked}};
    });
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
  $('connection').textContent = 'Live stream connected';
}
function renderArchived(tasks) {
  $('archived-agents').innerHTML = tasks.length ? tasks.map(t=>`<button class="button quiet" data-restore="${escapeHTML(t.id)}">↶ ${escapeHTML(t.id)}</button>`).join('') : '<small class="muted">None</small>';
  $('archived-agents').querySelectorAll('[data-restore]').forEach(button=>button.onclick=async()=>{
    try { await api(`/supervisor/agents/${encodeURIComponent(button.dataset.restore)}/restore`,{}); await refresh(); toast('Agent restored.'); } catch(e) { toast(e.message,true); }
  });
}
$('remove-agent').onclick = async () => {
  try { await api(`/supervisor/agents/${encodeURIComponent(selectedTask)}/remove`,{}); await refresh(); toast('Agent removed from scheduling. Its current turn finishes; files are kept.'); }
  catch(e) { toast(e.message,true); }
};
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
  event.preventDefault(); const data = new FormData(event.currentTarget), button = event.submitter; button.disabled=true;
  try { await saveModel(data.get('provider'), data.get('model').trim()); await api('/supervisor/settings',{supervisor:{...currentFleet().supervisor,provider:data.get('provider'),model:data.get('model').trim() || null,effort:data.get('effort')},max_parallel:event.currentTarget.elements['parallel-unlimited'].checked ? null : Number(data.get('parallel')),keeper:{enabled:event.currentTarget.elements['keeper-enabled'].checked,model:data.get('keeper-model').trim() || 'qwen2.5-coder:7b'}}); $('supervisor-dialog').close(); await refresh(); toast('Supervisor settings saved.'); }
  catch(e) { $('supervisor-error').textContent=e.message; } finally { button.disabled=false; }
};
function connectEvents() {
  events = new EventSource('/api/events');
  events.addEventListener('state',event=>{try{applySupervisor(JSON.parse(event.data));render();$('updated').textContent=`Live · ${new Date().toLocaleTimeString()}`;}catch(e){toast(e.message,true);}});
  events.addEventListener('tick',()=>{document.querySelectorAll('[data-until]').forEach(el=>el.textContent=countdown(Number(el.dataset.until)));loadLogs();});
  events.onopen=()=>{$('connection').textContent='Live stream connected';};
  events.onerror=()=>{$('connection').textContent='Stream reconnecting…';};
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
loadProjects().catch(e => toast(e.message,true));
selectTab(new URLSearchParams(location.search).get('tab') === 'activity' ? 'activity' : 'terminal');
refresh().then(connectEvents);
window.addEventListener('beforeunload',()=>events?.close());
