const $ = (id) => document.getElementById(id);

const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[char]));
const title = (value) => String(value || "").replace(/[-_]/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
const providerClass = (value) => String(value || "command").toLowerCase().replace(/[^a-z]/g, "");
const timeAgo = (stamp) => {
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(stamp || 0)));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
};

function render(state) {
  const tasks = state.runs.flatMap((run) => run.tasks.map((task) => ({ ...task, runId: run.id })));
  const displayRuns = [...state.runs, ...(state.live_runs || []).filter((run) => !state.runs.some((local) => local.id === run.id && local.repo === run.repo))];
  state.displayRuns = displayRuns;
  const activeTasks = tasks.filter((task) => task.status === "running").map((task) => ({
    ...task,
    provider: task.selected_provider || "agent",
    attempt: task.attempts || 1,
    state: "running",
  }));
  const active = [...activeTasks, ...(state.active_attempts || []).filter((attempt) => !activeTasks.some((task) => task.runId === attempt.run_id && task.id === attempt.task_id))];
  const processAgents = (state.process_agents || []).map((agent) => ({
    ...agent,
    task_id: `${agent.role} · ${agent.project}`,
    run_id: agent.repo,
    provider: agent.provider,
    attempt: agent.pid,
    state: "working",
    processAgent: true,
  }));
  const workerAgents = state.worker_agents || [...active, ...processAgents];
  const allActive = workerAgents;
  const total = tasks.length;
  const done = tasks.filter((task) => task.status === "done").length;
  const ready = state.providers.filter((provider) => !provider.retry_at || Number(provider.retry_at) * 1000 < Date.now()).length;
  const completion = total ? Math.round((done / total) * 100) : 0;

  $("connection-label").textContent = state.available ? "live state" : "waiting for state";
  $("updated-label").textContent = `updated ${new Date(state.generated_at * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"})}`;
  $("date-label").textContent = new Date().toLocaleDateString([], {month:"short", day:"numeric", year:"numeric"});
  $("active-count").textContent = allActive.length;
  $("metric-active").textContent = allActive.length;
  $("metric-active-note").textContent = allActive.length ? "Working right now" : "No workers running";
  $("metric-completion").textContent = `${completion}%`;
  $("metric-completion-note").textContent = `${done} of ${total} tasks complete`;
  $("metric-runs").textContent = displayRuns.length;
  $("metric-providers").textContent = `${ready} / ${state.providers.length}`;
  $("agents-badge").textContent = `${allActive.length} active`;

  $("agents-list").innerHTML = allActive.length ? allActive.map((task, index) => {
    const providerName = task.provider || task.selected_provider || "agent";
    const provider = providerClass(providerName);
    const taskId = task.processAgent && task.progress && task.progress.task ? task.progress.task : (task.task_id || task.id || "worker");
    const runId = task.run_id || task.runId || "local";
    const progress = task.progress && task.progress.total ? `${task.progress.done}/${task.progress.total} complete` : (task.processAgent ? `pid ${task.pid}` : `attempt ${task.attempt || task.attempts || 1}`);
    return `<article class="agent-card" data-detail-type="agent" data-detail-index="${index}"><div class="agent-avatar avatar-${provider}">${esc(providerName.slice(0, 2).toUpperCase())}</div><div><div class="agent-name">${esc(title(providerName))}</div><div class="agent-task">${esc(title(taskId))} · ${esc(task.processAgent ? task.project : runId)}</div></div><div class="agent-state"><div class="state-label">${esc(task.state || "working")}</div><div class="state-time">${esc(progress)}</div></div></article>`;
  }).join("") : `<div class="empty-state"><span class="empty-orb"></span><span>No agents are running right now.<br>Completed and queued work stays visible below.</span></div>`;

  $("runs-list").innerHTML = displayRuns.length ? displayRuns.map((run, index) => {
    const percent = run.total ? Math.round((run.done / run.total) * 100) : 0;
    const status = run.total && run.done === run.total ? "complete" : run.tasks.some((task) => task.status === "running") ? "active" : "queued";
    return `<article class="run-row" data-detail-type="run" data-detail-index="${index}"><div class="run-top"><span class="run-name">${esc(title(run.id))}</span><span class="run-fraction">${run.done} / ${run.total}</span></div><div class="progress-track"><div class="progress-bar" style="width:${percent}%"></div></div><div class="run-meta"><span class="run-status ${status}">${status}</span><span>${esc(run.branch || "local")}</span></div></article>`;
  }).join("") : `<div class="empty-state">No runs recorded yet.</div>`;

  $("timeline").innerHTML = state.events.length ? state.events.slice(0, 8).map((event, index) => `<article class="event" data-detail-type="event" data-detail-index="${index}"><span class="event-dot"></span><div><div class="event-kind">${esc(title(event.kind))}</div><div class="event-detail">${esc(event.task_id || event.run_id || "system")} · ${esc(event.detail || "")}</div></div><time class="event-time">${timeAgo(event.at)}</time></article>`).join("") : `<div class="empty-state">Activity will appear as the loop moves.</div>`;
  $("provider-list").innerHTML = state.providers.length ? state.providers.map((provider) => {
    const held = Number(provider.retry_at || 0) * 1000 > Date.now();
    const usage = (state.provider_usage || {})[provider.name] || {};
    const quota = usage.quota || {};
    const quotaText = quota.short_term == null ? "quota n/a" : `${quota.short_term}% 5h · ${quota.weekly ?? "—"}% 7d`;
    const recorded = `${Number(usage.tokens || 0).toLocaleString()} tokens · ${usage.calls || 0} calls`;
    return `<article class="provider-row"><div><div class="provider-name"><span class="provider-dot ${held ? "held" : ""}"></span>${esc(provider.name)}</div><div class="provider-usage">${esc(recorded)} <span>·</span> ${esc(quotaText)}</div></div><span class="provider-state">${held ? "held" : "ready"}</span></article>`;
  }).join("") : `<div class="empty-state">No providers registered.</div>`;
}

function openDetail(type, index, state) {
  const eventItems = state.events.slice(0, 8);
  const agentItems = [...(state.worker_agents || state.active_attempts || []), ...(state.process_agents || []).map((agent) => ({
    ...agent,
    task_id: `${agent.role} · ${agent.project}`,
    run_id: agent.repo,
    attempt: agent.pid,
  }))];
  state.runs.forEach((run) => run.tasks.filter((task) => task.status === "running").forEach((task) => agentItems.push({ ...task, run_id: run.id, task_id: task.id, provider: task.selected_provider, attempt: task.attempts })));
  const item = type === "agent" ? agentItems[index] : type === "run" ? (state.displayRuns || state.runs)[index] : eventItems[index];
  if (!item) return;
  const task = type === "run" ? (item.tasks.find((entry) => entry.status === "running") || item.tasks[0] || {}) : item;
  const name = type === "event" ? title(item.kind) : type === "run" ? title(item.id) : title(item.task_id || item.id || "Agent task");
  $("detail-kicker").textContent = type === "run" ? "Run detail" : type === "event" ? "Activity detail" : "Agent detail";
  $("detail-title").textContent = name;
  $("detail-subtitle").textContent = type === "event" ? `${item.run_id || "system"} · ${timeAgo(item.at)}` : `${item.run_id || item.runId || "local"} · ${item.provider || item.selected_provider || "agent"}`;
  $("detail-status").textContent = item.status || item.state || (type === "run" ? `${item.done} of ${item.total} complete` : "observed");
  const facts = type === "run" ? {"Tasks": `${item.done} / ${item.total}`, "Branch": item.branch, "Workspace": item.workspace, "Reason": item.reason || "—"} : {"Provider": item.provider || item.selected_provider || "—", "Model": item.model || item.selected_model || "—", "Attempts": item.attempt || item.attempts || "—", "Tokens": item.tokens ?? "—"};
  $("detail-facts").innerHTML = Object.entries(facts).map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("");
  $("detail-prompt").textContent = type === "event" ? item.detail || "No additional detail recorded." : task.prompt || "No prompt recorded for this task.";
  const files = type === "run" ? item.tasks.flatMap((entry) => entry.files || []) : (task.files || []);
  $("detail-files").innerHTML = files.length ? files.map((file) => `<span>${esc(file)}</span>`).join("") : `<span>none recorded</span>`;
  const check = type === "run" ? (item.tasks[0] && item.tasks[0].check) : task.check;
  $("detail-check").textContent = Array.isArray(check) ? check.join(" ") || "No trusted check recorded." : check || "No trusted check recorded.";
  $("detail-backdrop").hidden = false;
  $("detail-close").focus();
}

async function refresh() {
  try {
    const response = await fetch(`/api/state?ts=${Date.now()}`);
    if (!response.ok) throw new Error("state unavailable");
    const state = await response.json();
    render(state);
    document.querySelectorAll("[data-detail-type]").forEach((element) => element.addEventListener("click", () => openDetail(element.dataset.detailType, Number(element.dataset.detailIndex), state)));
  } catch (error) {
    // Keep the product surface useful when someone opens index.html directly.
    // Live state becomes available as soon as the tiny local server is started.
    render({
      generated_at: Date.now() / 1000,
      available: false,
      runs: [],
      providers: [],
      events: [],
      active_attempts: [],
      process_agents: [],
      worker_agents: [],
      provider_usage: {},
    });
    $("connection-label").textContent = "offline preview";
    $("updated-label").textContent = "start server for live state";
  }
}

$("refresh-button").addEventListener("click", refresh);
$("detail-close").addEventListener("click", () => { $("detail-backdrop").hidden = true; });
$("detail-backdrop").addEventListener("click", (event) => { if (event.target.id === "detail-backdrop") $("detail-backdrop").hidden = true; });
document.addEventListener("keydown", (event) => { if (event.key === "Escape") $("detail-backdrop").hidden = true; });
refresh();
setInterval(refresh, 5000);
