/**
 * Local AI Hub — browser API compatibility shim.
 *
 * The extension is written once, against the promise-based `browser.*` API
 * that Firefox provides in both MV2 and MV3. Chrome only exposes `chrome.*`,
 * names a few things differently and still has callback-only methods. This
 * shim normalises all of that so no other file needs to know which browser or
 * manifest version it is running under:
 *
 *   browser            ← chrome                (when only chrome exists)
 *   browser.action     ← browserAction         (MV2)
 *   browser.browserAction ← action             (MV3, so old code keeps working)
 *   browser.menus      ← contextMenus          (Chrome)
 *   promise-returning methods                  (callback APIs are wrapped)
 *
 * It is injected by scripts/build_mv3.py as the first background and content
 * script. The MV2 build does not load it at all: Firefox already speaks this
 * dialect, so the shipped MV2 extension stays byte-for-byte unchanged.
 *
 * Nothing here is page-facing: no exports, no window properties beyond the
 * `browser` global that extensions already have, no eval, no remote code.
 */
'use strict';

(function (root) {
  if (!root) return;
  const hadBrowser = Boolean(root.browser);
  const native = root.browser || root.chrome;
  if (!native) return;

  /** Methods that may still be callback-only in some browser/version. */
  const CALLBACK_METHODS = {
    runtime: ['sendMessage', 'openOptionsPage', 'setUninstallURL', 'reload'],
    tabs: [
      'query', 'get', 'create', 'update', 'remove', 'reload', 'sendMessage',
      'executeScript', 'insertCSS', 'removeCSS', 'highlight', 'move', 'duplicate',
    ],
    menus: ['create', 'update', 'remove', 'removeAll', 'refresh'],
    contextMenus: ['create', 'update', 'remove', 'removeAll', 'refresh'],
    browserAction: [
      'setBadgeText', 'setBadgeBackgroundColor', 'setBadgeTextColor', 'setTitle',
      'setIcon', 'setPopup', 'enable', 'disable', 'openPopup',
    ],
    action: [
      'setBadgeText', 'setBadgeBackgroundColor', 'setBadgeTextColor', 'setTitle',
      'setIcon', 'setPopup', 'enable', 'disable', 'openPopup',
    ],
    alarms: ['create', 'clear', 'clearAll', 'get', 'getAll'],
    scripting: ['executeScript', 'insertCSS', 'removeCSS'],
    permissions: ['request', 'remove', 'contains', 'getAll'],
    windows: ['get', 'getAll', 'getCurrent', 'create', 'update', 'remove'],
    storageArea: ['get', 'set', 'remove', 'clear', 'getBytesInUse'],
  };

  const wrapped = new WeakMap();

  /** Call a possibly-callback API and always hand back a promise. */
  function promisify(fn, owner) {
    return function (...args) {
      let returned;
      const promise = new Promise((resolve, reject) => {
        const done = function (value) {
          const error = root.chrome && root.chrome.runtime && root.chrome.runtime.lastError;
          if (error) reject(new Error(error.message || String(error)));
          else resolve(value);
        };
        try {
          returned = fn.apply(owner, args.concat([done]));
        } catch (error) {
          reject(error);
        }
      });
      // Firefox (and modern Chrome without a callback) already returns a
      // promise; prefer it so behaviour is identical on every browser.
      if (returned && typeof returned.then === 'function') return returned;
      return promise;
    };
  }

  function wrap(target, methods) {
    if (!target) return target;
    let cache = wrapped.get(target);
    if (!cache) {
      cache = new Map();
      wrapped.set(target, cache);
    }
    return new Proxy(target, {
      get(object, property) {
        const value = object[property];
        if (typeof value !== 'function') return value;
        if (methods.indexOf(property) === -1) {
          // Keep the original receiver (connectNative, getURL, getManifest…).
          if (!cache.has(property)) cache.set(property, value.bind(object));
          return cache.get(property);
        }
        if (!cache.has(property)) cache.set(property, promisify(value, object));
        return cache.get(property);
      },
    });
  }

  function wrapStorage(storage) {
    if (!storage) return storage;
    const facade = Object.create(null);
    ['local', 'sync', 'session', 'managed'].forEach((key) => {
      const area = storage[key];
      if (area && typeof area.get === 'function') facade[key] = wrap(area, CALLBACK_METHODS.storageArea);
      else if (area) facade[key] = area;
    });
    Object.keys(storage).forEach((key) => {
      if (!(key in facade)) facade[key] = storage[key];
    });
    return facade;
  }

  const facade = Object.create(null);
  const names = [
    'runtime', 'tabs', 'commands', 'alarms', 'scripting', 'permissions',
    'windows', 'i18n', 'extension', 'notifications',
  ];
  names.forEach((name) => {
    if (native[name]) facade[name] = wrap(native[name], CALLBACK_METHODS[name] || []);
  });
  if (native.storage) facade.storage = wrapStorage(native.storage);

  // menus ⇄ contextMenus, action ⇄ browserAction: expose whichever exists
  // under both names so callers never have to branch.
  const menus = native.menus || native.contextMenus;
  if (menus) {
    const wrappedMenus = wrap(menus, CALLBACK_METHODS.menus);
    facade.menus = wrappedMenus;
    facade.contextMenus = wrappedMenus;
  }
  const action = native.action || native.browserAction;
  if (action) {
    const wrappedAction = wrap(action, CALLBACK_METHODS.action);
    facade.action = wrappedAction;
    facade.browserAction = wrappedAction;
  }

  // Anything we did not model explicitly stays reachable, unwrapped.
  const passthrough = new Proxy(native, {
    get(object, property) {
      if (property in facade) return facade[property];
      return object[property];
    },
    has(object, property) {
      return property in facade || property in object;
    },
  });

  root.browser = passthrough;
  if (!root.chrome) root.chrome = native;

  /** Diagnostics for the options page / doctor output. */
  root.LAH_BROWSER = {
    vendor: root.navigator && /firefox/i.test(root.navigator.userAgent || '') ? 'firefox' : 'chromium',
    manifestVersion:
      (native.runtime && native.runtime.getManifest && native.runtime.getManifest().manifest_version) || 2,
    worker: typeof document === 'undefined',
    shimmed: !hadBrowser,
  };
})(typeof globalThis !== 'undefined' ? globalThis : typeof self !== 'undefined' ? self : this);
