/**
 * Local AI Hub — userscript (Tampermonkey / Violentmonkey) bridge.
 *
 * A userscript cannot use native messaging.  It talks to this content script
 * through CustomEvents on `document`, and only where the *user* opted in:
 *
 *   config/domains/<host>.json
 *   { "bridge": { "tampermonkey": true, "plugins": ["comments.classify"] }, ... }
 *
 * Security rules (mirrored by hub/bridge.py, which re-checks every task):
 *
 *   - no opt-in  -> the bridge is completely silent (no reply, no fingerprint)
 *   - the page may name a *registered, allowlisted plugin id* and nothing else
 *   - the page supplies INPUT DATA only: options, selectors, actions, config
 *     and code are never read from the request (strict whitelist)
 *   - input is JSON, size-capped and rate-limited
 *   - applying results uses the user's own `apply` spec, never a page-supplied
 *     one, and only when the domain config sets bridge.allow_apply
 *
 * Wire format: `event.detail` is a JSON **string** in both directions.  Strings
 * cross the content/page boundary safely in Firefox without cloneInto, and a
 * string can never carry functions, DOM nodes or getters.
 *
 *   page -> hub   document.dispatchEvent(new CustomEvent('local-ai-hub:request',
 *                   { detail: JSON.stringify({version:1, id:'r1',
 *                     plugin:'comments.classify', input:{comments:[...]}}) }))
 *   hub  -> page  'local-ai-hub:response'  detail = JSON string
 *                   {version:1, id:'r1', ok:true, output:{...}, meta:{...}}
 */
'use strict';

(function (global) {
  const REQUEST_EVENT = 'local-ai-hub:request';
  const RESPONSE_EVENT = 'local-ai-hub:response';
  const HELLO_EVENT = 'local-ai-hub:hello';
  const READY_EVENT = 'local-ai-hub:ready';
  const VERSION = 1;

  const DEFAULT_MAX_PER_MINUTE = 30;
  const DEFAULT_MAX_INPUT_BYTES = 256 * 1024;
  const MAX_IN_FLIGHT = 2;
  const ID_RE = /^[A-Za-z0-9_.:-]{1,128}$/;

  /** Only these keys are ever read out of a page request. */
  const ACCEPTED_KEYS = ['version', 'id', 'plugin', 'input', 'apply'];

  function byteLength(text) {
    // Cheap UTF-8 length without TextEncoder (not present in every test stub).
    let bytes = 0;
    for (let i = 0; i < text.length; i += 1) {
      const code = text.charCodeAt(i);
      if (code < 0x80) bytes += 1;
      else if (code < 0x800) bytes += 2;
      else if (code >= 0xd800 && code <= 0xdbff) {
        bytes += 4;
        i += 1;
      } else bytes += 3;
    }
    return bytes;
  }

  /**
   * Fallback policy resolution, used when the host is offline and therefore
   * could not hand us `bridge_policy`.  hub/bridge.py is the authority.
   */
  function policy(domainConfig) {
    const config = domainConfig || {};
    const bridge = config.bridge && typeof config.bridge === 'object' ? config.bridge : {};
    const features = config.features && typeof config.features === 'object' ? config.features : {};
    if (config.bridge_policy && typeof config.bridge_policy === 'object') {
      return config.bridge_policy;
    }
    const enabledFeatures = Object.keys(features).filter(
      (name) => features[name] && features[name].enabled
    );
    const declared = Array.isArray(bridge.plugins) && bridge.plugins.length
      ? bridge.plugins.map(String)
      : enabledFeatures;
    return {
      enabled: Boolean(bridge.tampermonkey),
      opted_in: Boolean(bridge.tampermonkey),
      domain: config.domain || '',
      plugins: Array.from(new Set(declared)).sort(),
      allow_apply: Boolean(bridge.allow_apply),
      max_requests_per_minute: Number(bridge.max_requests_per_minute) > 0
        ? Number(bridge.max_requests_per_minute)
        : DEFAULT_MAX_PER_MINUTE,
      max_input_bytes: Number(bridge.max_input_bytes) > 0
        ? Number(bridge.max_input_bytes)
        : DEFAULT_MAX_INPUT_BYTES,
    };
  }

  /** Parse `event.detail` (JSON string or plain object) into a plain object. */
  function decodeDetail(detail) {
    if (typeof detail === 'string') {
      if (byteLength(detail) > 4 * 1024 * 1024) return null;
      try {
        const parsed = JSON.parse(detail);
        return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
      } catch (_) {
        return null;
      }
    }
    if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
      try {
        return JSON.parse(JSON.stringify(detail));
      } catch (_) {
        return null;
      }
    }
    return null;
  }

  /**
   * Validate one page request against the policy.
   * Returns {ok:true, value:{id, plugin, input, apply}, ignored:[keys]}
   *      or {ok:false, error:{code, message}, id}
   */
  function sanitize(detail, resolvedPolicy, generateId) {
    const pol = resolvedPolicy || {};
    const raw = decodeDetail(detail);
    if (!raw) {
      return { ok: false, id: null, error: { code: 'bad_request', message: 'detail must be a JSON object or JSON string' } };
    }
    const id = raw.id == null ? (generateId ? generateId() : `bridge-${Date.now()}`) : String(raw.id);
    if (!ID_RE.test(id)) {
      return { ok: false, id: null, error: { code: 'bad_request', message: 'id must match [A-Za-z0-9_.:-]{1,128}' } };
    }
    if (raw.version != null && Number(raw.version) !== VERSION) {
      return { ok: false, id, error: { code: 'bad_request', message: `unsupported bridge version ${raw.version}` } };
    }
    if (!pol.enabled) {
      return { ok: false, id, error: { code: 'forbidden', message: 'the userscript bridge is not enabled for this domain' } };
    }
    const plugin = typeof raw.plugin === 'string' ? raw.plugin : '';
    if (!plugin || (pol.plugins || []).indexOf(plugin) === -1) {
      return { ok: false, id, error: { code: 'forbidden', message: `plugin ${plugin || '(missing)'} is not exposed to the bridge on this domain` } };
    }
    const input = raw.input == null ? {} : raw.input;
    if (typeof input !== 'object' || Array.isArray(input)) {
      return { ok: false, id, error: { code: 'invalid_input', message: 'input must be a JSON object' } };
    }
    let encoded;
    try {
      encoded = JSON.stringify(input);
    } catch (_) {
      return { ok: false, id, error: { code: 'invalid_input', message: 'input is not serialisable' } };
    }
    const limit = Number(pol.max_input_bytes) > 0 ? Number(pol.max_input_bytes) : DEFAULT_MAX_INPUT_BYTES;
    if (byteLength(encoded) > limit) {
      return { ok: false, id, error: { code: 'input_too_large', message: `bridge input exceeds ${limit} bytes` } };
    }
    const wantsApply = raw.apply === true;
    if (wantsApply && !pol.allow_apply) {
      return { ok: false, id, error: { code: 'forbidden', message: 'this domain does not allow the page to apply results (bridge.allow_apply)' } };
    }
    const ignored = Object.keys(raw).filter((key) => ACCEPTED_KEYS.indexOf(key) === -1);
    return { ok: true, id, value: { id, plugin, input: JSON.parse(encoded), apply: wantsApply }, ignored };
  }

  /** Sliding-window rate limiter (per page). */
  class RateLimiter {
    constructor(maxPerMinute, now) {
      this.max = Number(maxPerMinute) > 0 ? Number(maxPerMinute) : DEFAULT_MAX_PER_MINUTE;
      this.now = now || (() => Date.now());
      this.hits = [];
    }
    allow() {
      const cutoff = this.now() - 60000;
      this.hits = this.hits.filter((at) => at > cutoff);
      if (this.hits.length >= this.max) return false;
      this.hits.push(this.now());
      return true;
    }
    reset() {
      this.hits = [];
    }
  }

  /**
   * Wire the bridge to the page.
   *
   * options.getPolicy() -> resolved policy (re-read per event, so config
   *                      changes from the Settings UI apply immediately)
   * options.submit(req) -> Promise<{ok, output, error, meta, applied}>
   * options.target      -> event target (document)
   * options.dispatch    -> (name, detailString) => void   [test seam]
   * options.log         -> (message, level) => void
   */
  function attach(options) {
    const opts = options || {};
    const target = opts.target || (typeof document !== 'undefined' ? document : null);
    if (!target) return { detach() {}, handleRequest() {} };
    const log = opts.log || function () {};
    const dispatch =
      opts.dispatch ||
      function (name, detail) {
        target.dispatchEvent(new CustomEvent(name, { detail }));
      };
    const submit = opts.submit;
    const getPolicy = opts.getPolicy || (() => ({ enabled: false, plugins: [] }));
    let limiter = new RateLimiter(getPolicy().max_requests_per_minute, opts.now);
    let limiterMax = limiter.max;
    let inFlight = 0;

    function reply(id, payload) {
      dispatch(RESPONSE_EVENT, JSON.stringify(Object.assign({ version: VERSION, id }, payload)));
    }

    function onHello() {
      const pol = getPolicy();
      if (!pol.enabled) return; // silent on sites that did not opt in
      dispatch(
        READY_EVENT,
        JSON.stringify({
          version: VERSION,
          domain: pol.domain || '',
          plugins: pol.plugins || [],
          allow_apply: Boolean(pol.allow_apply),
          max_requests_per_minute: pol.max_requests_per_minute,
          max_input_bytes: pol.max_input_bytes,
        })
      );
    }

    function onRequest(event) {
      const pol = getPolicy();
      if (!pol.enabled) return; // silent: do not even acknowledge the extension
      if (limiter.max !== Number(pol.max_requests_per_minute) && Number(pol.max_requests_per_minute) > 0) {
        limiterMax = Number(pol.max_requests_per_minute);
        limiter = new RateLimiter(limiterMax, opts.now);
      }
      const checked = sanitize(event ? event.detail : null, pol, () => `bridge-${Date.now()}`);
      if (!checked.ok) {
        log(`bridge rejected: ${checked.error.message}`, 'warn');
        reply(checked.id, { ok: false, error: checked.error });
        return;
      }
      if (checked.ignored && checked.ignored.length) {
        log(`bridge ignored page-supplied keys: ${checked.ignored.join(', ')}`, 'warn');
      }
      if (!limiter.allow()) {
        reply(checked.id, {
          ok: false,
          error: { code: 'rate_limited', message: `more than ${limiter.max} bridge requests per minute` },
        });
        return;
      }
      if (inFlight >= MAX_IN_FLIGHT) {
        reply(checked.id, {
          ok: false,
          error: { code: 'rate_limited', message: 'too many bridge tasks already running' },
        });
        return;
      }
      inFlight += 1;
      Promise.resolve()
        .then(() => submit(checked.value))
        .then((result) => {
          const answer = result || {};
          reply(checked.id, {
            ok: Boolean(answer.ok),
            output: answer.ok ? answer.output : undefined,
            error: answer.ok ? undefined : answer.error || { code: 'plugin_failed', message: 'task failed' },
            meta: Object.assign({ ignored_keys: checked.ignored || [] }, answer.meta || {}),
            applied: answer.applied,
          });
        })
        .catch((error) => {
          reply(checked.id, {
            ok: false,
            error: { code: 'internal_error', message: (error && error.message) || String(error) },
          });
        })
        .then(() => {
          inFlight -= 1;
        });
    }

    target.addEventListener(REQUEST_EVENT, onRequest);
    target.addEventListener(HELLO_EVENT, onHello);
    return {
      detach() {
        target.removeEventListener(REQUEST_EVENT, onRequest);
        target.removeEventListener(HELLO_EVENT, onHello);
      },
      handleRequest: onRequest,
      hello: onHello,
    };
  }

  global.LAHBridge = {
    VERSION,
    REQUEST_EVENT,
    RESPONSE_EVENT,
    HELLO_EVENT,
    READY_EVENT,
    policy,
    sanitize,
    decodeDetail,
    byteLength,
    RateLimiter,
    attach,
  };
})(globalThis);
