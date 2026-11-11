/**
 * Local AI Hub — background controller.
 *
 * Owns the single native messaging port to the local host, the request /
 * response correlation, per-domain settings and image fetching.
 * Content scripts and UI pages talk to this file, never to the host directly.
 */
'use strict';

const HOST_NAME = LAH.HOST_NAME;
const KIND = LAH.KIND;

const state = {
  port: null,
  connected: false,
  connecting: false,
  lastError: null,
  lastDisconnect: null,
  hostInfo: null,
  hostStatus: null,
  hostStatusAt: 0,
  retryDelay: 1000,
  retryTimer: null,
  pending: new Map(),
  settings: Object.assign({}, LAH.DEFAULT_SETTINGS),
  domains: {},
  log: [],
};

// ---------------------------------------------------------------------------
// logging
// ---------------------------------------------------------------------------
function logLine(level, message, extra) {
  const entry = { at: Date.now(), level, message, extra: extra || null };
  state.log.push(entry);
  if (state.log.length > 200) state.log.shift();
  if (state.settings.debug || level === 'error') {
    console[level === 'error' ? 'error' : 'log'](`[LocalAIHub] ${message}`, extra || '');
  }
  broadcast({ kind: KIND.LOG, entry });
}

function broadcast(message) {
  browser.runtime.sendMessage(message).catch(() => {});
  browser.tabs.query({}).then((tabs) => {
    tabs.forEach((tab) => {
      if (tab.id != null) browser.tabs.sendMessage(tab.id, message).catch(() => {});
    });
  }).catch(() => {});
}

// ---------------------------------------------------------------------------
// storage
// ---------------------------------------------------------------------------
async function loadSettings() {
  const stored = await browser.storage.local.get(['settings', 'domains']);
  state.settings = Object.assign({}, LAH.DEFAULT_SETTINGS, stored.settings || {});
  state.domains = stored.domains || {};
}

async function saveSettings(patch) {
  state.settings = Object.assign({}, state.settings, patch || {});
  await browser.storage.local.set({ settings: state.settings });
  return state.settings;
}

async function saveDomainOverrides(domain, overrides) {
  if (!domain) throw new Error('domain is required');
  if (overrides === null) {
    delete state.domains[domain];
  } else {
    state.domains[domain] = overrides;
  }
  await browser.storage.local.set({ domains: state.domains });
  return state.domains[domain] || null;
}

// ---------------------------------------------------------------------------
// native port
// ---------------------------------------------------------------------------
function setBadge() {
  const text = state.connected ? 'on' : '';
  browser.browserAction.setBadgeText({ text }).catch(() => {});
  browser.browserAction
    .setBadgeBackgroundColor({ color: state.connected ? '#2f9e44' : '#c92a2a' })
    .catch(() => {});
  browser.browserAction
    .setTitle({ title: `Local AI Hub — ${state.connected ? 'connected' : 'disconnected'}` })
    .catch(() => {});
}

function rejectAllPending(reason) {
  state.pending.forEach((entry) => {
    clearTimeout(entry.timer);
    entry.reject(new Error(reason));
  });
  state.pending.clear();
}

function connect() {
  if (state.connected || state.connecting) return;
  state.connecting = true;
  logLine('info', `connecting to native host "${HOST_NAME}"`);
  let port;
  try {
    port = browser.runtime.connectNative(HOST_NAME);
  } catch (error) {
    state.connecting = false;
    state.lastError = LAH.errorMessage(error);
    logLine('error', 'connectNative failed', state.lastError);
    scheduleRetry();
    return;
  }
  state.port = port;

  port.onMessage.addListener((message) => {
    if (!message || typeof message !== 'object') return;
    if (message.type === 'event') {
      handleHostEvent(message);
      return;
    }
    const entry = state.pending.get(message.id);
    if (!entry) return;
    state.pending.delete(message.id);
    clearTimeout(entry.timer);
    entry.resolve(message);
  });

  port.onDisconnect.addListener(() => {
    const error = port.error || browser.runtime.lastError;
    state.connected = false;
    state.connecting = false;
    state.port = null;
    state.lastDisconnect = Date.now();
    state.lastError = error ? LAH.errorMessage(error) : 'port closed';
    logLine('error', 'native host disconnected', state.lastError);
    rejectAllPending(`native host disconnected: ${state.lastError}`);
    setBadge();
    broadcast({ kind: KIND.STATUS, status: publicStatus() });
    scheduleRetry();
  });

  // A successful ping marks the connection as usable.
  rawRequest(LAH.controlRequest('ping'), 8000)
    .then((response) => {
      state.connected = true;
      state.connecting = false;
      state.retryDelay = 1000;
      state.lastError = null;
      state.hostInfo = response.output || null;
      logLine('info', `connected to host v${(response.output || {}).version || '?'}`);
      setBadge();
      broadcast({ kind: KIND.STATUS, status: publicStatus() });
      refreshHostStatus(true);
    })
    .catch((error) => {
      state.connecting = false;
      state.connected = false;
      state.lastError = LAH.errorMessage(error);
      logLine('error', 'host handshake failed', state.lastError);
      setBadge();
      scheduleRetry();
    });
}

function scheduleRetry() {
  if (!state.settings.autoConnect || state.retryTimer) return;
  const delay = state.retryDelay;
  state.retryDelay = Math.min(state.retryDelay * 2, 30000);
  state.retryTimer = setTimeout(() => {
    state.retryTimer = null;
    connect();
  }, delay);
}

function handleHostEvent(message) {
  if (message.event === 'host.ready') {
    state.hostInfo = message.data || null;
  }
  if (String(message.event).startsWith('model.')) {
    state.hostStatusAt = 0; // model state changed: force a refresh
  }
  broadcast({ kind: KIND.EVENT, event: message.event, data: message.data, id: message.id || null });
}

/** Send a frame and await the correlated result frame. */
function rawRequest(request, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (!state.port) {
      reject(new Error('native host is not connected'));
      return;
    }
    const wait = timeoutMs || state.settings.requestTimeoutMs;
    const timer = setTimeout(() => {
      state.pending.delete(request.id);
      reject(new Error(`timeout after ${Math.round((wait || 0) / 1000)}s waiting for ${request.type}`));
    }, wait);
    // MV3 workers can be suspended, taking their timers with them: the
    // keep-alive alarm also sweeps deadlines so nothing hangs forever.
    state.pending.set(request.id, { resolve, reject, timer, request, deadline: Date.now() + wait });
    try {
      state.port.postMessage(request);
    } catch (error) {
      clearTimeout(timer);
      state.pending.delete(request.id);
      reject(new Error(`postMessage failed: ${LAH.errorMessage(error)}`));
    }
  });
}

function sweepPending() {
  const now = Date.now();
  state.pending.forEach((entry, id) => {
    if (!entry.deadline || now <= entry.deadline) return;
    state.pending.delete(id);
    clearTimeout(entry.timer);
    entry.reject(new Error(`timeout waiting for ${(entry.request || {}).type || 'response'}`));
  });
}

async function request(requestPayload, timeoutMs) {
  if (!state.connected && !state.connecting) connect();
  const deadline = Date.now() + 10000;
  while (!state.connected && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 150));
    if (!state.connecting && !state.connected) break;
  }
  if (!state.connected) {
    throw new Error(
      `Local AI Hub host is not running (${state.lastError || 'not connected'}). ` +
        'Run ./install.sh from the project folder and reload Firefox.'
    );
  }
  return rawRequest(requestPayload, timeoutMs);
}

async function refreshHostStatus(force) {
  if (!state.connected) return null;
  if (!force && Date.now() - state.hostStatusAt < 5000) return state.hostStatus;
  try {
    const response = await rawRequest(LAH.controlRequest('status'), 15000);
    if (response.ok) {
      state.hostStatus = response.output;
      state.hostStatusAt = Date.now();
    }
  } catch (error) {
    logLine('error', 'status request failed', LAH.errorMessage(error));
  }
  return state.hostStatus;
}

function publicStatus() {
  return {
    connected: state.connected,
    connecting: state.connecting,
    lastError: state.lastError,
    hostInfo: state.hostInfo,
    hostStatus: state.hostStatus,
    settings: state.settings,
    log: state.log.slice(-40),
    pending: state.pending.size,
  };
}

// ---------------------------------------------------------------------------
// domain configuration
// ---------------------------------------------------------------------------
async function domainConfig(domain) {
  const overrides = state.domains[domain] || null;
  let base = { domain, features: {} };
  if (state.connected) {
    try {
      const response = await rawRequest(LAH.controlRequest('domains.get', { domain }), 10000);
      if (response.ok) base = response.output;
    } catch (error) {
      logLine('error', `domains.get failed for ${domain}`, LAH.errorMessage(error));
    }
  }
  let merged = base;
  if (overrides) {
    merged = LAH.deepMerge(base, { features: overrides.features || {} });
    // The Settings UI may also opt a site into the userscript bridge.
    if (overrides.bridge && typeof overrides.bridge === 'object') {
      merged.bridge = LAH.deepMerge(base.bridge || {}, overrides.bridge);
    }
  }
  merged.domain = domain;
  merged.source = { file: Boolean(base.features && Object.keys(base.features).length), override: Boolean(overrides) };
  return merged;
}

// ---------------------------------------------------------------------------
// image fetching (background has host permissions; content scripts do not)
//
// MV2 background pages and Firefox MV3 event pages have a DOM; a Chrome MV3
// service worker does not. Both paths produce exactly the same JPEG base64 so
// plugins never see a difference.
// ---------------------------------------------------------------------------
function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let index = 0; index < bytes.length; index += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(index, index + chunk));
  }
  return btoa(binary);
}

async function encodeJpeg(bitmap, width, height, quality) {
  if (typeof document !== 'undefined' && document.createElement) {
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    canvas.getContext('2d').drawImage(bitmap, 0, 0, width, height);
    return (canvas.toDataURL('image/jpeg', quality).split(',')[1]) || '';
  }
  if (typeof OffscreenCanvas === 'undefined') {
    throw new Error('this browser cannot re-encode images in the background (no canvas)');
  }
  const canvas = new OffscreenCanvas(width, height);
  canvas.getContext('2d').drawImage(bitmap, 0, 0, width, height);
  const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality });
  return bytesToBase64(new Uint8Array(await blob.arrayBuffer()));
}

async function fetchImage(payload) {
  const url = payload.url;
  const maxSize = Math.max(64, Math.min(2048, payload.maxSize || state.settings.imageMaxSize));
  const quality = Math.min(1, Math.max(0.3, payload.quality || state.settings.imageQuality));
  if (!url) throw new Error('image url is required');
  if (!/^(https?:|data:|blob:)/i.test(url)) throw new Error(`unsupported image scheme: ${url.slice(0, 24)}`);

  const response = await fetch(url, { credentials: 'omit', cache: 'force-cache' });
  if (!response.ok) throw new Error(`image fetch failed: HTTP ${response.status}`);
  const blob = await response.blob();
  if (blob.size > 32 * 1024 * 1024) throw new Error('image larger than 32 MB');

  let bitmap;
  try {
    bitmap = await createImageBitmap(blob);
  } catch (error) {
    throw new Error(`cannot decode image (${blob.type || 'unknown type'})`);
  }
  const scale = Math.min(1, maxSize / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  let data;
  try {
    data = await encodeJpeg(bitmap, width, height, quality);
  } finally {
    if (bitmap.close) bitmap.close();
  }
  return {
    data_base64: data,
    width,
    height,
    bytes: Math.round((data.length * 3) / 4),
    mime: 'image/jpeg',
  };
}

// ---------------------------------------------------------------------------
// message routing
//
// `sendResponse` + `return true` is the one style every browser supports
// (Chrome still does not accept a returned Promise from onMessage), so the
// same file works as an MV2 background page, an MV3 event page and an MV3
// service worker.
// ---------------------------------------------------------------------------
function handleMessage(message, sender) {
  if (!message || typeof message !== 'object' || !message.kind) return undefined;
  const tabId = sender && sender.tab ? sender.tab.id : null;

  switch (message.kind) {
    case KIND.REQUEST: {
      const payload = message.request || {};
      if (payload.context && tabId != null) payload.context.tab_id = tabId;
      return request(payload, message.timeoutMs)
        .then((response) => ({ ok: Boolean(response.ok), response }))
        .catch((error) => ({ ok: false, error: LAH.errorMessage(error) }));
    }
    case KIND.STATUS:
      return refreshHostStatus(Boolean(message.force)).then(() => publicStatus());
    case KIND.DOMAIN_CONFIG:
      return domainConfig(message.domain || LAH.domainFromUrl((sender.tab && sender.tab.url) || ''));
    case KIND.SETTINGS:
      return Promise.resolve(state.settings);
    case KIND.SET_SETTINGS:
      return saveSettings(message.settings).then((settings) => {
        broadcast({ kind: KIND.STATUS, status: publicStatus() });
        return settings;
      });
    case KIND.DOMAIN_OVERRIDES:
      return Promise.resolve({ domains: state.domains, override: state.domains[message.domain] || null });
    case KIND.SET_DOMAIN_OVERRIDES:
      return saveDomainOverrides(message.domain, message.overrides).then(() => ({ domains: state.domains }));
    case KIND.FETCH_IMAGE:
      return fetchImage(message)
        .then((image) => ({ ok: true, image }))
        .catch((error) => ({ ok: false, error: LAH.errorMessage(error) }));
    case KIND.OPEN_OPTIONS:
      return browser.runtime.openOptionsPage().then(() => ({ ok: true }));
    case KIND.PICK_RESULT:
      return browser.storage.local
        .set({ picker: message.result || null })
        .then(() => browser.runtime.openOptionsPage())
        .then(() => ({ ok: true }))
        .catch((error) => ({ ok: false, error: LAH.errorMessage(error) }));
    case KIND.GET_PICK_RESULT:
      return browser.storage.local.get('picker').then((stored) => ({ result: stored.picker || null }));
    default:
      return undefined;
  }
}

browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const pending = handleMessage(message, sender);
  if (!pending || typeof pending.then !== 'function') return undefined;
  pending.then(
    (value) => {
      try {
        sendResponse(value);
      } catch (error) {
        /* the sender went away (tab closed) — nothing to answer */
      }
    },
    (error) => {
      try {
        sendResponse({ ok: false, error: LAH.errorMessage(error) });
      } catch (ignored) {
        /* same */
      }
    }
  );
  return true; // keep the response channel open (Chrome + Firefox)
});

// ---------------------------------------------------------------------------
// context menu: "Send selection to Local AI Hub"
// ---------------------------------------------------------------------------
function setupMenus() {
  if (!browser.menus) return;
  Promise.resolve(browser.menus.removeAll())
    .then(() =>
      browser.menus.create({
        id: 'lah-selection',
        title: 'Send selection to Local AI Hub',
        contexts: ['selection'],
      })
    )
    .catch((error) => logLine('error', 'menus.create failed', LAH.errorMessage(error)));
}

if (browser.menus && browser.menus.onClicked) {
  browser.menus.onClicked.addListener((info, tab) => {
    if (info.menuItemId !== 'lah-selection' || !tab || tab.id == null) return;
    browser.tabs
      .sendMessage(tab.id, { kind: KIND.COMMAND, command: 'lah-selection', text: info.selectionText || '' })
      .catch((error) => logLine('error', 'selection dispatch failed', LAH.errorMessage(error)));
  });
}

browser.commands.onCommand.addListener((command) => {
  browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => {
    tabs.forEach((tab) => {
      browser.tabs.sendMessage(tab.id, { kind: KIND.COMMAND, command }).catch(() => {});
    });
  });
});

// ---------------------------------------------------------------------------
// MV3 keep-alive
//
// An MV3 service worker (Chrome) or event page (Firefox) is suspended when
// idle. An open native-messaging port already resets that idle timer, but a
// periodic alarm makes the behaviour explicit, reconnects after a suspension
// and sweeps request deadlines whose setTimeout died with the worker.
// MV2 has alarms too, so this code path is identical everywhere.
// ---------------------------------------------------------------------------
const KEEPALIVE_ALARM = 'lah-keepalive';

function setupKeepAlive() {
  if (!browser.alarms || !browser.alarms.create) return;
  Promise.resolve(browser.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.5 })).catch(() => {});
}

if (browser.alarms && browser.alarms.onAlarm) {
  browser.alarms.onAlarm.addListener((alarm) => {
    if (!alarm || alarm.name !== KEEPALIVE_ALARM) return;
    sweepPending();
    if (!state.connected) {
      if (state.settings.autoConnect && !state.connecting) connect();
      return;
    }
    rawRequest(LAH.controlRequest('ping'), 8000).catch((error) => {
      logLine('error', 'keep-alive ping failed', LAH.errorMessage(error));
    });
  });
}

browser.runtime.onInstalled.addListener(async () => {
  await loadSettings();
  setupMenus();
  setupKeepAlive();
  logLine('info', 'extension installed/updated');
  if (state.settings.autoConnect) connect();
});

browser.runtime.onStartup.addListener(async () => {
  await loadSettings();
  setupMenus();
  setupKeepAlive();
  if (state.settings.autoConnect) connect();
});

// MV2 persistent background page: initialise immediately as well.
// In MV3 this runs on every worker start-up, which is exactly what we want:
// listeners above are registered synchronously, state is rebuilt from storage.
loadSettings().then(() => {
  setBadge();
  setupMenus();
  setupKeepAlive();
  if (state.settings.autoConnect) connect();
});
