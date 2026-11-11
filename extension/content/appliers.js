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
 *   fill       {value}                    -> form field value, opt-in per feature
 *
 * Every action is first turned into a *plan* (a plain description of the
 * change) and only then executed.  Dry-run mode stops after planning, so the
 * preview and the real application share exactly the same validation path.
 */
'use strict';

(function (global) {
  const ALLOWED_ACTIONS = new Set(['badge', 'class', 'attribute', 'hide', 'show', 'text', 'fill']);
  const BADGE_FLAG = 'data-lah-badge';
  const CLASS_FLAG = 'data-lah-classes';
  const ATTR_FLAG = 'data-lah-attrs';
  const FILL_FLAG = 'data-lah-filled';

  /** Original values of fields we filled, so clearAll() can restore them. */
  const filledOriginals = new WeakMap();

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

  function fieldType(element) {
    return String(element.type || element.getAttribute('type') || '').toLowerCase();
  }

  function isFillable(element) {
    const tag = element.tagName;
    if (tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT') return false;
    const type = fieldType(element);
    // Never touch credentials, hidden state, or anything that submits.
    if (['password', 'hidden', 'submit', 'button', 'reset', 'file', 'image'].includes(type)) return false;
    if (element.disabled || element.readOnly) return false;
    if (element.getAttribute && element.getAttribute('autocomplete') === 'cc-number') return false;
    return true;
  }

  /**
   * Validate + render one action into a plain plan object, or null if the
   * action is not allowed / does not match.  Pure: never touches the DOM.
   */
  function planAction(element, action, record, context) {
    switch (action.action) {
      case 'badge': {
        const tone = String(record.label || '').toLowerCase().replace(/[^a-z0-9-]/g, '');
        return {
          action: 'badge',
          text: template(action.text || '{label}', record).slice(0, 300),
          title: action.title ? template(action.title, record).slice(0, 500) : '',
          tone,
          target: action.target || null,
        };
      }
      case 'class': {
        const map = action.map || {};
        const key = String(record[action.field || 'label'] || '');
        const className = map[key];
        if (!className || !/^[A-Za-z0-9_-]+$/.test(className)) return null;
        return { action: 'class', className };
      }
      case 'attribute': {
        const name = String(action.name || '');
        const allowed = /^data-[a-z0-9-]+$/.test(name) || name === 'title' || name === 'aria-label';
        if (!allowed) return null;
        return { action: 'attribute', name, value: template(action.value, record).slice(0, 500) };
      }
      case 'hide':
      case 'show': {
        if (!matches(action.when, record)) return null;
        return { action: action.action };
      }
      case 'text': {
        if (!context.allowTextReplace) return null;
        const selector = action.selector || null;
        if (selector && !element.querySelector(selector)) return null;
        return { action: 'text', selector, value: template(action.value, record).slice(0, 2000) };
      }
      case 'fill': {
        if (!context.allowFormFill) return null;
        const target = action.selector ? element.querySelector(action.selector) : element;
        if (!target || !isFillable(target)) return null;
        const value = template(action.value == null ? '{value}' : action.value, record).slice(0, 2000);
        if (!value) return null;
        return { action: 'fill', selector: action.selector || null, value };
      }
      default:
        return null;
    }
  }

  /** Execute a plan produced by planAction.  Returns true when it changed something. */
  function executePlan(element, plan, context) {
    switch (plan.action) {
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
        badge.textContent = plan.text;
        if (plan.title) badge.title = plan.title;
        if (plan.tone) badge.setAttribute('data-tone', plan.tone);
        if (!existing) placeBadge(element, badge, plan.target);
        return true;
      }
      case 'class':
        element.classList.add(plan.className);
        remember(element, CLASS_FLAG, plan.className);
        return true;
      case 'attribute':
        element.setAttribute(plan.name, plan.value);
        remember(element, ATTR_FLAG, plan.name);
        return true;
      case 'hide':
      case 'show':
        element.classList.toggle('lah-hidden', plan.action === 'hide');
        remember(element, CLASS_FLAG, 'lah-hidden');
        return true;
      case 'text': {
        const target = plan.selector ? element.querySelector(plan.selector) : element;
        if (!target) return false;
        target.textContent = plan.value;
        return true;
      }
      case 'fill': {
        const target = plan.selector ? element.querySelector(plan.selector) : element;
        if (!target || !isFillable(target)) return false;
        if (!filledOriginals.has(target)) {
          filledOriginals.set(target, target.value == null ? '' : String(target.value));
        }
        target.value = plan.value;
        if (target.setAttribute) target.setAttribute(FILL_FLAG, '1');
        // Let the page's own validation notice the change; no submit, ever.
        try {
          if (typeof Event === 'function' && target.dispatchEvent) {
            target.dispatchEvent(new Event('input', { bubbles: true }));
          }
        } catch (_) {
          /* the stub DOM has no Event */
        }
        return true;
      }
      default:
        return false;
    }
  }

  function resultsOf(output, config) {
    const path = config.results_path || 'results';
    const results = path.split('.').reduce((value, key) => (value == null ? value : value[key]), output);
    return Array.isArray(results) ? results : [];
  }

  function allowedActions(config) {
    return (config.actions || []).filter((action) => action && ALLOWED_ACTIONS.has(action.action));
  }

  /**
   * Build the change set without touching the DOM (dry run / review preview).
   * Returns {plans: [{id, record, changes: [plan]}], skipped, total}
   */
  function planResults(output, itemsById, spec, context) {
    const config = spec || {};
    const list = resultsOf(output, config);
    const actions = allowedActions(config);
    const matchBy = config.match_by || 'id';
    const plans = [];
    const skipped = [];

    list.forEach((record) => {
      if (!record || typeof record !== 'object') return;
      const key = String(record[matchBy] == null ? '' : record[matchBy]);
      const item = itemsById.get(key);
      if (!item) {
        skipped.push(key);
        return;
      }
      const changes = [];
      actions.forEach((action) => {
        try {
          const plan = planAction(item.element, action, record, context);
          if (plan) changes.push(plan);
        } catch (error) {
          skipped.push(`${key}: ${error.message}`);
        }
      });
      plans.push({ id: key, record, element: item.element, changes });
    });

    return { plans, skipped, total: list.length, matched: plans.length };
  }

  /** Apply a plugin result set to the elements it was extracted from. */
  function applyResults(output, itemsById, spec, context) {
    const planned = planResults(output, itemsById, spec, context);
    let applied = 0;
    const skipped = planned.skipped.slice();
    planned.plans.forEach((entry) => {
      entry.changes.forEach((plan) => {
        try {
          if (executePlan(entry.element, plan, context)) applied += 1;
        } catch (error) {
          skipped.push(`${entry.id}: ${error.message}`);
        }
      });
    });
    return { applied, matched: planned.matched, total: planned.total, skipped };
  }

  /** Apply a previously computed (and possibly user-filtered) plan set. */
  function applyPlans(plans, context) {
    let applied = 0;
    const skipped = [];
    (plans || []).forEach((entry) => {
      (entry.changes || []).forEach((plan) => {
        try {
          if (executePlan(entry.element, plan, context)) applied += 1;
        } catch (error) {
          skipped.push(`${entry.id}: ${error.message}`);
        }
      });
    });
    return { applied, matched: (plans || []).length, skipped };
  }

  /** Human-readable one-line description of a planned change (preview UI). */
  function describePlan(plan) {
    switch (plan.action) {
      case 'badge':
        return `badge “${plan.text}”`;
      case 'class':
        return `class ${plan.className}`;
      case 'attribute':
        return `${plan.name}="${plan.value}"`;
      case 'hide':
        return 'hide element';
      case 'show':
        return 'show element';
      case 'text':
        return `replace text with “${plan.value.slice(0, 60)}”`;
      case 'fill':
        return `fill field with “${plan.value.slice(0, 60)}”`;
      default:
        return plan.action;
    }
  }

  /** Undo every DOM change the hub made (badges, classes, attributes, fills). */
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
    scope.querySelectorAll(`[${FILL_FLAG}]`).forEach((element) => {
      if (filledOriginals.has(element)) element.value = filledOriginals.get(element);
      element.removeAttribute(FILL_FLAG);
    });
    scope.querySelectorAll('[data-lah-id]').forEach((element) => element.removeAttribute('data-lah-id'));
  }

  global.LAHApply = {
    applyResults,
    planResults,
    applyPlans,
    describePlan,
    planAction,
    executePlan,
    clearAll,
    template,
    ALLOWED_ACTIONS: Array.from(ALLOWED_ACTIONS),
  };
})(globalThis);
