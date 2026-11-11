/**
 * Extension logic tests (no browser needed).
 *
 *   node tests/test_extension.js
 *
 * Loads the real extension scripts into a minimal DOM stub and verifies the
 * security-critical parts: template rendering is text-only, the action
 * allowlist is enforced, and results are matched back by id.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const ROOT = path.resolve(__dirname, '..');
const EXT = path.join(ROOT, 'extension');

// ---------------------------------------------------------------- tiny DOM
class ClassList {
  constructor() {
    this.set = new Set();
  }
  add(name) {
    this.set.add(name);
  }
  remove(name) {
    this.set.delete(name);
  }
  toggle(name, on) {
    if (on) this.set.add(name);
    else this.set.delete(name);
  }
  contains(name) {
    return this.set.has(name);
  }
}

class Element {
  constructor(tagName = 'div') {
    this.tagName = tagName.toUpperCase();
    this.attributes = new Map();
    this.classList = new ClassList();
    this.children = [];
    this.textContent = '';
    this.nextElementSibling = null;
    this.title = '';
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  removeAttribute(name) {
    this.attributes.delete(name);
  }
  appendChild(child) {
    this.children.push(child);
    return child;
  }
  insertAdjacentElement(_position, child) {
    this.nextElementSibling = child;
    return child;
  }
  querySelector(selector) {
    return this.children.find((child) => child.matches(selector)) || null;
  }
  matches(selector) {
    const attr = selector.match(/^\[([a-z-]+)="([^"]+)"\]$/);
    if (attr) return this.getAttribute(attr[1]) === attr[2];
    return false;
  }
}

const sandbox = {
  console,
  document: { createElement: (tag) => new Element(tag), baseURI: 'https://example-board.com/t/1' },
  URL,
  Set,
  Map,
  Number,
  String,
  Boolean,
  Array,
  Object,
  JSON,
  Math,
  Date,
  RegExp,
  Promise,
  globalThis: null,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

for (const file of ['lib/protocol.js', 'content/appliers.js', 'content/bridge.js']) {
  vm.runInContext(fs.readFileSync(path.join(EXT, file), 'utf8'), sandbox, { filename: file });
}

const { LAH, LAHApply, LAHBridge } = sandbox;

// ---------------------------------------------------------------- tests
let passed = 0;
const queue = [];
/** Register a check; sync and async bodies are both supported. */
function test(name, fn) {
  queue.push([name, fn]);
}

console.log('Extension logic tests\n');

test('protocol helpers build universal tasks', () => {
  const request = LAH.taskRequest('comments.classify', { comments: [] }, { batch_size: 4 }, { domain: 'x.test' });
  assert.strictEqual(request.type, 'task');
  assert.strictEqual(request.protocol, '1.0');
  assert.strictEqual(request.context.interface, 'firefox');
  assert.ok(request.id);
});

test('domain extraction from url', () => {
  assert.strictEqual(LAH.domainFromUrl('https://Board.Example.com/t/1'), 'board.example.com');
  assert.strictEqual(LAH.domainFromUrl('not a url'), '');
});

test('deep merge keeps untouched keys', () => {
  const merged = LAH.deepMerge({ a: { x: 1, y: 2 }, b: 3 }, { a: { y: 9 } });
  assert.strictEqual(JSON.stringify(merged), JSON.stringify({ a: { x: 1, y: 9 }, b: 3 }));
});

test('templates render text, arrays and numbers', () => {
  assert.strictEqual(LAHApply.template('{label} {confidence}', { label: 'spam', confidence: 0.912 }), 'spam 0.91');
  assert.strictEqual(LAHApply.template('{tags}', { tags: ['cat', 'sunset'] }), 'cat, sunset');
  assert.strictEqual(LAHApply.template('{missing}', {}), '');
});

test('templates never evaluate code', () => {
  const rendered = LAHApply.template('{label}', { label: '<img src=x onerror=alert(1)>' });
  assert.strictEqual(rendered, '<img src=x onerror=alert(1)>'); // stored as textContent, never parsed
});

test('badge + class + attribute + hide are applied by id', () => {
  const element = new Element('p');
  const items = new Map([['c0', { id: 'c0', element }]]);
  const output = { results: [{ id: 'c0', label: 'spam', confidence: 0.9 }] };
  const spec = {
    results_path: 'results',
    match_by: 'id',
    actions: [
      { action: 'badge', text: '{label} {confidence}' },
      { action: 'class', map: { spam: 'lah-flag-spam' } },
      { action: 'attribute', name: 'data-lah-label', value: '{label}' },
      { action: 'hide', when: { field: 'label', in: ['spam'] } },
    ],
  };
  const report = LAHApply.applyResults(output, items, spec, { pluginId: 'comments.classify' });
  assert.strictEqual(report.applied, 4);
  assert.strictEqual(element.children[0].textContent, 'spam 0.9');
  assert.ok(element.classList.contains('lah-flag-spam'));
  assert.strictEqual(element.getAttribute('data-lah-label'), 'spam');
  assert.ok(element.classList.contains('lah-hidden'));
});

test('unknown actions and dangerous attributes are ignored', () => {
  const element = new Element('p');
  const items = new Map([['c0', { id: 'c0', element }]]);
  const output = { results: [{ id: 'c0', label: 'ok' }] };
  const spec = {
    actions: [
      { action: 'script', value: 'alert(1)' },
      { action: 'attribute', name: 'onclick', value: 'alert(1)' },
      { action: 'attribute', name: 'src', value: 'javascript:alert(1)' },
      { action: 'text', value: 'overwrite' },
    ],
  };
  const report = LAHApply.applyResults(output, items, spec, { pluginId: 'x' });
  assert.strictEqual(report.applied, 0);
  assert.strictEqual(element.getAttribute('onclick'), null);
  assert.strictEqual(element.textContent, '');
});

test('text replacement requires explicit opt-in', () => {
  const element = new Element('p');
  const items = new Map([['c0', { id: 'c0', element }]]);
  const output = { results: [{ id: 'c0', label: 'ok' }] };
  const spec = { actions: [{ action: 'text', value: '{label}' }] };
  assert.strictEqual(LAHApply.applyResults(output, items, spec, { pluginId: 'x' }).applied, 0);
  assert.strictEqual(
    LAHApply.applyResults(output, items, spec, { pluginId: 'x', allowTextReplace: true }).applied,
    1
  );
  assert.strictEqual(element.textContent, 'ok');
});

test('class names are validated', () => {
  const element = new Element('p');
  const items = new Map([['c0', { id: 'c0', element }]]);
  const output = { results: [{ id: 'c0', label: 'x' }] };
  const spec = { actions: [{ action: 'class', map: { x: 'bad class; drop' } }] };
  assert.strictEqual(LAHApply.applyResults(output, items, spec, { pluginId: 'x' }).applied, 0);
});

test('unmatched ids are reported, not applied', () => {
  const items = new Map([['c0', { id: 'c0', element: new Element('p') }]]);
  const output = { results: [{ id: 'ghost', label: 'spam' }] };
  const report = LAHApply.applyResults(output, items, { actions: [{ action: 'badge', text: '{label}' }] }, { pluginId: 'x' });
  assert.strictEqual(report.applied, 0);
  assert.strictEqual(JSON.stringify(report.skipped), JSON.stringify(['ghost']));
});

test('image badges are placed next to the img element', () => {
  const image = new Element('img');
  const items = new Map([['i0', { id: 'i0', element: image }]]);
  const output = { results: [{ id: 'i0', tags: ['cat'], description: 'a cat' }] };
  const spec = { actions: [{ action: 'badge', text: '{tags}' }, { action: 'attribute', name: 'title', value: '{description}' }] };
  const report = LAHApply.applyResults(output, items, spec, { pluginId: 'images.tag' });
  assert.strictEqual(report.applied, 2);
  assert.strictEqual(image.nextElementSibling.textContent, 'cat');
  assert.strictEqual(image.getAttribute('title'), 'a cat');
});

test('manifest is valid MV2 with the expected id and permissions', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  assert.strictEqual(manifest.manifest_version, 2);
  assert.strictEqual(manifest.browser_specific_settings.gecko.id, 'local-ai-hub@localaihub.dev');
  assert.ok(manifest.permissions.includes('nativeMessaging'));
  assert.ok(manifest.background.persistent, 'persistent background keeps the native port alive');
  for (const file of manifest.background.scripts) {
    assert.ok(fs.existsSync(path.join(EXT, file)), `missing ${file}`);
  }
  for (const script of manifest.content_scripts[0].js.concat(manifest.content_scripts[0].css)) {
    assert.ok(fs.existsSync(path.join(EXT, script)), `missing ${script}`);
  }
  for (const size of Object.keys(manifest.icons)) {
    assert.ok(fs.existsSync(path.join(EXT, manifest.icons[size])), `missing icon ${size}`);
  }
});

// ------------------------------------------------------------ dry run / review
test('dry run plans changes without touching the DOM', () => {
  const element = new Element('p');
  const items = new Map([['c0', { id: 'c0', element }]]);
  const output = { results: [{ id: 'c0', label: 'spam', confidence: 0.9 }] };
  const spec = {
    actions: [
      { action: 'badge', text: '{label}' },
      { action: 'class', map: { spam: 'lah-flag-spam' } },
      { action: 'hide', when: { field: 'label', in: ['spam'] } },
    ],
  };
  const context = { pluginId: 'comments.classify' };
  const planned = LAHApply.planResults(output, items, spec, context);
  assert.strictEqual(planned.plans.length, 1);
  assert.strictEqual(planned.plans[0].changes.length, 3);
  assert.strictEqual(element.children.length, 0, 'dry run must not add a badge');
  assert.strictEqual(element.classList.contains('lah-flag-spam'), false);
  assert.strictEqual(element.classList.contains('lah-hidden'), false);

  const report = LAHApply.applyPlans(planned.plans, context);
  assert.strictEqual(report.applied, 3);
  assert.strictEqual(element.children[0].textContent, 'spam');
});

test('review can drop individual items before applying', () => {
  const a = new Element('p');
  const b = new Element('p');
  const items = new Map([['a', { id: 'a', element: a }], ['b', { id: 'b', element: b }]]);
  const output = { results: [{ id: 'a', label: 'spam' }, { id: 'b', label: 'spam' }] };
  const spec = { actions: [{ action: 'badge', text: '{label}' }] };
  const context = { pluginId: 'x' };
  const planned = LAHApply.planResults(output, items, spec, context);
  const accepted = planned.plans.filter((plan) => plan.id === 'a');
  assert.strictEqual(LAHApply.applyPlans(accepted, context).applied, 1);
  assert.strictEqual(a.children.length, 1);
  assert.strictEqual(b.children.length, 0, 'rejected item must stay untouched');
});

test('plan descriptions are human readable text', () => {
  const element = new Element('p');
  const plan = LAHApply.planAction(element, { action: 'attribute', name: 'title', value: '{label}' }, { label: 'spam' }, {});
  assert.strictEqual(LAHApply.describePlan(plan), 'title="spam"');
});

// ------------------------------------------------------------------ form fill
test('fill requires opt-in and never touches password fields', () => {
  const input = new Element('input');
  input.setAttribute('type', 'text');
  input.value = '';
  const items = new Map([['f0', { id: 'f0', element: input }]]);
  const output = { results: [{ id: 'f0', value: 'Ada Lovelace' }] };
  const spec = { actions: [{ action: 'fill', value: '{value}' }] };
  assert.strictEqual(LAHApply.applyResults(output, items, spec, { pluginId: 'forms.fill' }).applied, 0);
  assert.strictEqual(input.value, '');
  assert.strictEqual(
    LAHApply.applyResults(output, items, spec, { pluginId: 'forms.fill', allowFormFill: true }).applied,
    1
  );
  assert.strictEqual(input.value, 'Ada Lovelace');

  const secret = new Element('input');
  secret.setAttribute('type', 'password');
  secret.value = 'hunter2';
  const secretItems = new Map([['f0', { id: 'f0', element: secret }]]);
  assert.strictEqual(
    LAHApply.applyResults(output, secretItems, spec, { pluginId: 'forms.fill', allowFormFill: true }).applied,
    0
  );
  assert.strictEqual(secret.value, 'hunter2');
});

// --------------------------------------------------------------------- bridge
const boardConfig = {
  domain: 'example-board.com',
  bridge: { tampermonkey: true, allow_apply: false },
  features: {
    'comments.classify': { enabled: true },
    'images.tag': { enabled: false },
  },
};

test('bridge is off unless the domain opts in', () => {
  assert.strictEqual(LAHBridge.policy({ features: { 'comments.classify': { enabled: true } } }).enabled, false);
  const pol = LAHBridge.policy(boardConfig);
  assert.strictEqual(pol.enabled, true);
  assert.deepStrictEqual(pol.plugins, ['comments.classify']);
  assert.strictEqual(pol.allow_apply, false);
});

test('bridge explicit plugin list wins over enabled features', () => {
  const pol = LAHBridge.policy({
    bridge: { tampermonkey: true, plugins: ['hello.python'] },
    features: { 'comments.classify': { enabled: true } },
  });
  assert.deepStrictEqual(pol.plugins, ['hello.python']);
});

test('bridge prefers the policy resolved by the host', () => {
  const pol = LAHBridge.policy({ bridge_policy: { enabled: true, plugins: ['x.y'], allow_apply: true } });
  assert.deepStrictEqual(pol.plugins, ['x.y']);
  assert.strictEqual(pol.allow_apply, true);
});

test('bridge accepts only allowlisted plugins and object input', () => {
  const pol = LAHBridge.policy(boardConfig);
  const good = LAHBridge.sanitize(JSON.stringify({ id: 'r1', plugin: 'comments.classify', input: { comments: [] } }), pol);
  assert.strictEqual(good.ok, true);
  assert.strictEqual(good.value.plugin, 'comments.classify');

  const wrongPlugin = LAHBridge.sanitize({ id: 'r2', plugin: 'shell.exec', input: {} }, pol);
  assert.strictEqual(wrongPlugin.ok, false);
  assert.strictEqual(wrongPlugin.error.code, 'forbidden');

  const disabledPlugin = LAHBridge.sanitize({ id: 'r3', plugin: 'images.tag', input: {} }, pol);
  assert.strictEqual(disabledPlugin.ok, false, 'a disabled feature is not exposed');

  const badInput = LAHBridge.sanitize({ id: 'r4', plugin: 'comments.classify', input: 'rm -rf /' }, pol);
  assert.strictEqual(badInput.ok, false);
  assert.strictEqual(badInput.error.code, 'invalid_input');

  const badId = LAHBridge.sanitize({ id: '../../etc/passwd', plugin: 'comments.classify', input: {} }, pol);
  assert.strictEqual(badId.ok, false);
});

test('bridge drops page-supplied config, options and code', () => {
  const pol = LAHBridge.policy(boardConfig);
  const checked = LAHBridge.sanitize(
    {
      id: 'r5',
      plugin: 'comments.classify',
      input: { comments: [] },
      options: { model: 'evil' },
      context: { domain_config: { features: { 'comments.classify': { enabled: true } } } },
      selectors: { item_selector: '*' },
      code: 'alert(1)',
    },
    pol
  );
  assert.strictEqual(checked.ok, true);
  assert.deepStrictEqual(Object.keys(checked.value).sort(), ['apply', 'id', 'input', 'plugin']);
  assert.deepStrictEqual(checked.ignored.sort(), ['code', 'context', 'options', 'selectors']);
});

test('bridge enforces the input size cap', () => {
  const pol = LAHBridge.policy({
    bridge: { tampermonkey: true, max_input_bytes: 128 },
    features: { 'comments.classify': { enabled: true } },
  });
  const checked = LAHBridge.sanitize(
    { id: 'r6', plugin: 'comments.classify', input: { text: 'x'.repeat(500) } },
    pol
  );
  assert.strictEqual(checked.ok, false);
  assert.strictEqual(checked.error.code, 'input_too_large');
});

test('bridge apply needs a domain-level opt-in', () => {
  const pol = LAHBridge.policy(boardConfig);
  const denied = LAHBridge.sanitize({ id: 'r7', plugin: 'comments.classify', input: {}, apply: true }, pol);
  assert.strictEqual(denied.ok, false);
  assert.strictEqual(denied.error.code, 'forbidden');
  const allowed = LAHBridge.policy({
    bridge: { tampermonkey: true, allow_apply: true },
    features: { 'comments.classify': { enabled: true } },
  });
  assert.strictEqual(LAHBridge.sanitize({ id: 'r8', plugin: 'comments.classify', input: {}, apply: true }, allowed).ok, true);
});

test('bridge rate limiter blocks bursts', () => {
  let clock = 1000;
  const limiter = new LAHBridge.RateLimiter(3, () => clock);
  assert.strictEqual(limiter.allow(), true);
  assert.strictEqual(limiter.allow(), true);
  assert.strictEqual(limiter.allow(), true);
  assert.strictEqual(limiter.allow(), false);
  clock += 61000;
  assert.strictEqual(limiter.allow(), true, 'the window slides');
});

function fakeTarget() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(name, fn) {
      listeners.set(name, (listeners.get(name) || []).concat(fn));
    },
    removeEventListener(name, fn) {
      listeners.set(name, (listeners.get(name) || []).filter((entry) => entry !== fn));
    },
    fire(name, detail) {
      (listeners.get(name) || []).forEach((fn) => fn({ detail }));
    },
  };
}

test('bridge stays silent on domains that did not opt in', () => {
  const target = fakeTarget();
  const replies = [];
  let submitted = 0;
  LAHBridge.attach({
    target,
    getPolicy: () => LAHBridge.policy({ features: { 'comments.classify': { enabled: true } } }),
    submit: () => {
      submitted += 1;
      return Promise.resolve({ ok: true, output: {} });
    },
    dispatch: (name, detail) => replies.push([name, detail]),
  });
  target.fire(LAHBridge.HELLO_EVENT, '{}');
  target.fire(LAHBridge.REQUEST_EVENT, JSON.stringify({ id: 'r1', plugin: 'comments.classify', input: {} }));
  assert.strictEqual(replies.length, 0, 'no reply at all — the page cannot even detect the hub');
  assert.strictEqual(submitted, 0);
});

test('bridge round trip replies with JSON and forwards data only', async () => {
  const target = fakeTarget();
  const replies = [];
  const seen = [];
  LAHBridge.attach({
    target,
    getPolicy: () => LAHBridge.policy(boardConfig),
    submit: (request) => {
      seen.push(request);
      return Promise.resolve({ ok: true, output: { results: [] }, meta: { duration_ms: 5 } });
    },
    dispatch: (name, detail) => replies.push([name, detail]),
  });
  target.fire(LAHBridge.HELLO_EVENT, '{}');
  assert.strictEqual(replies[0][0], LAHBridge.READY_EVENT);
  assert.deepStrictEqual(JSON.parse(replies[0][1]).plugins, ['comments.classify']);

  target.fire(
    LAHBridge.REQUEST_EVENT,
    JSON.stringify({ id: 'r9', plugin: 'comments.classify', input: { comments: [{ id: 'c0', text: 'hi' }] }, options: { model: 'x' } })
  );
  await new Promise((resolve) => setTimeout(resolve, 0));
  const answer = JSON.parse(replies[replies.length - 1][1]);
  assert.strictEqual(answer.id, 'r9');
  assert.strictEqual(answer.ok, true);
  assert.deepStrictEqual(answer.output, { results: [] });
  assert.deepStrictEqual(answer.meta.ignored_keys, ['options']);
  assert.deepStrictEqual(Object.keys(seen[0]).sort(), ['apply', 'id', 'input', 'plugin']);
});

test('bridge reports task failures as error envelopes', async () => {
  const target = fakeTarget();
  const replies = [];
  LAHBridge.attach({
    target,
    getPolicy: () => LAHBridge.policy(boardConfig),
    submit: () => Promise.resolve({ ok: false, error: { code: 'model_unavailable', message: 'no model' } }),
    dispatch: (name, detail) => replies.push([name, detail]),
  });
  target.fire(LAHBridge.REQUEST_EVENT, JSON.stringify({ id: 'r10', plugin: 'comments.classify', input: {} }));
  await new Promise((resolve) => setTimeout(resolve, 0));
  const answer = JSON.parse(replies[0][1]);
  assert.strictEqual(answer.ok, false);
  assert.strictEqual(answer.error.code, 'model_unavailable');
});

test('manifest wires the bridge and the new commands', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(EXT, 'manifest.json'), 'utf8'));
  assert.ok(manifest.content_scripts[0].js.includes('content/bridge.js'));
  assert.ok(manifest.permissions.includes('menus'));
  ['lah-dry-run', 'lah-pick-selector', 'lah-run-feature-1'].forEach((command) => {
    assert.ok(manifest.commands[command], `missing command ${command}`);
  });
});

(async () => {
  for (const [name, fn] of queue) {
    try {
      await fn();
      passed += 1;
      console.log(`  \x1b[32mPASS\x1b[0m ${name}`);
    } catch (error) {
      console.log(`  \x1b[31mFAIL\x1b[0m ${name} — ${error.message}`);
      process.exitCode = 1;
    }
  }
  console.log(`\n${passed}/${queue.length} extension checks passed`);
})();
