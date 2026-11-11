/**
 * Local AI Hub — shared protocol helpers (background, content, UI pages).
 * Plain script, no modules: usable in every extension context.
 */
'use strict';

(function (global) {
  const PROTOCOL = '1.0';
  const HOST_NAME = 'local_ai_hub';

  /** Internal extension message kinds (never exposed to webpages). */
  const KIND = {
    REQUEST: 'hub.request',
    STATUS: 'hub.status',
    DOMAIN_CONFIG: 'hub.domainConfig',
    SETTINGS: 'hub.getSettings',
    SET_SETTINGS: 'hub.setSettings',
    DOMAIN_OVERRIDES: 'hub.getDomainOverrides',
    SET_DOMAIN_OVERRIDES: 'hub.setDomainOverrides',
    FETCH_IMAGE: 'hub.fetchImage',
    EVENT: 'hub.event',
    COMMAND: 'hub.command',
    OPEN_OPTIONS: 'hub.openOptions',
    LOG: 'hub.log',
  };

  const DEFAULT_SETTINGS = {
    autoConnect: true,
    showPanel: true,
    autoRun: false,
    requestTimeoutMs: 240000,
    imageMaxSize: 768,
    imageQuality: 0.85,
    maxItems: 200,
    debug: false,
  };

  let counter = 0;
  function newId(prefix) {
    counter += 1;
    return `${prefix || 'req'}-${Date.now().toString(36)}-${counter}-${Math.random()
      .toString(36)
      .slice(2, 7)}`;
  }

  function taskRequest(plugin, input, options, context) {
    return {
      protocol: PROTOCOL,
      id: newId('task'),
      type: 'task',
      plugin,
      input: input || {},
      options: options || {},
      context: Object.assign({ interface: 'firefox' }, context || {}),
    };
  }

  function controlRequest(type, extra) {
    return Object.assign({ protocol: PROTOCOL, id: newId('ctl'), type }, extra || {});
  }

  function domainFromUrl(url) {
    try {
      return new URL(url).hostname.toLowerCase();
    } catch (_) {
      return '';
    }
  }

  /** Merge b into a (objects only, b wins). */
  function deepMerge(a, b) {
    const out = Array.isArray(a) ? a.slice() : Object.assign({}, a);
    Object.keys(b || {}).forEach((key) => {
      const value = b[key];
      if (value && typeof value === 'object' && !Array.isArray(value) && out[key] && typeof out[key] === 'object' && !Array.isArray(out[key])) {
        out[key] = deepMerge(out[key], value);
      } else {
        out[key] = value;
      }
    });
    return out;
  }

  function send(kind, payload) {
    return browser.runtime.sendMessage(Object.assign({ kind }, payload || {}));
  }

  function errorMessage(error) {
    if (!error) return 'unknown error';
    if (typeof error === 'string') return error;
    return error.message || JSON.stringify(error);
  }

  global.LAH = {
    PROTOCOL,
    HOST_NAME,
    KIND,
    DEFAULT_SETTINGS,
    newId,
    taskRequest,
    controlRequest,
    domainFromUrl,
    deepMerge,
    send,
    errorMessage,
  };
})(globalThis);
