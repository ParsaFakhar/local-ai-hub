/**
 * Local AI Hub — content orchestrator.
 *
 * Reads the domain configuration, extracts inputs with the generic
 * selector engine, submits universal tasks through the background page
 * and applies the results with the allowlisted applier.
 *
 * Three execution modes, all using the same validated path:
 *   apply   (default) plan the changes and execute them
 *   review            plan, show every item, apply only what the user ticks
 *   dry run           plan and show what *would* change, touch nothing
 *
 * The page itself gets no privileged channel.  The optional userscript bridge
 * (content/bridge.js) is opt-in per domain and carries input data only.
 */
'use strict';

(function () {
  const KIND = LAH.KIND;
  if (window.__localAiHubContentLoaded) return;
  window.__localAiHubContentLoaded = true;

  const state = {
    domain: location.hostname,
    settings: Object.assign({}, LAH.DEFAULT_SETTINGS),
    config: { features: {} },
    connected: false,
    items: new Map(),
    running: new Set(),
    tasks: new Map(),
    results: {},
    plans: {},
    panel: null,
    ui: null,
    collapsed: false,
    dryRun: false,
    review: false,
    picker: null,
    bridge: null,
  };

  // -------------------------------------------------------------------------
  function enabledFeatures() {
    const features = (state.config && state.config.features) || {};
    return Object.keys(features).filter((id) => features[id] && features[id].enabled);
  }

  function featureConfig(pluginId) {
    return ((state.config || {}).features || {})[pluginId] || null;
  }

  function log(message, level) {
    if (state.settings.debug || level === 'error') {
      console[level === 'error' ? 'error' : 'log'](`[LocalAIHub] ${message}`);
    }
    if (!state.ui) return;
    const line = document.createElement('div');
    line.className = `log-line ${level || 'info'}`;
    line.textContent = `${new Date().toLocaleTimeString()}  ${message}`;
    state.ui.log.prepend(line);
    while (state.ui.log.childElementCount > 40) state.ui.log.lastElementChild.remove();
  }

  // -------------------------------------------------------------------------
  // Panel (shadow DOM: page CSS cannot touch it, it cannot leak into the page)
  // -------------------------------------------------------------------------
  const PANEL_CSS = `
    :host { all: initial; }
    .panel { position: fixed; right: 16px; bottom: 16px; width: 340px; max-height: 76vh;
      display: flex; flex-direction: column; z-index: 2147483647;
      font: 13px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
      color: #e9ecef; background: #14161a; border: 1px solid #2b2f36;
      border-radius: 12px; box-shadow: 0 12px 32px rgba(0,0,0,.45); overflow: hidden; }
    .panel.collapsed .body, .panel.collapsed .footer { display: none; }
    header { display: flex; align-items: center; gap: 8px; padding: 10px 12px;
      background: #1b1e24; border-bottom: 1px solid #2b2f36; cursor: default; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: #c92a2a; flex: none; }
    .dot.on { background: #2f9e44; }
    .title { font-weight: 600; letter-spacing: .2px; }
    .domain { color: #868e96; font-size: 11px; margin-left: auto; max-width: 130px;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    button { font: inherit; color: #e9ecef; background: #2b3038; border: 1px solid #3a4048;
      border-radius: 7px; padding: 3px 9px; cursor: pointer; }
    button:hover { background: #363c45; }
    button:disabled { opacity: .5; cursor: default; }
    button.primary { background: #1c6dd0; border-color: #2b7fe0; }
    button.primary:hover { background: #2b7fe0; }
    button.icon { padding: 2px 7px; background: transparent; border-color: transparent; color: #adb5bd; }
    button.toggle.on { background: #1c6dd0; border-color: #2b7fe0; }
    .body { padding: 8px 12px; overflow-y: auto; }
    .row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #22262d; }
    .row:last-child { border-bottom: 0; }
    .name { font-weight: 500; }
    .meta { color: #868e96; font-size: 11px; margin-left: auto; text-align: right; max-width: 120px; }
    .footer { padding: 8px 12px; border-top: 1px solid #2b2f36; background: #1b1e24; }
    .actions { display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
    .status { color: #adb5bd; font-size: 11px; min-height: 15px; }
    .bar { height: 3px; background: #2b3038; border-radius: 2px; overflow: hidden; margin: 6px 0; }
    .bar > i { display: block; height: 100%; width: 0; background: #2b7fe0; transition: width .2s; }
    .log { max-height: 110px; overflow-y: auto; font-size: 11px; color: #868e96;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .log-line.error { color: #ff8787; }
    .log-line.warn { color: #ffd43b; }
    .empty { color: #868e96; padding: 6px 0; }
    .review { border-top: 1px solid #2b2f36; padding: 8px 12px; max-height: 240px; overflow-y: auto; }
    .review h4 { margin: 0 0 6px; font-size: 12px; font-weight: 600; color: #adb5bd; }
    .review-item { display: flex; gap: 6px; align-items: flex-start; padding: 4px 0;
      border-bottom: 1px solid #22262d; font-size: 12px; }
    .review-item:last-child { border-bottom: 0; }
    .review-item input { margin-top: 3px; }
    .review-text { flex: 1; overflow: hidden; }
    .review-id { color: #868e96; font-family: ui-monospace, Menlo, monospace; font-size: 10px; }
    .review-changes { color: #ced4da; }
    .review-actions { display: flex; gap: 6px; margin-top: 8px; }
    .badge-mode { font-size: 10px; color: #ffd43b; margin-left: 6px; }
  `;

  function buildPanel() {
    if (state.panel) return state.panel;
    const host = document.createElement('div');
    host.id = 'local-ai-hub-panel';
    host.style.all = 'initial';
    const shadow = host.attachShadow({ mode: 'open' });
    const style = document.createElement('style');
    style.textContent = PANEL_CSS;
    const panel = document.createElement('div');
    panel.className = 'panel';
    panel.innerHTML = `
      <header>
        <span class="dot" data-role="dot"></span>
        <span class="title">Local AI Hub</span>
        <span class="domain" data-role="domain"></span>
        <button class="icon" data-role="collapse" title="Collapse">–</button>
        <button class="icon" data-role="close" title="Hide (Alt+Shift+H)">×</button>
      </header>
      <div class="body" data-role="rows"></div>
      <div class="review" data-role="review" hidden></div>
      <div class="footer">
        <div class="actions">
          <button class="primary" data-role="run-all">Run all</button>
          <button data-role="cancel" disabled>Cancel</button>
          <button data-role="clear">Clear</button>
          <button data-role="settings">Settings</button>
        </div>
        <div class="actions">
          <button class="toggle" data-role="dry-run" title="Plan only, change nothing">Dry run</button>
          <button class="toggle" data-role="review" title="Approve each item before applying">Review</button>
          <button data-role="pick" title="Click an element to build a selector">Pick selector</button>
        </div>
        <div class="bar"><i data-role="bar"></i></div>
        <div class="status" data-role="status">idle</div>
        <div class="log" data-role="log"></div>
      </div>`;
    shadow.append(style, panel);
    (document.body || document.documentElement).appendChild(host);

    const ui = {
      host,
      panel,
      dot: shadow.querySelector('[data-role=dot]'),
      domain: shadow.querySelector('[data-role=domain]'),
      rows: shadow.querySelector('[data-role=rows]'),
      review: shadow.querySelector('[data-role=review]'),
      status: shadow.querySelector('[data-role=status]'),
      bar: shadow.querySelector('[data-role=bar]'),
      log: shadow.querySelector('[data-role=log]'),
      runAll: shadow.querySelector('[data-role=run-all]'),
      cancel: shadow.querySelector('[data-role=cancel]'),
      dryRun: shadow.querySelector('[data-role=dry-run]'),
      reviewToggle: shadow.querySelector('[data-role=review]'),
      pick: shadow.querySelector('[data-role=pick]'),
    };
    ui.domain.textContent = state.domain;
    shadow.querySelector('[data-role=collapse]').addEventListener('click', () => {
      state.collapsed = !state.collapsed;
      panel.classList.toggle('collapsed', state.collapsed);
    });
    shadow.querySelector('[data-role=close]').addEventListener('click', () => hidePanel());
    shadow.querySelector('[data-role=clear]').addEventListener('click', () => {
      LAHApply.clearAll(document);
      state.results = {};
      state.plans = {};
      renderRows();
      renderReview();
      setStatus('results cleared');
    });
    shadow.querySelector('[data-role=settings]').addEventListener('click', () => LAH.send(KIND.OPEN_OPTIONS));
    ui.runAll.addEventListener('click', () => runAll());
    ui.cancel.addEventListener('click', () => cancelRunning());
    ui.dryRun.addEventListener('click', () => {
      state.dryRun = !state.dryRun;
      syncToggles();
      setStatus(state.dryRun ? 'dry run: nothing will be changed' : 'dry run off');
    });
    ui.reviewToggle.addEventListener('click', () => {
      state.review = !state.review;
      syncToggles();
      setStatus(state.review ? 'review: approve items before applying' : 'review off');
    });
    ui.pick.addEventListener('click', () => startPicker());

    state.panel = host;
    state.ui = ui;
    renderRows();
    syncToggles();
    setConnected(state.connected);
    return host;
  }

  function hidePanel() {
    if (state.panel) state.panel.style.display = 'none';
  }

  function showPanel() {
    if (!state.panel) buildPanel();
    state.panel.style.display = '';
  }

  function setConnected(connected) {
    state.connected = connected;
    if (state.ui) state.ui.dot.classList.toggle('on', connected);
  }

  function setStatus(text) {
    if (state.ui) state.ui.status.textContent = text;
  }

  function setProgress(value) {
    if (state.ui) state.ui.bar.style.width = `${Math.round(Math.max(0, Math.min(1, value || 0)) * 100)}%`;
  }

  function syncToggles() {
    if (!state.ui) return;
    state.ui.dryRun.classList.toggle('on', state.dryRun);
    state.ui.reviewToggle.classList.toggle('on', state.review);
    state.ui.cancel.disabled = state.tasks.size === 0;
  }

  function renderRows() {
    if (!state.ui) return;
    const features = (state.config && state.config.features) || {};
    const ids = enabledFeatures();
    state.ui.rows.textContent = '';
    if (!ids.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'No features enabled for this site. Open Settings to add one.';
      state.ui.rows.appendChild(empty);
      return;
    }
    ids.forEach((id, index) => {
      const feature = features[id];
      const row = document.createElement('div');
      row.className = 'row';
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = id;
      if (index < 3) name.title = `Alt+Shift+${index + 1}`;
      const meta = document.createElement('span');
      meta.className = 'meta';
      meta.dataset.role = `meta-${id}`;
      const result = state.results[id];
      meta.textContent = result ? result.summary : feature.auto_run ? 'auto' : 'manual';
      const button = document.createElement('button');
      button.textContent = state.running.has(id) ? 'Running…' : 'Run';
      button.disabled = state.running.has(id);
      button.addEventListener('click', () => runFeature(id));
      row.append(name, meta, button);
      state.ui.rows.appendChild(row);
    });
    syncToggles();
  }

  // -------------------------------------------------------------------------
  // Review / dry-run preview
  // -------------------------------------------------------------------------
  function renderReview() {
    if (!state.ui) return;
    const box = state.ui.review;
    box.textContent = '';
    const pluginIds = Object.keys(state.plans);
    if (!pluginIds.length) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    pluginIds.forEach((pluginId) => {
      const entry = state.plans[pluginId];
      const heading = document.createElement('h4');
      heading.textContent = `${pluginId} — ${entry.plans.length} item(s)${entry.dryRun ? ' (dry run)' : ''}`;
      box.appendChild(heading);

      entry.plans.forEach((plan, index) => {
        const row = document.createElement('div');
        row.className = 'review-item';
        const check = document.createElement('input');
        check.type = 'checkbox';
        check.checked = plan.accepted !== false;
        check.disabled = entry.dryRun;
        check.addEventListener('change', () => {
          plan.accepted = check.checked;
        });
        const text = document.createElement('div');
        text.className = 'review-text';
        const id = document.createElement('div');
        id.className = 'review-id';
        id.textContent = plan.id;
        const changes = document.createElement('div');
        changes.className = 'review-changes';
        changes.textContent = plan.changes.length
          ? plan.changes.map((change) => LAHApply.describePlan(change)).join(' · ')
          : 'no change';
        text.append(id, changes);
        row.append(check, text);
        box.appendChild(row);
        if (index > 199) return;
      });

      const actions = document.createElement('div');
      actions.className = 'review-actions';
      if (!entry.dryRun) {
        const apply = document.createElement('button');
        apply.className = 'primary';
        apply.textContent = 'Apply selected';
        apply.addEventListener('click', () => {
          const accepted = entry.plans.filter((plan) => plan.accepted !== false);
          const report = LAHApply.applyPlans(accepted, entry.context);
          setStatus(`${pluginId}: applied ${report.applied} change(s) from ${accepted.length} item(s)`);
          log(`${pluginId}: review applied ${report.applied} change(s)`);
          delete state.plans[pluginId];
          renderReview();
        });
        actions.appendChild(apply);
      }
      const discard = document.createElement('button');
      discard.textContent = entry.dryRun ? 'Close preview' : 'Discard';
      discard.addEventListener('click', () => {
        delete state.plans[pluginId];
        renderReview();
      });
      actions.appendChild(discard);
      box.appendChild(actions);
    });
  }

  // -------------------------------------------------------------------------
  // Task execution
  // -------------------------------------------------------------------------
  function modeFor(feature) {
    if (state.dryRun || feature.dry_run) return 'dry';
    if (state.review || feature.review) return 'review';
    return 'apply';
  }

  function applyContext(pluginId, feature) {
    return {
      pluginId,
      allowTextReplace: Boolean(feature.allow_text_replace),
      allowFormFill: Boolean(feature.allow_form_fill),
    };
  }

  async function cancelRunning() {
    const ids = Array.from(state.tasks.keys());
    if (!ids.length) return;
    for (const id of ids) {
      try {
        await LAH.send(KIND.REQUEST, { request: LAH.controlRequest('task.cancel', { task_id: id }) });
      } catch (error) {
        log(`cancel failed: ${LAH.errorMessage(error)}`, 'error');
      }
    }
    setStatus(`cancellation requested for ${ids.length} task(s)`);
  }

  async function runFeature(pluginId) {
    const feature = featureConfig(pluginId);
    if (!feature || !feature.enabled) {
      log(`${pluginId} is not enabled for ${state.domain}`, 'error');
      return null;
    }
    if (state.running.has(pluginId)) return null;
    state.running.add(pluginId);
    renderRows();
    setStatus(`${pluginId}: extracting…`);
    setProgress(0.05);

    try {
      const { items, errors } = LAHExtract.extractItems(feature, pluginId, {
        maxItems: state.settings.maxItems,
      });
      (errors || []).forEach((message) => log(message, 'warn'));
      if (!items.length) {
        setStatus(`${pluginId}: no elements matched ${feature.item_selector || '(no selector)'}`);
        log(`${pluginId}: nothing matched`, 'warn');
        return null;
      }
      items.forEach((item) => state.items.set(item.id, item));
      log(`${pluginId}: ${items.length} item(s) extracted`);

      const images = await LAHExtract.resolveImages(items, state.settings);
      (images.errors || []).forEach((message) => log(message, 'warn'));
      if (images.imageCount) log(`${pluginId}: ${images.imageCount} image(s) fetched locally`);

      const input = LAHExtract.buildInput(feature, items, LAHExtract.pageFacts());
      const request = LAH.taskRequest(pluginId, input, feature.options || {}, {
        domain: state.domain,
        url: location.href,
        title: document.title,
        domain_config: { features: { [pluginId]: feature } },
      });
      state.tasks.set(request.id, pluginId);
      syncToggles();
      setStatus(`${pluginId}: running on ${items.length} item(s)…`);
      setProgress(0.15);

      const reply = await LAH.send(KIND.REQUEST, { request });
      state.tasks.delete(request.id);
      syncToggles();
      if (!reply || !reply.ok) {
        const message = (reply && (reply.error || (reply.response && reply.response.error && reply.response.error.message))) || 'task failed';
        setStatus(`${pluginId}: ${message}`);
        log(`${pluginId}: ${message}`, 'error');
        return null;
      }

      const response = reply.response;
      const itemsById = new Map(items.map((item) => [item.id, item]));
      const mode = modeFor(feature);
      const context = applyContext(pluginId, feature);
      let summary;

      if (mode === 'apply') {
        const applied = LAHApply.applyResults(response.output, itemsById, feature.apply, context);
        summary = `${applied.matched}/${items.length} · ${response.meta.duration_ms} ms`;
        log(`${pluginId}: ${applied.applied} action(s) applied, models: ${(response.meta.models || []).join(', ') || 'none'}`);
      } else {
        const planned = LAHApply.planResults(response.output, itemsById, feature.apply, context);
        state.plans[pluginId] = {
          plans: planned.plans,
          context,
          dryRun: mode === 'dry',
        };
        renderReview();
        const changes = planned.plans.reduce((total, plan) => total + plan.changes.length, 0);
        summary = `${planned.matched}/${items.length} · ${changes} planned`;
        log(`${pluginId}: ${mode === 'dry' ? 'dry run' : 'review'} — ${changes} change(s) planned, nothing applied`);
      }

      state.results[pluginId] = { summary, output: response.output, meta: response.meta };
      setStatus(`${pluginId}: ${summary}${response.meta.mock_model ? ' (mock model)' : ''}${mode !== 'apply' ? ` · ${mode}` : ''}`);
      setProgress(1);
      return response;
    } catch (error) {
      const message = LAH.errorMessage(error);
      setStatus(`${pluginId}: ${message}`);
      log(`${pluginId}: ${message}`, 'error');
      return null;
    } finally {
      state.running.delete(pluginId);
      renderRows();
      setTimeout(() => setProgress(0), 1200);
    }
  }

  async function runAll() {
    const ids = enabledFeatures();
    if (!ids.length) {
      showPanel();
      setStatus('no features enabled for this site');
      return;
    }
    for (const id of ids) {
      await runFeature(id);
    }
    setStatus(`finished ${ids.length} feature(s)${state.dryRun ? ' (dry run)' : ''}`);
  }

  // -------------------------------------------------------------------------
  // Userscript bridge (opt-in per domain, input data only)
  // -------------------------------------------------------------------------
  function bridgePolicy() {
    return LAHBridge.policy(state.config);
  }

  async function bridgeSubmit(req) {
    const feature = featureConfig(req.plugin) || {};
    const request = LAH.taskRequest(req.plugin, req.input, {}, {
      domain: state.domain,
      url: location.href,
      title: document.title,
      interface: 'tampermonkey',
      // deliberately no options and no domain_config: the page supplies data only
    });
    state.tasks.set(request.id, req.plugin);
    syncToggles();
    showPanel();
    setStatus(`${req.plugin}: userscript request…`);
    try {
      const reply = await LAH.send(KIND.REQUEST, { request });
      if (!reply || !reply.ok) {
        const error = (reply && reply.response && reply.response.error) || {
          code: 'plugin_failed',
          message: (reply && reply.error) || 'task failed',
        };
        return { ok: false, error };
      }
      const response = reply.response;
      const answer = { ok: true, output: response.output, meta: response.meta };
      if (req.apply && feature.apply) {
        // Apply with the *user's* configured actions, matched by data-lah-id.
        const itemsById = new Map();
        document.querySelectorAll('[data-lah-id]').forEach((element) => {
          itemsById.set(element.getAttribute('data-lah-id'), { id: element.getAttribute('data-lah-id'), element });
        });
        const applied = LAHApply.applyResults(response.output, itemsById, feature.apply, applyContext(req.plugin, feature));
        answer.applied = { applied: applied.applied, matched: applied.matched, skipped: applied.skipped };
      }
      state.results[req.plugin] = {
        summary: `bridge · ${response.meta.duration_ms} ms`,
        output: response.output,
        meta: response.meta,
      };
      renderRows();
      return answer;
    } catch (error) {
      return { ok: false, error: { code: 'internal_error', message: LAH.errorMessage(error) } };
    } finally {
      state.tasks.delete(request.id);
      syncToggles();
    }
  }

  function attachBridge() {
    if (state.bridge || typeof LAHBridge === 'undefined') return;
    state.bridge = LAHBridge.attach({
      getPolicy: bridgePolicy,
      submit: bridgeSubmit,
      log,
    });
    const pol = bridgePolicy();
    if (pol.enabled) log(`userscript bridge active (${(pol.plugins || []).join(', ') || 'no plugins'})`);
  }

  // -------------------------------------------------------------------------
  // "Send selection to Local AI Hub" (context menu)
  // -------------------------------------------------------------------------
  async function runSelection(text) {
    const selection = (state.config && state.config.selection) || {};
    const pluginId = selection.plugin || 'page.summarize';
    const feature = featureConfig(pluginId);
    if (feature && feature.enabled === false) {
      log(`${pluginId} is disabled for ${state.domain}`, 'error');
      return;
    }
    const inputKey = selection.input_key || 'text';
    const input = { [inputKey]: String(text || '').slice(0, 20000) };
    if (selection.extra_input && typeof selection.extra_input === 'object') {
      Object.assign(input, selection.extra_input);
    }
    input.page_title = document.title;
    const request = LAH.taskRequest(pluginId, input, (feature && feature.options) || selection.options || {}, {
      domain: state.domain,
      url: location.href,
      title: document.title,
    });
    showPanel();
    state.tasks.set(request.id, pluginId);
    syncToggles();
    setStatus(`${pluginId}: summarising selection…`);
    try {
      const reply = await LAH.send(KIND.REQUEST, { request });
      if (!reply || !reply.ok) {
        const message = (reply && (reply.error || (reply.response && reply.response.error && reply.response.error.message))) || 'task failed';
        setStatus(`${pluginId}: ${message}`);
        log(`${pluginId}: ${message}`, 'error');
        return;
      }
      const output = reply.response.output || {};
      const textOut = output.summary || output.text || JSON.stringify(output).slice(0, 400);
      state.results[pluginId] = { summary: `selection · ${reply.response.meta.duration_ms} ms`, output, meta: reply.response.meta };
      log(`${pluginId}: ${String(textOut).slice(0, 500)}`);
      setStatus(`${pluginId}: selection processed (${reply.response.meta.duration_ms} ms)`);
      renderRows();
    } finally {
      state.tasks.delete(request.id);
      syncToggles();
    }
  }

  // -------------------------------------------------------------------------
  // Selector picker: click an element, get a config-ready selector
  // -------------------------------------------------------------------------
  function cssEscape(value) {
    return String(value).replace(/[^A-Za-z0-9_-]/g, '\\$&');
  }

  function selectorFor(element) {
    if (element.id) return `#${cssEscape(element.id)}`;
    const tag = element.tagName.toLowerCase();
    const classes = Array.from(element.classList || [])
      .filter((name) => /^[A-Za-z0-9_-]+$/.test(name) && !name.startsWith('lah-'))
      .slice(0, 2);
    let selector = tag + (classes.length ? `.${classes.join('.')}` : '');
    const parent = element.parentElement;
    if (parent) {
      const siblings = Array.from(parent.children).filter((child) => child.tagName === element.tagName);
      if (siblings.length > 1 && !classes.length) {
        selector += `:nth-of-type(${siblings.indexOf(element) + 1})`;
      }
    }
    return selector;
  }

  function startPicker() {
    if (state.picker) return;
    showPanel();
    setStatus('picker: click an element (Esc to cancel)');
    let hovered = null;
    let previousOutline = '';

    function highlight(element) {
      if (hovered === element) return;
      if (hovered) hovered.style.outline = previousOutline;
      hovered = element;
      if (element) {
        previousOutline = element.style.outline;
        element.style.outline = '2px solid #2b7fe0';
      }
    }

    function onMove(event) {
      highlight(event.target);
    }

    function cleanup() {
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('click', onClick, true);
      document.removeEventListener('keydown', onKey, true);
      highlight(null);
      state.picker = null;
      if (state.ui) state.ui.pick.classList.remove('on');
    }

    function onKey(event) {
      if (event.key === 'Escape') {
        cleanup();
        setStatus('picker cancelled');
      }
    }

    function onClick(event) {
      const element = event.target;
      event.preventDefault();
      event.stopPropagation();
      const itemSelector = selectorFor(element);
      let matches = 0;
      try {
        matches = document.querySelectorAll(itemSelector).length;
      } catch (_) {
        matches = 0;
      }
      const parent = element.parentElement;
      const result = {
        domain: state.domain,
        url: location.href,
        item_selector: itemSelector,
        root_selector: parent ? selectorFor(parent) : '',
        matches,
        sample: (element.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200),
        fields: { text: { attr: 'text' } },
        at: Date.now(),
      };
      cleanup();
      setStatus(`picker: ${itemSelector} (${matches} match${matches === 1 ? '' : 'es'}) — sent to Settings`);
      log(`picked ${itemSelector} (${matches} matches)`);
      LAH.send(KIND.PICK_RESULT, { result }).catch(() => {});
    }

    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
    state.picker = { cleanup };
    if (state.ui) state.ui.pick.classList.add('on');
  }

  // -------------------------------------------------------------------------
  // Messages from the background page
  // -------------------------------------------------------------------------
  browser.runtime.onMessage.addListener((message) => {
    if (!message || !message.kind) return;
    if (message.kind === KIND.COMMAND) {
      if (message.command === 'lah-run-all') {
        showPanel();
        runAll();
      } else if (message.command === 'lah-toggle-panel') {
        if (!state.panel || state.panel.style.display === 'none') showPanel();
        else hidePanel();
      } else if (message.command === 'lah-dry-run') {
        showPanel();
        state.dryRun = !state.dryRun;
        syncToggles();
        setStatus(state.dryRun ? 'dry run: nothing will be changed' : 'dry run off');
      } else if (message.command === 'lah-pick-selector') {
        startPicker();
      } else if (message.command === 'lah-selection') {
        runSelection(message.text);
      } else if (/^lah-run-feature-[123]$/.test(message.command || '')) {
        const index = Number(message.command.slice(-1)) - 1;
        const ids = enabledFeatures();
        if (ids[index]) {
          showPanel();
          runFeature(ids[index]);
        } else {
          showPanel();
          setStatus(`no feature #${index + 1} on this site`);
        }
      }
    } else if (message.kind === KIND.STATUS) {
      setConnected(Boolean(message.status && message.status.connected));
    } else if (message.kind === KIND.EVENT) {
      if (message.event === 'host.ready') setConnected(true);
      const pluginId = state.tasks.get(message.id);
      if (pluginId && message.event === 'progress') {
        const value = Number((message.data || {}).value) || 0;
        setProgress(0.15 + value * 0.8);
        setStatus(`${pluginId}: ${(message.data || {}).message || 'working…'}`);
      } else if (pluginId && message.event === 'plugin.log') {
        log(`${pluginId}: ${(message.data || {}).message || ''}`, (message.data || {}).level);
      } else if (pluginId && message.event === 'task.cancelling') {
        setStatus(`${pluginId}: cancelling…`);
      } else if (String(message.event).startsWith('model.')) {
        log(`${message.event} ${JSON.stringify(message.data || {})}`);
      }
    }
  });

  // -------------------------------------------------------------------------
  async function init() {
    if (!document.body) return;
    try {
      state.settings = Object.assign({}, LAH.DEFAULT_SETTINGS, (await LAH.send(KIND.SETTINGS)) || {});
      state.config = (await LAH.send(KIND.DOMAIN_CONFIG, { domain: state.domain })) || { features: {} };
      const status = await LAH.send(KIND.STATUS, {});
      setConnected(Boolean(status && status.connected));
    } catch (error) {
      log(`init failed: ${LAH.errorMessage(error)}`, 'error');
      return;
    }

    state.dryRun = Boolean(state.settings.dryRun);
    state.review = Boolean(state.settings.review);

    const ids = enabledFeatures();
    if ((ids.length || bridgePolicy().enabled) && state.settings.showPanel) buildPanel();
    attachBridge();
    if (state.settings.autoRun) {
      ids.filter((id) => state.config.features[id].auto_run).forEach((id) => runFeature(id));
    }
    if (ids.length) log(`ready: ${ids.join(', ')}`);
  }

  init();
})();
