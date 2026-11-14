"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const stages = ["Queued", "Implement", "Checks", "Review", "QA", "Integrate", "Done"];
  const phaseNames = {
    queued: "Queued", implementing: "Implementation", checking: "Checks",
    reviewing: "Review", preparing: "Preparing QA", qa_checks: "QA checks",
    qa: "QA", integrating: "Integration", awaiting_merge: "Awaiting merge",
    completed: "Completed",
  };
  const phaseStages = {
    queued: 0, implementing: 1, checking: 2, reviewing: 3, preparing: 4,
    qa_checks: 4, qa: 4, integrating: 5, awaiting_merge: 5, completed: 6,
  };
  const state = {
    data: null, factoryId: null, workId: null, filter: "all", search: "",
    tab: "overview", detail: null, detailError: null, detailPending: false,
    detailSequence: 0, detailAbort: null, lastSuccess: 0, receivedAt: null, requestElapsed: 0, refreshing: false,
    connectionError: null, terminal: new Map(), terminalPending: new Set(),
    outputs: new Map(), scroll: new Map(), logPositions: new Map(), openChecks: new Set(),
    openAssignments: new Set(), expanded: false,
    renderedPanelKey: null,
    lastPanelSignature: null, toastTimer: null,
  };

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }
  function append(parent, ...children) {
    children.flat().filter(Boolean).forEach(child => parent.append(child));
    return parent;
  }
  function human(value) {
    const text = String(value || "Unknown").replaceAll("_", " ");
    return text === "qa" ? "QA" : text.replace(/^./, c => c.toUpperCase());
  }
  function roleName(value) {
    return { implementer: "Implementation agent", reviewer: "Review agent", qa: "QA agent" }[value] || human(value);
  }
  function timestamp(value) {
    if (typeof value === "number") return value * 1000;
    const result = Date.parse(value || "");
    return Number.isFinite(result) ? result : null;
  }
  function relative(value) {
    const time = timestamp(value);
    if (time === null) return "Time unavailable";
    const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
    if (seconds < 10) return "Just now";
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    return `${Math.floor(seconds / 86400)}d ago`;
  }
  function timeNode(value, className = "") {
    const element = node("time", className, relative(value));
    const time = timestamp(value);
    if (time !== null) {
      element.dateTime = new Date(time).toISOString();
      element.title = new Date(time).toLocaleString();
    }
    return element;
  }
  function safeLink(url, label) {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return null;
    try {
      const parsed = new URL(url);
      if (!["https:", "http:"].includes(parsed.protocol)) return null;
      const link = node("a", "", label);
      link.href = parsed.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      return link;
    } catch { return null; }
  }
  function badge(label, tone = "") {
    return append(node("span", `badge ${tone}`), node("span", "badge-dot"), node("span", "", label));
  }
  function isAgentWorking(work) {
    if (work?.agent_working !== true || state.connectionError || state.receivedAt === null) return false;
    const generated = state.data?.generated_at;
    const observed = work.agent_observed_at;
    if (!Number.isFinite(generated) || !Number.isFinite(observed)) return false;
    const initialAge = Math.max(0, generated - observed) * 1000;
    const elapsed = Math.max(0, performance.now() - state.receivedAt);
    return initialAge + state.requestElapsed + elapsed <= 15000;
  }
  function expireWorkingIndicators() {
    const work = new Map((selectedFactory()?.work || []).map(item => [item.id, item]));
    if (!$$(".agent-working").some(indicator => !isAgentWorking(work.get(indicator.dataset.workId)))) return;
    const headerScroll = $("#detail-header").scrollTop;
    renderQueue(); renderDetailHeader();
    $("#detail-header").scrollTop = headerScroll;
  }
  function workingIndicator(work, label = "Agent working") {
    const indicator = node("span", "agent-working");
    indicator.dataset.workId = work.id;
    indicator.title = "Worker reported working in the latest coordinator observation (within 15 seconds).";
    const orbit = node("span", "agent-orbit");
    orbit.setAttribute("aria-hidden", "true");
    return append(indicator, orbit, node("span", "", label));
  }
  function empty(title, message, className = "") {
    return append(node("div", `empty-state ${className}`),
      node("span", "empty-orbit"), node("h3", "", title), node("p", "", message));
  }
  function section(title) {
    return append(node("section", "detail-section"), node("h3", "", title));
  }
  function selectedFactory() {
    return state.data?.factories?.find(factory => factory.id === state.factoryId) || null;
  }
  function selectedWork() {
    return selectedFactory()?.work?.find(work => work.id === state.workId) || null;
  }
  function waitingDependencies(work) {
    return (work.dependencies || []).filter(dependency => dependency.state !== "completed");
  }
  function bucket(work) {
    if (work.phase === "completed") return "done";
    if (selectedFactory()?.paused) return "waiting";
    if (["paused", "deferred", "quarantined", "blocked", "backoff", "reconciling"].includes(work.control)
        || waitingDependencies(work).length || work.phase === "awaiting_merge") return "waiting";
    return "active";
  }
  function workStatus(work) {
    if (work.phase === "completed") return { label: "Completed", tone: "success" };
    if (selectedFactory()?.paused) return { label: "Factory paused", tone: "warning" };
    if (work.control === "paused") return { label: "Paused", tone: "warning" };
    if (work.control === "quarantined") return { label: "Needs attention", tone: "error" };
    if (["deferred", "blocked"].includes(work.control)) return { label: "Needs input", tone: "warning" };
    if (work.phase === "awaiting_merge") return { label: "Awaiting merge", tone: "warning" };
    if (waitingDependencies(work).length) return { label: "Waiting", tone: "warning" };
    if (["backoff", "reconciling"].includes(work.control)) return { label: "Recovering", tone: "warning" };
    return { label: phaseNames[work.phase] || human(work.phase), tone: work.active_role ? "active" : "" };
  }
  function workReason(work) {
    if (work.phase === "completed") return "";
    if (work.control === "paused") return "Paused by the owner";
    if (selectedFactory()?.paused) return "Factory paused; work is preserved";
    const dependencies = waitingDependencies(work);
    if (dependencies.length) return `Waiting for ${dependencies.map(item => item.title || "a dependency").join(", ")}`;
    if (work.latest_failure) return ["backoff", "reconciling", "deferred", "quarantined", "blocked"].includes(work.control)
      ? work.latest_failure : `Previous interruption: ${work.latest_failure}`;
    if (work.phase === "awaiting_merge") return "The validated pull request is waiting for checks, review, or merge";
    return "";
  }
  function factoryStatus(factory) {
    if (factory.paused) return factory.coordinator_running ? "Paused, watcher running" : "Paused";
    return factory.coordinator_running ? "Running" : "Watcher stopped";
  }
  function currentKey() { return `${state.factoryId || ""}/${state.workId || ""}`; }
  function visibleWork(factory) {
    return (factory?.work || []).filter(work =>
      (state.filter === "all" || bucket(work) === state.filter)
      && String(work.title || "").toLowerCase().includes(state.search.toLowerCase()))
      .sort((a, b) => {
        const rank = { active: 0, waiting: 1, done: 2 };
        const group = rank[bucket(a)] - rank[bucket(b)];
        if (group) return group;
        if (bucket(a) === "active") {
          const queued = Number(a.phase === "queued") - Number(b.phase === "queued");
          if (queued) return queued;
          if (a.phase !== "queued") return (b.updated || 0) - (a.updated || 0);
        }
        if (bucket(a) === "done") return (b.updated || 0) - (a.updated || 0);
        return (a.created || 0) - (b.created || 0);
      });
  }

  async function request(url, signal) {
    const controller = new AbortController();
    const cancel = () => controller.abort();
    let timedOut = false;
    if (signal?.aborted) controller.abort();
    signal?.addEventListener("abort", cancel, { once: true });
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 8000);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal });
      let data;
      try { data = await response.json(); } catch { throw new Error("The local server returned an unreadable response."); }
      if (!response.ok) {
        const error = new Error(typeof data.error === "string" ? data.error : "The requested factory data is unavailable.");
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (timedOut) throw new Error("The local server did not respond in time. Refresh will retry.");
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", cancel);
    }
  }
  function showToast(message) {
    const toast = $("#toast");
    clearTimeout(state.toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    state.toastTimer = setTimeout(() => { toast.hidden = true; }, 2600);
  }
  async function copyText(text, button) {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try { await navigator.clipboard.writeText(text); copied = true; } catch { /* Try the local fallback. */ }
    }
    if (!copied) {
      const previous = document.activeElement;
      const input = node("textarea", "visually-hidden");
      input.value = text;
      input.setAttribute("aria-label", "Text to copy");
      document.body.append(input);
      input.focus(); input.select();
      try { copied = document.execCommand("copy"); } catch { copied = false; }
      input.remove();
      if (previous instanceof HTMLElement) previous.focus({ preventScroll: true });
    }
    if (copied) {
      const original = button.textContent;
      button.textContent = "Copied";
      showToast("Copied to clipboard");
      setTimeout(() => { if (button.isConnected) button.textContent = original; }, 1600);
    } else {
      showToast("Copy unavailable. Select and copy the visible text.");
    }
  }
  function copyButton(text, label = "Copy", ariaLabel = "Copy command") {
    const button = node("button", "button button-small", label);
    button.type = "button";
    button.setAttribute("aria-label", ariaLabel);
    let hash = 0;
    for (const character of String(text)) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
    button.dataset.focusKey = `copy:${ariaLabel}:${hash >>> 0}`;
    button.addEventListener("click", () => copyText(String(text), button));
    return button;
  }
  function commandRow(command, label) {
    if (!command) return null;
    const container = node("div", "");
    if (label) container.append(node("p", "secondary-line", label));
    container.append(append(node("div", "command-row"), node("code", "", command),
      copyButton(command, "Copy", `Copy ${label || "command"}`)));
    return container;
  }
  function restoreFocus(key, root) {
    if (!key) return;
    const target = $$('[data-focus-key]', root).find(element => element.dataset.focusKey === key);
    target?.focus({ preventScroll: true });
  }

  function renderFactories() {
    const list = $("#factory-list");
    const focus = document.activeElement?.dataset?.focusKey;
    const scroll = list.scrollTop;
    const horizontal = list.scrollLeft;
    const factories = state.data?.factories || [];
    $("#factory-count").textContent = factories.length ? String(factories.length) : "";
    if (!factories.length) {
      list.replaceChildren(node("p", "rail-empty", state.data ? "No factories found" : "Connecting to local factories…"));
      return;
    }
    list.replaceChildren(...factories.map(factory => {
      const button = node("button", "factory-button");
      button.type = "button";
      button.dataset.focusKey = `factory:${factory.id}`;
      button.setAttribute("aria-current", String(factory.id === state.factoryId));
      append(button, append(node("span", "factory-button-name"), node("span", "", factory.name),
        node("span", "count-label", (factory.work || []).length)),
      append(node("span", "factory-button-meta"),
        node("span", `rail-dot ${factory.paused ? "paused" : factory.coordinator_running ? "running" : ""}`),
        node("span", "", factoryStatus(factory))));
      button.addEventListener("click", () => selectFactory(factory.id));
      return button;
    }));
    list.scrollTop = scroll; list.scrollLeft = horizontal;
    restoreFocus(focus, list);
  }
  function renderHeader() {
    const factory = selectedFactory();
    $("#factory-title").textContent = factory?.name || "Your local factories";
    const repositories = factory?.repositories || [];
    const names = repositories.map(repo => typeof repo === "string" ? repo : repo.name).filter(Boolean);
    $("#factory-description").textContent = factory
      ? names.length ? names.join(", ") : "Local work, validation, and integration"
      : "Work, evidence, and the next step.";
    const status = $("#factory-state");
    status.hidden = !factory;
    if (factory) {
      const watcher = factory.watcher;
      let label = factory.coordinator_running ? "Coordinator running" : "Coordinator stopped";
      if (factory.paused) label += "; factory paused";
      status.replaceChildren(node("span", `status-dot`), node("strong", "", label));
      if (watcher?.enabled) status.append(node("span", "", `Issue intake ${human(watcher.state || "enabled").toLowerCase()}`));
      if (factory.last_activity) status.append(timeNode(factory.last_activity));
    }
    renderConnection();
  }
  function renderConnection() {
    const element = $("#connection-state");
    const age = state.lastSuccess ? Date.now() - state.lastSuccess : Infinity;
    let mode = "";
    let label = "Connecting";
    if (state.connectionError) { mode = "offline"; label = "Reconnecting"; }
    else if (state.lastSuccess && age > 6500) { mode = "stale"; label = "Data may be stale"; }
    else if (state.lastSuccess) { mode = "live"; label = "Live"; }
    element.className = `connection-state ${mode}`;
    if (element.dataset.label !== label) {
      element.replaceChildren(node("span", "status-dot"), node("span", "", label));
      element.dataset.label = label;
    }
    $("#updated-at").textContent = state.lastSuccess ? `Updated ${relative(state.lastSuccess / 1000).toLowerCase()}` : "Waiting for the first update";
    const notice = $("#notice");
    const errors = state.data?.errors || [];
    notice.hidden = !state.connectionError && !errors.length;
    notice.classList.toggle("error", Boolean(state.connectionError));
    const signature = JSON.stringify([state.connectionError, Boolean(state.data), errors]);
    if (notice.dataset.signature === signature) return;
    notice.dataset.signature = signature;
    if (state.connectionError) {
      notice.replaceChildren(node("strong", "", "Connection interrupted. "),
        node("span", "", state.data
          ? "Showing the last received state. Automatic refresh will retry."
          : "Keep the local factory UI server running, then refresh. Automatic retry is active."));
    } else if (errors.length) {
      const details = node("details", "");
      append(details, node("summary", "", "Show details"), node("pre", "", errors.join("\n")));
      notice.replaceChildren(node("strong", "", "Some factory data could not be read. "),
        node("span", "", "Available factories remain visible."), details);
    }
  }
  function renderPipeline() {
    const work = selectedWork();
    const index = work ? (phaseStages[work.phase] ?? 0) : -1;
    $("#pipeline-caption").textContent = work?.title || "Select a work item to trace its progress";
    $("#pipeline").replaceChildren(...stages.map((label, position) => {
      const stage = node("li", "pipeline-stage");
      const done = work?.phase === "completed";
      if (position < index) stage.classList.add("completed");
      if (position === index) {
        stage.classList.add("current");
        if (done) stage.classList.add("done");
        else if (work && bucket(work) === "waiting") stage.classList.add("waiting");
        stage.setAttribute("aria-current", "step");
      }
      append(stage, node("span", "stage-marker", position < index || (done && position === index) ? "✓" : position + 1),
        node("span", "", label));
      return stage;
    }));
  }
  function renderQueue() {
    const factory = selectedFactory();
    const list = $("#work-list");
    const focus = document.activeElement?.dataset?.focusKey;
    const scroll = list.scrollTop;
    const work = visibleWork(factory);
    const total = factory?.work?.length || 0;
    $("#work-count").textContent = total ? `${work.length} of ${total}` : "";
    $$("#filters button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.filter === state.filter)));
    if (!work.length) {
      let title = "No work here yet";
      let message = "Submit an issue or work item to begin its first orbit.";
      if (!factory && state.data) { title = "Set up your first factory"; message = "Run pluto factory setup in your terminal, then refresh."; }
      else if (!state.data) { title = "Connecting to your factories"; message = "Live work will appear here."; }
      else if (state.search || state.filter !== "all") { title = "No matching work"; message = "Choose another filter or clear your search."; }
      list.replaceChildren(empty(title, message));
      return;
    }
    list.replaceChildren(...work.map(item => {
      const button = node("button", "work-row");
      button.type = "button";
      button.dataset.workId = item.id;
      button.dataset.focusKey = `work:${item.id}`;
      button.setAttribute("aria-current", String(item.id === state.workId));
      const status = workStatus(item);
      const statusGroup = append(node("span", "work-status-group"), badge(status.label, status.tone));
      if (isAgentWorking(item)) statusGroup.append(workingIndicator(item));
      append(button, append(node("span", "work-row-top"), statusGroup, timeNode(item.updated, "work-age")),
        node("span", "work-title", item.title));
      const reason = workReason(item);
      if (reason) button.append(node("span", `work-reason ${bucket(item) === "waiting" ? "waiting" : ""}`, reason));
      const meta = node("span", "work-meta");
      if (item.active_role) meta.append(node("span", "", `${bucket(item) === "waiting" ? "Last role: " : ""}${roleName(item.active_role)}`));
      if (item.source_url) meta.append(node("span", "", "From issue"));
      if (item.pr_links?.length) meta.append(node("span", "", `${item.pr_links.length} pull ${item.pr_links.length === 1 ? "request" : "requests"}`));
      if (meta.childNodes.length) button.append(meta);
      button.addEventListener("click", () => { selectWork(item.id); revealDetail(); });
      button.addEventListener("keydown", queueKeyboard);
      const wrapper = node("div", "work-list-item");
      wrapper.setAttribute("role", "listitem");
      return append(wrapper, button);
    }));
    list.scrollTop = scroll;
    restoreFocus(focus, list);
  }
  function queueKeyboard(event) {
    const buttons = $$(".work-row");
    const index = buttons.indexOf(event.currentTarget);
    let next;
    if (event.key === "ArrowDown") next = buttons[Math.min(index + 1, buttons.length - 1)];
    if (event.key === "ArrowUp") next = buttons[Math.max(index - 1, 0)];
    if (event.key === "Home") next = buttons[0];
    if (event.key === "End") next = buttons.at(-1);
    if (next) { event.preventDefault(); next.focus(); }
  }

  function setTaskExpanded(expanded, restoreFocus = false) {
    state.expanded = Boolean(expanded && selectedWork());
    document.body.classList.toggle("task-expanded", state.expanded);
    const button = $("#expand-task");
    button.setAttribute("aria-expanded", String(state.expanded));
    button.textContent = state.expanded ? "Restore workspace" : "Expand task";
    button.title = state.expanded ? "Restore workspace (Escape)" : "Expand task reading area";
    if (restoreFocus) button.focus({ preventScroll: true });
  }
  function renderDetailHeader() {
    const summary = selectedWork();
    const factory = selectedFactory();
    const header = $("#detail-header");
    $("#detail-tabs").hidden = !summary;
    $("#detail-tools").hidden = !summary;
    if (!summary) {
      if (state.expanded) setTaskExpanded(false);
      header.replaceChildren(); return;
    }
    const work = state.detail?.work?.id === summary.id ? { ...state.detail.work, ...summary } : summary;
    const status = workStatus(work);
    header.replaceChildren(badge(status.label, status.tone), node("h2", "detail-title", work.title));
    const meta = append(node("div", "detail-meta"), node("span", "", phaseNames[work.phase] || human(work.phase)), timeNode(work.updated));
    if (isAgentWorking(work)) {
      meta.append(workingIndicator(work, work.active_role ? `${roleName(work.active_role)} working` : "Agent working"));
    } else if (work.active_role) meta.append(node("span", "", roleName(work.active_role)));
    header.append(meta);
    const links = node("div", "detail-links");
    append(links, safeLink(work.source_url, "Open source issue"), (work.pr_links || []).map(pr => safeLink(pr.url, pr.title || "Open pull request")));
    if (links.childNodes.length) header.append(links);
    let alert = workReason(work);
    if (work.control === "paused" && factory?.coordinator_running) alert = "This task is paused. The factory watcher is still running.";
    else if (factory?.paused && factory.coordinator_running) alert = "The factory is paused. Its watcher process is running, but no new work will be dispatched.";
    else if (!factory?.coordinator_running && work.phase !== "completed") alert = alert ? `${alert}. The factory coordinator is stopped.` : "The factory coordinator is stopped. Work is preserved until it resumes.";
    if (alert) header.append(node("p", `detail-alert ${status.tone !== "error" ? "waiting" : ""}`, alert));
  }
  function savePanelScroll() {
    const panel = $("#detail-content");
    if (state.renderedPanelKey) state.scroll.set(state.renderedPanelKey, panel.scrollTop);
    $$('[data-log-key]', panel).forEach(log => state.logPositions.set(log.dataset.logKey, {
      top: log.scrollTop, tail: log.scrollHeight - log.clientHeight - log.scrollTop < 24,
    }));
    $$("details.check", panel).forEach(check => {
      if (check.open) state.openChecks.add(check.dataset.checkId);
      else state.openChecks.delete(check.dataset.checkId);
    });
  }
  function renderDetail(force = false) {
    renderDetailHeader();
    const panel = $("#detail-content");
    $$("#detail-tabs button").forEach(button => {
      const active = button.dataset.tab === state.tab;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    });
    panel.setAttribute("aria-labelledby", `tab-${state.tab}`);
    if (!selectedWork()) {
      panel.replaceChildren(empty("Follow a piece of work", "Select an item to see its assignment, checks, and activity.", "detail-empty"));
      state.lastPanelSignature = null;
      state.renderedPanelKey = null;
      return;
    }
    const key = currentKey();
    const panelKey = `${key}/${state.tab}`;
    const signature = JSON.stringify([key, state.tab, state.detail, state.detailError, state.terminal.get(key), state.terminalPending.has(key), state.outputs.get(key)]);
    if (!force && signature === state.lastPanelSignature) return;
    const focus = document.activeElement?.dataset?.focusKey;
    savePanelScroll();
    if (state.detailError) panel.replaceChildren(empty("Task details are unavailable", state.detailError));
    else if (!state.detail || state.detail.work?.id !== state.workId) panel.replaceChildren(empty("Loading task details", "The queue remains available while this task loads."));
    else {
      const work = state.detail.work;
      const content = {
        overview: () => renderOverview(work), activity: () => renderActivity(work),
        checks: () => renderChecks(work), output: () => renderOutput(work),
      }[state.tab]();
      panel.replaceChildren(...content);
    }
    panel.scrollTop = state.scroll.get(panelKey) ?? 0;
    $$('[data-log-key]', panel).forEach(log => {
      const position = state.logPositions.get(log.dataset.logKey);
      if (position) log.scrollTop = position.tail ? log.scrollHeight : position.top;
    });
    state.renderedPanelKey = panelKey;
    restoreFocus(focus, panel);
    state.lastPanelSignature = signature;
  }
  function renderOverview(work) {
    const sections = [];
    if (work.revision_reason) sections.push(append(section("Current revision"), node("p", "body-copy", work.revision_reason)));
    const acceptance = section("Acceptance criteria");
    if (work.acceptance?.length) acceptance.append(append(node("ol", "acceptance-list"), work.acceptance.map(text => node("li", "", text))));
    else acceptance.append(node("p", "body-copy", "No acceptance criteria were recorded."));
    sections.push(acceptance);
    if (work.dependencies?.length) {
      const list = node("ul", "dependency-list");
      work.dependencies.forEach(dependency => list.append(append(node("li", ""), node("span", "", dependency.title || "Dependency"),
        badge(human(dependency.state), dependency.state === "completed" ? "success" : "warning"))));
      sections.push(append(section("Dependencies"), list));
    }
    const runs = [...(work.runs || [])].sort((a, b) => (b.updated || 0) - (a.updated || 0));
    let workers = null;
    if (runs.length) {
      const list = node("ul", "worker-list");
      const roles = new Set();
      runs.forEach(run => {
        if (roles.has(run.role)) return;
        roles.add(run.role);
        list.append(append(node("li", "worker-row"),
          append(node("span", ""), node("span", "worker-role", roleName(run.role)), timeNode(run.updated, "secondary-line")),
          badge(human(run.state), run.state === "running" ? "active" : run.state === "failed" ? "error" : "")));
      });
      workers = append(section("Worker sessions"), list);
    }
    let reportsSection = null;
    if (work.reports?.length) {
      const reports = section("Latest reports");
      [...work.reports].sort((a, b) => (b.created || 0) - (a.created || 0)).slice(0, 4).forEach(report => {
        const item = node("article", "report");
        append(item, append(node("div", "report-heading"), node("strong", "", roleName(report.role)),
          badge(human(report.outcome), report.outcome === "ready" ? "success" : report.outcome === "error" ? "error" : "warning")),
        node("p", "body-copy", report.summary));
        if (report.remaining_work?.length) item.append(node("p", "secondary-line", `Remaining: ${report.remaining_work.join("; ")}`));
        if (report.artifacts?.length) {
          const artifacts = node("ul", "artifact-list");
          report.artifacts.forEach(artifact => artifacts.append(append(node("li", ""), node("span", "artifact-path", artifact), copyButton(artifact, "Copy", "Copy artifact path"))));
          item.append(artifacts);
        }
        reports.append(item);
      });
      reportsSection = reports;
    }
    if (work.herdr) {
      const herdr = section("Continue in Herdr");
      if (work.herdr.available) {
        herdr.append(node("p", "body-copy", "Open this task’s active worker in Herdr. The command rechecks that it still belongs to this task."));
        append(herdr, commandRow(work.herdr.open_command, "Show this task in Herdr"),
          commandRow(work.herdr.terminal_command, "Attach to this task’s worker"));
      } else {
        herdr.append(node("p", "body-copy", "This task has no active worker to open. Its recorded reports, checks, and activity remain available here."));
      }
      sections.push(herdr);
    }
    if (reportsSection) sections.push(reportsSection);
    if (workers) sections.push(workers);
    const assignment = section("Source assignment");
    const disclosure = node("details", "assignment-disclosure");
    const assignmentKey = currentKey();
    disclosure.open = state.openAssignments.has(assignmentKey);
    const summary = node("summary", "", "Read the full assignment");
    summary.dataset.focusKey = "assignment-disclosure";
    disclosure.addEventListener("toggle", () => {
      if (disclosure.open) state.openAssignments.add(assignmentKey);
      else state.openAssignments.delete(assignmentKey);
    });
    append(disclosure, summary, node("p", "body-copy", work.instruction || "No assignment text was recorded."));
    assignment.append(disclosure);
    sections.push(assignment);
    const metadata = node("dl", "metadata");
    append(metadata, node("dt", "", "Created"), node("dd", "", timestamp(work.created) ? new Date(timestamp(work.created)).toLocaleString() : "Unavailable"),
      node("dt", "", "Task reference"), node("dd", "", work.id));
    sections.push(append(section("Record"), metadata));
    return sections;
  }
  function renderActivity(work) {
    const events = [...(work.events || [])].sort((a, b) => (b.created || 0) - (a.created || 0) || (b.seq || 0) - (a.seq || 0));
    if (!events.length) return [empty("No activity recorded", "Lifecycle events will appear as the factory moves this work forward.")];
    const list = node("ol", "event-list");
    events.forEach(event => {
      const tone = /fail|error|quarantin/.test(event.kind || "") ? "error" : /completed|passed|accepted/.test(event.kind || "") ? "success" : "";
      const item = node("li", `event ${tone}`);
      append(item, append(node("div", "event-heading"), node("strong", "", event.title || human(event.kind)), timeNode(event.created)),
        event.message ? node("p", "", event.message) : null);
      list.append(item);
    });
    return [list];
  }
  function commandText(evidence) {
    return Array.isArray(evidence.command) ? evidence.command.join(" ") : evidence.command || "Command not recorded";
  }
  function environmentText(environment) {
    return Object.entries(environment || {}).map(([key, value]) => `${key}=${value}`).join(", ");
  }
  function profileText(environment) {
    const features = Object.fromEntries(Object.entries(environment || {}).filter(([key, value]) =>
      /FEATURE|FLAG/.test(key) && !String(value).includes("/")));
    return environmentText(features) || "Default profile";
  }
  function logBlock(title, text, key, tone = "") {
    const wrapper = node("div", "");
    const heading = append(node("div", "log-heading"), node("span", "", title));
    if (text) heading.append(copyButton(text, "Copy", `Copy ${title.toLowerCase()}`));
    const pre = node("pre", `log ${tone}`, text || "No output recorded.");
    pre.dataset.logKey = key;
    pre.tabIndex = 0;
    pre.setAttribute("aria-label", title);
    append(wrapper, heading, pre);
    return wrapper;
  }
  function renderChecks(work) {
    const evidence = [...(work.evidence || [])].filter(item => item.command || item.stdout || item.stderr || typeof item.exit_code === "number")
      .sort((a, b) => (b.created || 0) - (a.created || 0));
    if (!evidence.length) return [empty("Checks have not run yet", "Independent evidence appears after the implementation is captured.")];
    const passed = evidence.filter(item => item.passed).length;
    const heading = section("Validation history");
    heading.append(node("p", "body-copy", "Includes earlier attempts. Outcomes describe each recorded run."));
    const summary = append(node("div", "checks-summary"), node("span", "", `${evidence.length} recorded checks`),
      badge(`${passed} passed, ${evidence.length - passed} failed`, evidence.length === passed ? "success" : "warning"));
    const list = node("div", "checks-list");
    evidence.forEach(check => {
      const details = node("details", "check");
      details.dataset.checkId = check.id;
      details.open = state.openChecks.has(check.id);
      details.addEventListener("toggle", () => {
        if (details.open) state.openChecks.add(check.id);
        else state.openChecks.delete(check.id);
      });
      const title = node("span", "check-title");
      const meta = node("span", "check-meta");
      append(meta, node("span", "", human(check.kind)), node("span", "", profileText(check.environment)),
        typeof check.seconds === "number" ? node("span", "", `${check.seconds.toFixed(1)}s`) : null);
      append(title, node("span", "check-name", check.repository || "Repository"), meta);
      const summary = append(node("summary", ""), title, badge(check.passed ? "Passed" : "Failed", check.passed ? "success" : "error"));
      summary.dataset.focusKey = `check:${check.id}`;
      const body = append(node("div", "check-body"),
        commandRow(commandText(check), "Executed command"),
        node("p", "secondary-line", `Exit ${check.exit_code ?? "unavailable"}. Captured ${relative(check.created).toLowerCase()}.`),
        logBlock("Standard output", check.stdout, `${check.id}:stdout`),
        check.stderr ? logBlock("Standard error", check.stderr, `${check.id}:stderr`, "stderr") : null);
      if (Object.keys(check.environment || {}).length) {
        const environment = node("dl", "metadata environment-values");
        Object.entries(check.environment).forEach(([key, value]) => append(environment,
          node("dt", "", key), node("dd", "", value)));
        append(body, node("h4", "check-subheading", "Environment"), environment);
      }
      append(details, summary, body);
      list.append(details);
    });
    return [heading, summary, list];
  }
  function renderOutput(work) {
    const content = [];
    const key = currentKey();
    const evidence = [...(work.evidence || [])].filter(item => item.command || item.stdout || item.stderr)
      .sort((a, b) => (b.created || 0) - (a.created || 0));
    if (evidence.length) {
      const preferred = state.outputs.get(key);
      const selected = evidence.find(item => item.id === preferred) || evidence[0];
      const select = node("select", "");
      select.id = "output-selection";
      select.dataset.focusKey = "output-selection";
      evidence.forEach(item => {
        // Short labels fit phones; the full command is shown under Checks.
        const label = [item.repository, item.kind && human(item.kind), profileText(item.environment)]
          .filter(Boolean).join(" · ");
        const option = node("option", "", label || commandText(item));
        option.title = commandText(item);
        option.value = item.id;
        option.selected = item.id === selected.id;
        select.append(option);
      });
      select.addEventListener("change", () => { state.outputs.set(key, select.value); renderDetail(true); });
      const label = node("label", "", "Check output"); label.htmlFor = select.id;
      content.push(append(node("div", "output-tools"), label, select));
      content.push(logBlock("Standard output", selected.stdout, `${selected.id}:stdout`));
      if (selected.stderr) content.push(logBlock("Standard error", selected.stderr, `${selected.id}:stderr`, "stderr"));
    } else content.push(node("p", "body-copy", "No command output has been recorded for this task."));
    const terminal = section("Worker terminal");
    terminal.classList.add("terminal-section");
    const capture = node("button", "button button-small", state.terminalPending.has(key) ? "Capturing…" : "Capture terminal");
    capture.type = "button";
    capture.setAttribute("aria-disabled", String(state.terminalPending.has(key)));
    capture.setAttribute("aria-busy", String(state.terminalPending.has(key)));
    capture.dataset.focusKey = "capture-terminal";
    capture.addEventListener("click", captureTerminal);
    const heading = append(node("div", "terminal-heading"), node("p", "body-copy", "Read a snapshot of this task’s verified worker."), capture);
    terminal.append(heading);
    const result = state.terminal.get(key);
    if (result?.available) {
      terminal.append(logBlock("Terminal snapshot", result.text, `${key}:terminal`, "terminal"));
      terminal.append(node("p", "hint", `Captured ${relative(result.captured_at).toLowerCase()}. This snapshot does not update automatically.`));
      append(terminal, commandRow(result.peek_command, "Show captured worker in Herdr"), commandRow(result.attach_command, "Attach to captured worker"));
    } else if (result) terminal.append(node("p", "hint", result.message || "No verified active terminal is available for this task."));
    else terminal.append(node("p", "hint", "Terminal contents are read only when you request a capture. Commands are copied, never executed here."));
    content.push(terminal);
    return content;
  }

  function renderAll() {
    renderFactories(); renderHeader(); renderPipeline(); renderQueue(); renderDetail();
  }
  function reconcileSelection() {
    const factories = state.data?.factories || [];
    if (!factories.some(factory => factory.id === state.factoryId)) state.factoryId = factories[0]?.id || null;
    const work = selectedFactory()?.work || [];
    if (!work.some(item => item.id === state.workId)) {
      state.workId = visibleWork(selectedFactory())[0]?.id || null;
      state.detail = null;
      state.lastPanelSignature = null;
    }
  }
  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    const requestStartedAt = performance.now();
    $("#refresh-button").setAttribute("aria-busy", "true");
    try {
      const data = await request("/api/state");
      if (data.schema_version !== 1 || !Array.isArray(data.factories)) throw new Error("Unsupported dashboard data format.");
      state.data = data;
      state.lastSuccess = Date.now();
      state.receivedAt = performance.now();
      state.requestElapsed = Math.max(0, state.receivedAt - requestStartedAt);
      state.connectionError = null;
      reconcileSelection();
      renderAll();
      await loadDetail();
    } catch (error) {
      state.connectionError = error.message;
      renderConnection();
      expireWorkingIndicators();
      if (!state.data) renderAll();
    } finally {
      state.refreshing = false;
      $("#refresh-button").setAttribute("aria-busy", "false");
    }
  }
  async function loadDetail() {
    if (!state.factoryId || !state.workId) return;
    state.detailAbort?.abort();
    const controller = new AbortController();
    state.detailAbort = controller;
    const sequence = ++state.detailSequence;
    const key = currentKey();
    state.detailPending = true;
    try {
      const data = await request(`/api/factories/${encodeURIComponent(state.factoryId)}/work/${encodeURIComponent(state.workId)}`, controller.signal);
      if (sequence !== state.detailSequence || key !== currentKey()) return;
      if (!data.work || data.work.id !== state.workId) throw new Error("The task changed while details were loading. Refresh to load its current state.");
      state.detail = data;
      state.detailError = null;
    } catch (error) {
      if (error.name === "AbortError" || sequence !== state.detailSequence) return;
      state.detailError = error.status === 409
        ? "The selected task changed. Its current state will be loaded on the next refresh."
        : error.message;
    } finally {
      if (sequence === state.detailSequence) {
        state.detailPending = false; renderDetail();
        // Re-align once loaded content settles the layout.
        if (state.revealPending) { state.revealPending = false; revealDetail("auto"); }
      }
    }
  }
  function selectFactory(id) {
    savePanelScroll();
    state.factoryId = id; state.workId = null; state.detail = null;
    state.detailError = null; state.lastPanelSignature = null;
    reconcileSelection(); renderAll(); loadDetail();
  }
  function selectWork(id) {
    if (id === state.workId) return;
    savePanelScroll();
    state.workId = id; state.detail = null; state.detailError = null;
    state.lastPanelSignature = null;
    renderQueue(); renderPipeline(); renderDetail(true); loadDetail();
  }
  // On narrow screens the detail renders below the queue; follow the tap.
  const compactLayout = window.matchMedia("(max-width: 680px)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  function scrollBehavior() { return reducedMotion.matches ? "auto" : "smooth"; }
  function revealDetail(behavior = scrollBehavior()) {
    if (!compactLayout.matches || state.expanded) return;
    state.revealPending = state.detailPending;
    $("#detail-pane").scrollIntoView({ behavior, block: "start" });
  }
  function returnToQueue() {
    const row = $('.work-row[aria-current="true"]') || $("#work-list");
    row.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
    if (row.matches(".work-row")) row.focus({ preventScroll: true });
  }
  function changeTab(tab, focus = false) {
    if (tab === state.tab) return;
    savePanelScroll();
    state.tab = tab; state.lastPanelSignature = null;
    renderDetail(true);
    if (focus) $(`#tab-${tab}`).focus();
  }
  async function captureTerminal() {
    if (!state.factoryId || !state.workId) return;
    const key = currentKey();
    if (state.terminalPending.has(key)) return;
    state.terminalPending.add(key); renderDetail(true);
    try {
      const result = await request(`/api/factories/${encodeURIComponent(state.factoryId)}/work/${encodeURIComponent(state.workId)}/terminal`);
      state.terminal.set(key, result);
    } catch (error) {
      state.terminal.set(key, { available: false, message: error.status === 409
        ? "This task’s worker changed. Refresh the task and capture its current terminal again."
        : error.message });
    } finally {
      state.terminalPending.delete(key);
      if (key === currentKey()) renderDetail(true);
    }
  }

  $$("#filters button").forEach(button => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    const visible = visibleWork(selectedFactory());
    if (!visible.some(work => work.id === state.workId)) {
      const next = visible[0]?.id || null;
      if (next) selectWork(next);
      else { savePanelScroll(); state.workId = null; state.detail = null; }
    }
    renderAll();
  }));
  $("#work-search").addEventListener("input", event => {
    state.search = event.target.value;
    renderQueue();
  });
  $$("#detail-tabs button").forEach(button => {
    button.addEventListener("click", () => changeTab(button.dataset.tab));
    button.addEventListener("keydown", event => {
      const tabs = $$("#detail-tabs button");
      let index = tabs.indexOf(button);
      if (event.key === "ArrowRight") index = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") index = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") index = 0;
      else if (event.key === "End") index = tabs.length - 1;
      else return;
      event.preventDefault(); changeTab(tabs[index].dataset.tab, true);
    });
  });
  $("#expand-task").addEventListener("click", () => setTaskExpanded(!state.expanded));
  $("#back-to-queue").addEventListener("click", returnToQueue);
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && state.expanded) {
      event.preventDefault();
      setTaskExpanded(false, true);
    }
  });
  $("#refresh-button").addEventListener("click", refresh);
  renderAll(); refresh();
  setInterval(refresh, 2000);
  setInterval(() => { renderConnection(); expireWorkingIndicators(); }, 1000);
  document.addEventListener("visibilitychange", expireWorkingIndicators);
})();
