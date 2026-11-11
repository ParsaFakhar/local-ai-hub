// ==UserScript==
// @name         Local AI Hub bridge (example)
// @namespace    https://github.com/ParsaFakhar/local-ai-hub
// @version      1.0.0
// @description  Talk to your local AI Hub from a userscript — input data only, no cloud.
// @author       Local AI Hub
// @match        https://example-board.com/*
// @grant        none
// @run-at       document-idle
// ==/UserScript==

/*
 * REQUIREMENTS
 *   1. The Local AI Hub Firefox extension is loaded and its native host runs.
 *   2. The domain is opted in, in config/domains/<host>.json (or Settings → Domains):
 *
 *        { "bridge": { "tampermonkey": true,
 *                      "plugins": ["comments.classify", "hello.python"],
 *                      "allow_apply": false } }
 *
 *   Without that opt-in the content script stays completely silent.
 *
 * WHAT A USERSCRIPT CAN AND CANNOT DO
 *   can    : name an allowlisted plugin id, send JSON input, ask for the result
 *   cannot : send options, selectors, actions, config, code, or a model name
 *
 * Wire format: CustomEvents on `document`, `detail` is a JSON *string*.
 */

(function () {
  'use strict';

  const REQUEST = 'local-ai-hub:request';
  const RESPONSE = 'local-ai-hub:response';
  const HELLO = 'local-ai-hub:hello';
  const READY = 'local-ai-hub:ready';

  /** Minimal promise client for the bridge. */
  const Hub = {
    pending: new Map(),
    counter: 0,
    info: null,

    init() {
      document.addEventListener(RESPONSE, (event) => {
        let message;
        try {
          message = JSON.parse(event.detail);
        } catch (_) {
          return;
        }
        const entry = this.pending.get(message.id);
        if (!entry) return;
        this.pending.delete(message.id);
        clearTimeout(entry.timer);
        if (message.ok) entry.resolve(message);
        else entry.reject(new Error(`${message.error.code}: ${message.error.message}`));
      });
      document.addEventListener(READY, (event) => {
        try {
          this.info = JSON.parse(event.detail);
          console.info('[LocalAIHub] bridge ready', this.info);
        } catch (_) {
          /* ignore */
        }
      });
      return this;
    },

    /** Ask which plugins this site exposes (resolves null if not opted in). */
    hello(timeoutMs = 1500) {
      return new Promise((resolve) => {
        const done = (value) => {
          document.removeEventListener(READY, onReady);
          resolve(value);
        };
        const onReady = (event) => {
          try {
            done(JSON.parse(event.detail));
          } catch (_) {
            done(null);
          }
        };
        document.addEventListener(READY, onReady);
        document.dispatchEvent(new CustomEvent(HELLO, { detail: '{}' }));
        setTimeout(() => done(null), timeoutMs);
      });
    },

    /** Run a plugin. `apply` only works when the domain allows it. */
    run(plugin, input, options = {}) {
      this.counter += 1;
      const id = `us-${Date.now().toString(36)}-${this.counter}`;
      const payload = { version: 1, id, plugin, input, apply: Boolean(options.apply) };
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error('Local AI Hub did not answer (is the extension running and this domain opted in?)'));
        }, options.timeoutMs || 120000);
        this.pending.set(id, { resolve, reject, timer });
        document.dispatchEvent(new CustomEvent(REQUEST, { detail: JSON.stringify(payload) }));
      });
    },
  }.init();

  // Expose it so you can experiment from the devtools console.
  window.LocalAIHub = Hub;

  // -------------------------------------------------------------------------
  // Demo UI: a small button that classifies the posts on the page.
  // -------------------------------------------------------------------------
  function collectComments() {
    return Array.from(document.querySelectorAll('p.comment, .comment, article'))
      .slice(0, 40)
      .map((element, index) => ({
        id: element.getAttribute('data-lah-id') || `us-${index}`,
        text: (element.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 2000),
      }))
      .filter((comment) => comment.text);
  }

  function button(label, onClick) {
    const node = document.createElement('button');
    node.textContent = label;
    node.style.cssText =
      'position:fixed;right:16px;bottom:16px;z-index:2147483646;padding:8px 12px;' +
      'border-radius:8px;border:1px solid #3a4048;background:#1c6dd0;color:#fff;font:13px sans-serif;cursor:pointer';
    node.addEventListener('click', onClick);
    document.body.appendChild(node);
    return node;
  }

  button('Local AI Hub: classify', async (event) => {
    const node = event.currentTarget;
    node.disabled = true;
    node.textContent = 'asking the local hub…';
    try {
      const info = await Hub.hello();
      if (!info) throw new Error('bridge is not enabled for this domain');
      const comments = collectComments();
      if (!comments.length) throw new Error('no comments found — adjust collectComments()');
      const answer = await Hub.run('comments.classify', {
        comments,
        page_title: document.title,
      });
      const counts = (answer.output.summary || {}).counts || {};
      node.textContent = `done: ${JSON.stringify(counts)}`;
      console.info('[LocalAIHub] result', answer.output, answer.meta);
    } catch (error) {
      node.textContent = `error: ${error.message}`;
      console.warn('[LocalAIHub]', error);
    } finally {
      setTimeout(() => {
        node.disabled = false;
        node.textContent = 'Local AI Hub: classify';
      }, 4000);
    }
  });
})();
