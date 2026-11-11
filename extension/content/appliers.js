/**
 * Local AI Hub — result application (strict allowlist).
 *
 * Model output is DATA.  It can only ever reach the page through these
 * actions, and only as textContent / class names / allowlisted attributes.
 * There is no innerHTML, no eval, no script injection, no URL navigation.
 *
 *   badge      {text}                     -> hub-owned <span> label
 *   class      {map: {label: className}}  -> add a CSS class
 *   attribute  {name, value}              -> data-*, title, aria-label only
 *   hide/show  {when}                     -> toggle a hub CSS class
 *   text       {selector, value}          -> textContent, opt-in per feature
 */
'use strict';

(function (global) {
  const ALLOWED_ACTIONS = new Set(['badge', 'class', 'attribute', 'hide', 'show', 'text']);
  const BADGE_FLAG = 'data-lah-badge';
  const CLASS_FLAG = 'data-lah-classes';
  const ATTR_FLAG = 'data-lah-attrs';

  function stringify(value) {
    if (value == null) return '';
    if (Array.isArray(value)) return value.map(stringify).join(', ');
    if (typeof value === 'object') return JSON.stringify(value).slice(0, 200);
    return String(value);
  }

  function template(text, record) {
    return String(text == null ? '' : text).replace(/\{([a-zA-Z0-9_.]+)\}/g, (_, path) => {
      let value = record;
      for (const part of path.split('.')) {
        if (value == null) return '';
        value = value[part];
      }
      if (typeof value === 'number') value = Math.round(value * 100) / 100;
      return stringify(value);
    });
  }

  function matches(when, record) {
    if (!when) return true;
    const value = record[when.field];
    if (Array.isArray(when.in)) return when.in.map(String).includes(String(value));
    if ('equals' in when) return String(value) === String(when.equals);
    if ('gte' in when) return Number(value) >= Number(when.gte);
    if ('lte' in when) return Number(value) <= Number(when.lte);
    if ('truthy' in when) return Boolean(value) === Boolean(when.truthy);
    return true;
  }

  function remember(element, flag, value) {
    const current = (element.getAttribute(flag) || '').split(' ').filter(Boolean);
    if (!current.includes(value)) current.push(value);
    element.setAttribute(flag, current.join(' '));
  }

  function placeBadge(element, badge, targetSelector) {
    let host = element;
    if (targetSelector) {
      const found = element.querySelector(targetSelector);
      if (found) host = found;
    }
    if (host.tagName === 'IMG' || host.tagName === 'INPUT' || host.tagName === 'BR') {
      host.insertAdjacentElement('afterend', badge);
    } else {
      host.appendChild(badge);
    }
  }

  function applyAction(element, action, record, context) {
    switch (action.action) {
      case 'badge': {
        const selector = `[${BADGE_FLAG}="${context.pluginId}"]`;
        const existing =
          element.querySelector(selector) ||
          (element.nextElementSibling && element.nextElementSibling.matches && element.nextElementSibling.matches(selector)
            ? element.nextElementSibling
            : null);
        const badge = existing || document.createElement('span');
        badge.setAttribute(BADGE_FLAG, context.pluginId);
        badge.className = 'lah-badge';
        badge.textContent = template(action.text || '{label}', record).slice(0, 300);
        if (action.title) badge.title = template(action.title, record).slice(0, 500);
        const tone = String(record.label || '').toLowerCase().replace(/[^a-z0-9-]/g, '');
        if (tone) badge.setAttribute('data-tone', tone);
        if (!existing) placeBadge(element, badge, action.target);
        return true;
      }
      case 'class': {
        const map = action.map || {};
        const key = String(record[action.field || 'label'] || '');
        const className = map[key];
        if (!className || !/^[A-Za-z0-9_-]+$/.test(className)) return false;
        element.classList.add(className);
        remember(element, CLASS_FLAG, className);
        return true;
      }
      case 'attribute': {
        const name = String(action.name || '');
        const allowed = /^data-[a-z0-9-]+$/.test(name) || name === 'title' || name === 'aria-label';
        if (!allowed) return false;
        element.setAttribute(name, template(action.value, record).slice(0, 500));
        remember(element, ATTR_FLAG, name);
        return true;
      }
      case 'hide':
      case 'show': {
        if (!matches(action.when, record)) return false;
        element.classList.toggle('lah-hidden', action.action === 'hide');
        remember(element, CLASS_FLAG, 'lah-hidden');
        return true;
      }
      case 'text': {
        if (!context.allowTextReplace) return false;
        const target = action.selector ? element.querySelector(action.selector) : element;
        if (!target) return false;
        target.textContent = template(action.value, record).slice(0, 2000);
        return true;
      }
      default:
        return false;
    }
  }

  /** Apply a plugin result set to the elements it was extracted from. */
  function applyResults(output, itemsById, spec, context) {
    const config = spec || {};
    const path = config.results_path || 'results';
    const results = path.split('.').reduce((value, key) => (value == null ? value : value[key]), output);
    const list = Array.isArray(results) ? results : [];
    const actions = (config.actions || []).filter((action) => action && ALLOWED_ACTIONS.has(action.action));
    const matchBy = config.match_by || 'id';
    let applied = 0;
    const skipped = [];

    list.forEach((record) => {
      if (!record || typeof record !== 'object') return;
      const key = String(record[matchBy] == null ? '' : record[matchBy]);
      const item = itemsById.get(key);
      if (!item) {
        skipped.push(key);
        return;
      }
      actions.forEach((action) => {
        try {
          if (applyAction(item.element, action, record, context)) applied += 1;
        } catch (error) {
          skipped.push(`${key}: ${error.message}`);
        }
      });
    });

    return { applied, matched: list.length - skipped.length, total: list.length, skipped };
  }

  /** Undo every DOM change the hub made (badges, classes, attributes). */
  function clearAll(root) {
    const scope = root || document;
    scope.querySelectorAll(`[${BADGE_FLAG}]`).forEach((node) => node.remove());
    scope.querySelectorAll(`[${CLASS_FLAG}]`).forEach((element) => {
      (element.getAttribute(CLASS_FLAG) || '').split(' ').filter(Boolean).forEach((className) => {
        element.classList.remove(className);
      });
      element.removeAttribute(CLASS_FLAG);
    });
    scope.querySelectorAll(`[${ATTR_FLAG}]`).forEach((element) => {
      (element.getAttribute(ATTR_FLAG) || '').split(' ').filter(Boolean).forEach((name) => {
        if (name !== CLASS_FLAG && name !== BADGE_FLAG) element.removeAttribute(name);
      });
      element.removeAttribute(ATTR_FLAG);
    });
    scope.querySelectorAll('[data-lah-id]').forEach((element) => element.removeAttribute('data-lah-id'));
  }

  global.LAHApply = { applyResults, clearAll, template, ALLOWED_ACTIONS: Array.from(ALLOWED_ACTIONS) };
})(globalThis);
