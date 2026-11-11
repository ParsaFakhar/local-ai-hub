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

for (const file of ['lib/protocol.js', 'content/appliers.js']) {
  vm.runInContext(fs.readFileSync(path.join(EXT, file), 'utf8'), sandbox, { filename: file });
}

const { LAH, LAHApply } = sandbox;

// ---------------------------------------------------------------- tests
let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  \x1b[32mPASS\x1b[0m ${name}`);
  } catch (error) {
    console.log(`  \x1b[31mFAIL\x1b[0m ${name} — ${error.message}`);
    process.exitCode = 1;
  }
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

console.log(`\n${passed} extension checks passed`);
