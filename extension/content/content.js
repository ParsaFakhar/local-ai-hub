/**
 * Local AI Hub — content orchestrator.
 *
 * Reads the domain configuration, extracts inputs with the generic
 * selector engine, submits universal tasks through the background page
 * and applies the results with the allowlisted applier.
 *
 * The page itself gets no API and no privileged channel: everything is
 * driven by user configuration and user actions.
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
    panel: null,
    ui: null,
    collapsed: false,
  };

  // -------------------------------------------------------------------------
  function enabledFeatures() {
    const features = (state.config && state.config.features) || {};
    return Object.keys(features).filter((id) => features[id] && features[id].enabled);
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
    .panel { position: fixed; right: 16px; bottom: 16px; width: 330px; max-height: 70vh;
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
    .body { padding: 8px 12px; overflow-y: auto; }
    .row { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #22262d; }
    .row:last-child { border-bottom: 0; }
    .name { font-weight: 500; }
    .meta { color: #868e96; font-size: 11px; margin-left: auto; text-align: right; max-width: 120px; }
    .footer { padding: 8px 12px; border-top: 1px solid #2b2f36; background: #1b1e24; }
    .actions { display: flex; gap: 6px; margin-bottom: 6px; }
    .status { color: #adb5bd; font-size: 11px; min-height: 15px; }
    .bar { height: 3px; background: #2b3038; border-radius: 2px; overflow: hidden; margin: 6px 0; }
    .bar > i { display: block; height: 100%; width: 0; background: #2b7fe0; transition: width .2s; }
    .log { max-height: 110px; overflow-y: auto; font-size: 11px; color: #868e96;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
    .log-line.error { color: #ff8787; }
    .log-line.warn { color: #ffd43b; }
    .empty { color: #868e96; padding: 6px 0; }
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
      <div class="footer">
        <div class="actions">
          <button class="primary" data-role="run-all">Run all</button>
          <button data-role="clear">Clear</button>
          <button data-role="settings">Settings</button>
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
      status: shadow.querySelector('[data-role=status]'),
      bar: shadow.querySelector('[data-role=bar]'),
      log: shadow.querySelector('[data-role=log]'),
      runAll: shadow.querySelector('[data-role=run-all]'),
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
      renderRows();
      setStatus('results cleared');
    });
    shadow.querySelector('[data-role=settings]').addEventListener('click', () => LAH.send(KIND.OPEN_OPTIONS));
    ui.runAll.addEventListener('click', () => runAll());

    state.panel = host;
    state.ui = ui;
    renderRows();
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
    ids.forEach((id) => {
      const feature = features[id];
      const row = document.createElement('div');
      row.className = 'row';
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = id;
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
  }

  // -------------------------------------------------------------------------
  // Task execution
  // -------------------------------------------------------------------------
  async function runFeature(pluginId) {
    const feature = ((state.config || {}).features || {})[pluginId];
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
      setStatus(`${pluginId}: running on ${items.length} item(s)…`);
      setProgress(0.15);

      const reply = await LAH.send(KIND.REQUEST, { request });
      state.tasks.delete(request.id);
      if (!reply || !reply.ok) {
        const message = (reply && (reply.error || (reply.response && reply.response.error && reply.response.error.message))) || 'task failed';
        setStatus(`${pluginId}: ${message}`);
        log(`${pluginId}: ${message}`, 'error');
        return null;
      }

      const response = reply.response;
      const itemsById = new Map(items.map((item) => [item.id, item]));
      const applied = LAHApply.applyResults(response.output, itemsById, feature.apply, {
        pluginId,
        allowTextReplace: Boolean(feature.allow_text_replace),
      });
      const summary = `${applied.matched}/${items.length} · ${response.meta.duration_ms} ms`;
      state.results[pluginId] = { summary, output: response.output, meta: response.meta };
      setStatus(`${pluginId}: ${summary}${response.meta.mock_model ? ' (mock model)' : ''}`);
      log(`${pluginId}: ${applied.applied} action(s) applied, models: ${(response.meta.models || []).join(', ') || 'none'}`);
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
    setStatus(`finished ${ids.length} feature(s)`);
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

    const ids = enabledFeatures();
    if (ids.length && state.settings.showPanel) buildPanel();
    if (state.settings.autoRun) {
      ids.filter((id) => state.config.features[id].auto_run).forEach((id) => runFeature(id));
    }
    if (ids.length) log(`ready: ${ids.join(', ')}`);
  }

  init();
})();
