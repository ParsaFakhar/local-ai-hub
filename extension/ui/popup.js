'use strict';

const $ = (id) => document.getElementById(id);

function show(value) {
  const box = $('output');
  box.classList.remove('hidden');
  box.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

async function activeTab() {
  const tabs = await browser.tabs.query({ active: true, currentWindow: true });
  return tabs[0] || null;
}

async function refresh() {
  const status = await LAH.send(LAH.KIND.STATUS, { force: true });
  const host = (status && status.hostStatus) || {};
  const models = host.models || {};
  const connected = Boolean(status && status.connected);

  $('dot').classList.toggle('on', connected);
  $('subtitle').textContent = connected
    ? `connected · v${(host.version || (status.hostInfo || {}).version || '?')}`
    : 'disconnected';
  $('offline').classList.toggle('hidden', connected);
  $('offline-reason').textContent = connected ? '' : (status && status.lastError) || '';

  $('host').textContent = connected ? `pid ok · protocol ${host.protocol || LAH.PROTOCOL}` : 'offline';
  $('ollama').textContent = models.backend === 'mock'
    ? 'mock backend (offline test mode)'
    : models.running
      ? `running${models.started_by_hub ? ' (started by hub)' : ''}`
      : 'stopped (starts on demand)';
  $('model').textContent = models.default_model || '—';
  $('loaded').textContent = (models.loaded_models || []).join(', ') || 'none';
  $('plugins').textContent = host.plugins ? `${host.plugins.count} registered` : '—';

  const tab = await activeTab();
  const domain = tab ? LAH.domainFromUrl(tab.url || '') : '';
  if (domain) {
    const config = await LAH.send(LAH.KIND.DOMAIN_CONFIG, { domain });
    const enabled = Object.keys((config && config.features) || {}).filter(
      (id) => config.features[id] && config.features[id].enabled
    );
    $('site').textContent = `${domain} · ${enabled.length ? enabled.join(', ') : 'no features'}`;
  } else {
    $('site').textContent = '—';
  }
  $('run').disabled = !connected;
}

$('run').addEventListener('click', async () => {
  const tab = await activeTab();
  if (!tab) return;
  try {
    await browser.tabs.sendMessage(tab.id, { kind: LAH.KIND.COMMAND, command: 'lah-run-all' });
    show('Task started — watch the panel on the page.');
  } catch (error) {
    show(`Cannot reach the page script: ${LAH.errorMessage(error)}\nReload the tab and try again.`);
  }
});

$('ping').addEventListener('click', async () => {
  const reply = await LAH.send(LAH.KIND.REQUEST, { request: LAH.controlRequest('ping') });
  show(reply.response || reply);
  refresh();
});

$('unload').addEventListener('click', async () => {
  const reply = await LAH.send(LAH.KIND.REQUEST, { request: LAH.controlRequest('models.unload') });
  show(reply.response || reply);
  refresh();
});

$('refresh').addEventListener('click', () => {
  refresh();
  show('refreshed');
});

$('settings').addEventListener('click', () => LAH.send(LAH.KIND.OPEN_OPTIONS));

browser.runtime.onMessage.addListener((message) => {
  if (message && message.kind === LAH.KIND.EVENT && String(message.event).startsWith('model.')) refresh();
});

refresh();
