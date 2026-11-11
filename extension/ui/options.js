'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  domains: {},
  hostDomains: [],
  plugins: [],
  current: null,
  settings: {},
};

// ---------------------------------------------------------------------------
// tabs
// ---------------------------------------------------------------------------
document.querySelectorAll('.tabs button').forEach((button) => {
  button.addEventListener('click', () => {
    document.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('active', b === button));
    ['domains', 'plugins', 'hub', 'diagnostics'].forEach((name) => {
      $(`tab-${name}`).classList.toggle('hidden', name !== button.dataset.tab);
    });
    if (button.dataset.tab === 'plugins') loadPlugins();
    if (button.dataset.tab === 'diagnostics') refreshDiagnostics();
  });
});

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function featureTemplate(pluginId) {
  const base = {
    enabled: true,
    auto_run: false,
    root_selector: '',
    item_selector: '',
    fields: { text: { attr: 'text' } },
    input: { items: '@items' },
    options: {},
    apply: {
      results_path: 'results',
      match_by: 'id',
      actions: [{ action: 'badge', text: '{label}' }],
    },
  };
  if (pluginId === 'comments.classify') {
    base.item_selector = 'p.comment';
    base.root_selector = 'div.comments';
    base.fields = { text: { attr: 'text' }, author: { selector: '.author', attr: 'text' } };
    base.input = { comments: '@items', page_title: '@page.title' };
    base.options = { labels: ['ok', 'spam', 'toxic', 'off-topic'], batch_size: 8 };
    base.apply.actions = [
      { action: 'badge', text: '{label} {confidence}' },
      { action: 'attribute', name: 'data-lah-label', value: '{label}' },
      { action: 'class', map: { spam: 'lah-flag-spam', toxic: 'lah-flag-toxic' } },
    ];
  } else if (pluginId === 'images.tag') {
    base.item_selector = 'img';
    base.fields = {
      data_base64: { attr: 'src', as: 'image', max_size: 768, quality: 0.85 },
      alt: { attr: 'alt' },
    };
    base.input = { images: '@items' };
    base.options = { max_tags: 8 };
    base.apply.actions = [
      { action: 'badge', text: '{tags}' },
      { action: 'attribute', name: 'title', value: '{description}' },
    ];
  } else if (pluginId.startsWith('hello.')) {
    base.item_selector = 'h1';
    base.fields = { name: { attr: 'text' } };
    base.input = { name: 'world' };
    base.apply.actions = [{ action: 'badge', text: '{greeting}' }];
  }
  return base;
}

async function loadStatus() {
  const status = await LAH.send(LAH.KIND.STATUS, { force: true });
  const connected = Boolean(status && status.connected);
  $('dot').classList.toggle('on', connected);
  const host = (status && status.hostStatus) || {};
  $('subtitle').textContent = connected ? 'connected to local host' : `disconnected — ${(status && status.lastError) || 'host not running'}`;
  $('version').textContent = host.version ? `v${host.version}` : '';
  return status;
}

// ---------------------------------------------------------------------------
// domains
// ---------------------------------------------------------------------------
function renderDomainList() {
  const names = new Set([...state.hostDomains.map((d) => d.domain), ...Object.keys(state.domains)]);
  const list = $('domain-list');
  list.textContent = '';
  if (!names.size) {
    const empty = document.createElement('div');
    empty.className = 'item muted';
    empty.textContent = 'No domains yet — add one below.';
    list.appendChild(empty);
  }
  Array.from(names)
    .sort()
    .forEach((domain) => {
      const item = document.createElement('div');
      item.className = `item${state.current === domain ? ' active' : ''}`;
      const label = document.createElement('span');
      label.textContent = domain;
      const tag = document.createElement('span');
      tag.className = 'tag';
      const hostEntry = state.hostDomains.find((entry) => entry.domain === domain);
      tag.textContent = [hostEntry ? 'file' : null, state.domains[domain] ? 'override' : null]
        .filter(Boolean)
        .join(' + ');
      item.append(label, tag);
      item.addEventListener('click', () => selectDomain(domain));
      list.appendChild(item);
    });
}

async function loadDomains() {
  const stored = await LAH.send(LAH.KIND.DOMAIN_OVERRIDES, {});
  state.domains = (stored && stored.domains) || {};
  const reply = await LAH.send(LAH.KIND.REQUEST, { request: LAH.controlRequest('domains.list') });
  state.hostDomains = reply && reply.ok ? reply.response.output.domains || [] : [];
  renderDomainList();
  if (!state.current && state.hostDomains.length) selectDomain(state.hostDomains[0].domain);
}

async function selectDomain(domain) {
  state.current = domain;
  $('current-domain').textContent = domain;
  renderDomainList();
  const override = state.domains[domain];
  $('domain-editor').value = JSON.stringify(override || { features: {} }, null, 2);
  $('editor-status').textContent = override
    ? 'Editing the Firefox-side override.'
    : 'No override yet — saving creates one on top of the host file config.';
  const config = await LAH.send(LAH.KIND.DOMAIN_CONFIG, { domain });
  $('effective').textContent = JSON.stringify(config, null, 2);
}

$('add-domain').addEventListener('click', async () => {
  const raw = $('new-domain').value.trim().toLowerCase();
  if (!raw) return;
  const domain = raw.replace(/^https?:\/\//, '').split('/')[0];
  $('new-domain').value = '';
  await selectDomain(domain);
});

$('save-domain').addEventListener('click', async () => {
  if (!state.current) return;
  let parsed;
  try {
    parsed = JSON.parse($('domain-editor').value || '{}');
  } catch (error) {
    $('editor-status').textContent = `Invalid JSON: ${error.message}`;
    return;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    $('editor-status').textContent = 'Override must be a JSON object.';
    return;
  }
  await LAH.send(LAH.KIND.SET_DOMAIN_OVERRIDES, { domain: state.current, overrides: parsed });
  state.domains[state.current] = parsed;
  $('editor-status').textContent = 'Saved. Reload the target tab to use it.';
  await selectDomain(state.current);
});

$('validate').addEventListener('click', () => {
  try {
    const parsed = JSON.parse($('domain-editor').value || '{}');
    const features = parsed.features || {};
    const problems = [];
    Object.keys(features).forEach((id) => {
      const feature = features[id] || {};
      if (!state.plugins.some((plugin) => plugin.id === id)) problems.push(`${id}: unknown plugin (install it on the host)`);
      if (!feature.item_selector) problems.push(`${id}: missing item_selector`);
      (feature.apply && feature.apply.actions ? feature.apply.actions : []).forEach((action) => {
        if (!LAHApplyAllowed.includes(action.action)) problems.push(`${id}: unsupported action ${action.action}`);
      });
    });
    $('editor-status').textContent = problems.length ? problems.join(' · ') : 'Looks valid.';
  } catch (error) {
    $('editor-status').textContent = `Invalid JSON: ${error.message}`;
  }
});

const LAHApplyAllowed = ['badge', 'class', 'attribute', 'hide', 'show', 'text'];

$('reload-domain').addEventListener('click', () => state.current && selectDomain(state.current));

$('delete-domain').addEventListener('click', async () => {
  if (!state.current) return;
  await LAH.send(LAH.KIND.SET_DOMAIN_OVERRIDES, { domain: state.current, overrides: null });
  delete state.domains[state.current];
  await selectDomain(state.current);
});

$('insert-template').addEventListener('click', () => {
  const pluginId = $('template-plugin').value;
  if (!pluginId || !state.current) return;
  let parsed = {};
  try {
    parsed = JSON.parse($('domain-editor').value || '{}');
  } catch (_) {
    parsed = {};
  }
  parsed.features = parsed.features || {};
  parsed.features[pluginId] = featureTemplate(pluginId);
  $('domain-editor').value = JSON.stringify(parsed, null, 2);
  $('editor-status').textContent = `Template for ${pluginId} inserted — adjust the selectors, then Save.`;
});

$('export-config').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify({ domains: state.domains, settings: state.settings }, null, 2)], {
    type: 'application/json',
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'local-ai-hub-config.json';
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
});

$('import-config').addEventListener('click', () => $('import-file').click());
$('import-file').addEventListener('change', async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const text = await file.text();
  try {
    const parsed = JSON.parse(text);
    const domains = parsed.domains || {};
    for (const domain of Object.keys(domains)) {
      await LAH.send(LAH.KIND.SET_DOMAIN_OVERRIDES, { domain, overrides: domains[domain] });
    }
    if (parsed.settings) {
      await LAH.send(LAH.KIND.SET_SETTINGS, { settings: parsed.settings });
      await loadSettings();
    }
    await loadDomains();
    $('editor-status').textContent = `Imported ${Object.keys(domains).length} domain override(s).`;
  } catch (error) {
    $('editor-status').textContent = `Import failed: ${error.message}`;
  }
  event.target.value = '';
});

// ---------------------------------------------------------------------------
// plugins
// ---------------------------------------------------------------------------
function sampleInput(plugin) {
  const schema = plugin.input_schema || {};
  const properties = schema.properties || {};
  const sample = {};
  Object.keys(properties).forEach((key) => {
    const spec = properties[key];
    if (spec.default !== undefined) sample[key] = spec.default;
    else if (spec.type === 'array') {
      if (key === 'comments') sample[key] = [{ id: 'c1', text: 'Great post, thanks!' }];
      else if (key === 'images') sample[key] = [{ id: 'i1', alt: 'no pixels in a CLI test' }];
      else sample[key] = [];
    } else if (spec.type === 'string') sample[key] = key === 'name' ? 'Local AI Hub' : '';
    else if (spec.type === 'boolean') sample[key] = false;
  });
  return sample;
}

async function loadPlugins() {
  const reply = await LAH.send(LAH.KIND.REQUEST, { request: LAH.controlRequest('plugins.list') });
  const rows = $('plugin-rows');
  rows.textContent = '';
  if (!reply || !reply.ok) {
    const row = document.createElement('tr');
    const cell = document.createElement('td');
    cell.colSpan = 5;
    cell.className = 'muted';
    cell.textContent = `Host unavailable: ${(reply && (reply.error || (reply.response.error || {}).message)) || 'not connected'}`;
    row.appendChild(cell);
    rows.appendChild(row);
    return;
  }
  state.plugins = reply.response.output.plugins || [];
  const select = $('template-plugin');
  select.textContent = '';
  state.plugins.forEach((plugin) => {
    const option = document.createElement('option');
    option.value = plugin.id;
    option.textContent = plugin.id;
    select.appendChild(option);

    const row = document.createElement('tr');
    [plugin.id, plugin.runtime, plugin.version, plugin.description].forEach((value, index) => {
      const cell = document.createElement('td');
      cell.textContent = value || '';
      if (index === 0) cell.className = 'mono';
      row.appendChild(cell);
    });
    const cell = document.createElement('td');
    const button = document.createElement('button');
    button.textContent = 'Test run';
    button.addEventListener('click', async () => {
      button.disabled = true;
      const request = LAH.taskRequest(plugin.id, sampleInput(plugin), {}, { interface: 'options' });
      const result = await LAH.send(LAH.KIND.REQUEST, { request });
      $('plugin-output').classList.remove('hidden');
      $('plugin-output').textContent = JSON.stringify(result.response || result, null, 2);
      button.disabled = false;
    });
    cell.appendChild(button);
    row.appendChild(cell);
    rows.appendChild(row);
  });
}

$('reload-plugins').addEventListener('click', loadPlugins);

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------
async function loadSettings() {
  state.settings = (await LAH.send(LAH.KIND.SETTINGS, {})) || {};
  $('set-autoConnect').checked = Boolean(state.settings.autoConnect);
  $('set-showPanel').checked = Boolean(state.settings.showPanel);
  $('set-autoRun').checked = Boolean(state.settings.autoRun);
  $('set-debug').checked = Boolean(state.settings.debug);
  $('set-requestTimeout').value = Math.round((state.settings.requestTimeoutMs || 240000) / 1000);
  $('set-maxItems').value = state.settings.maxItems || 200;
  $('set-imageMaxSize').value = state.settings.imageMaxSize || 768;
  $('set-imageQuality').value = state.settings.imageQuality || 0.85;
}

$('save-settings').addEventListener('click', async () => {
  const settings = {
    autoConnect: $('set-autoConnect').checked,
    showPanel: $('set-showPanel').checked,
    autoRun: $('set-autoRun').checked,
    debug: $('set-debug').checked,
    requestTimeoutMs: Math.max(10, Number($('set-requestTimeout').value) || 240) * 1000,
    maxItems: Math.max(1, Number($('set-maxItems').value) || 200),
    imageMaxSize: Math.max(128, Number($('set-imageMaxSize').value) || 768),
    imageQuality: Math.min(1, Math.max(0.3, Number($('set-imageQuality').value) || 0.85)),
  };
  state.settings = await LAH.send(LAH.KIND.SET_SETTINGS, { settings });
  $('settings-status').textContent = 'Saved.';
  setTimeout(() => ($('settings-status').textContent = ''), 2500);
});

$('unload-models').addEventListener('click', async () => {
  const reply = await LAH.send(LAH.KIND.REQUEST, { request: LAH.controlRequest('models.unload') });
  $('hub-config').classList.remove('hidden');
  $('hub-config').textContent = JSON.stringify(reply.response || reply, null, 2);
});

$('show-config').addEventListener('click', async () => {
  const reply = await LAH.send(LAH.KIND.REQUEST, { request: LAH.controlRequest('config.get') });
  $('hub-config').classList.remove('hidden');
  $('hub-config').textContent = JSON.stringify(reply.response || reply, null, 2);
});

// ---------------------------------------------------------------------------
// diagnostics
// ---------------------------------------------------------------------------
async function refreshDiagnostics() {
  const status = await loadStatus();
  $('diag-status').textContent = JSON.stringify(status, null, 2);
  $('diag-log').textContent = ((status && status.log) || [])
    .slice()
    .reverse()
    .map((entry) => `${new Date(entry.at).toLocaleTimeString()} ${entry.level} ${entry.message}${entry.extra ? ` ${JSON.stringify(entry.extra)}` : ''}`)
    .join('\n');
}

$('diag-refresh').addEventListener('click', refreshDiagnostics);
$('diag-ping').addEventListener('click', async () => {
  const reply = await LAH.send(LAH.KIND.REQUEST, { request: LAH.controlRequest('ping') });
  $('diag-status').textContent = JSON.stringify(reply.response || reply, null, 2);
  loadStatus();
});
$('diag-copy').addEventListener('click', async () => {
  const text = `LOCAL AI HUB DIAGNOSTICS\n\n${$('diag-status').textContent}\n\nLOG\n${$('diag-log').textContent}`;
  try {
    await navigator.clipboard.writeText(text);
    $('diag-copy').textContent = 'Copied ✓';
    setTimeout(() => ($('diag-copy').textContent = 'Copy diagnostics'), 2000);
  } catch (_) {
    /* clipboard blocked */
  }
});

browser.runtime.onMessage.addListener((message) => {
  if (!message || message.kind !== LAH.KIND.EVENT) return;
  const line = `${new Date().toLocaleTimeString()} ${message.event} ${JSON.stringify(message.data || {})}`;
  $('diag-log').textContent = `${line}\n${$('diag-log').textContent}`.slice(0, 8000);
});

// ---------------------------------------------------------------------------
(async function init() {
  await loadStatus();
  await loadSettings();
  await loadPlugins();
  await loadDomains();
})();
