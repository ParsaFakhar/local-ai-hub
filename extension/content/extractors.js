/**
 * Local AI Hub — generic selector-driven extraction.
 *
 * A feature config describes *what* to read from the page:
 *
 *   root_selector : optional container(s)
 *   item_selector : the repeating element (one task item per match)
 *   fields        : { name: {selector?, attr?, as?, transform?, max_length?} }
 *   input         : { pluginInputKey: "@items" | "@page.title" | literal }
 *
 * Nothing here executes page code or evaluates strings: it only reads
 * selectors and attributes supplied by the *user's* configuration.
 */
'use strict';

(function (global) {
  function textOf(element) {
    return (element.textContent || '').replace(/\s+/g, ' ').trim();
  }

  function absoluteUrl(value) {
    if (!value) return '';
    try {
      return new URL(value, document.baseURI).href;
    } catch (_) {
      return String(value);
    }
  }

  function applyTransform(value, transform, attr) {
    if (value == null) return '';
    let out = value;
    switch (transform) {
      case 'basename':
        out = String(out).split('?')[0].split('#')[0].split('/').filter(Boolean).pop() || '';
        break;
      case 'lowercase':
        out = String(out).toLowerCase();
        break;
      case 'trim':
        out = String(out).trim();
        break;
      case 'number':
        out = Number(String(out).replace(/[^\d.-]/g, '')) || 0;
        break;
      case 'domain':
        try {
          out = new URL(absoluteUrl(out)).hostname;
        } catch (_) {
          out = '';
        }
        break;
      default:
        break;
    }
    if (attr === 'text' && typeof out === 'string') out = out.replace(/\s+/g, ' ').trim();
    return out;
  }

  function readField(itemElement, rawSpec) {
    const spec = typeof rawSpec === 'string' ? { attr: rawSpec } : rawSpec || {};
    const attr = spec.attr || 'text';
    let target = itemElement;
    if (spec.selector) {
      target = itemElement.querySelector(spec.selector);
      if (!target) return spec.as === 'image' ? { __lahImage: '', missing: true } : '';
    }

    let value;
    if (attr === 'text') {
      value = textOf(target);
    } else if (attr === 'value') {
      value = target.value != null ? String(target.value) : '';
    } else {
      value = target.getAttribute(attr) || '';
      if (attr === 'src' || attr === 'href' || attr === 'data-src') value = absoluteUrl(value);
    }

    if (spec.as === 'image') {
      return {
        __lahImage: absoluteUrl(value),
        max_size: spec.max_size || null,
        quality: spec.quality || null,
      };
    }
    value = applyTransform(value, spec.transform, attr);
    if (spec.max_length && typeof value === 'string') value = value.slice(0, spec.max_length);
    return value;
  }

  /** Collect items; each gets a stable data-lah-id used to apply results back. */
  function extractItems(feature, pluginId, options) {
    const opts = options || {};
    const errors = [];
    const itemSelector = feature.item_selector;
    if (!itemSelector) return { items: [], errors: ['feature has no item_selector'] };

    let roots = [document];
    if (feature.root_selector) {
      try {
        roots = Array.from(document.querySelectorAll(feature.root_selector));
      } catch (error) {
        return { items: [], errors: [`invalid root_selector: ${error.message}`] };
      }
      if (!roots.length) errors.push(`root_selector matched nothing: ${feature.root_selector}`);
    }

    const maxItems = feature.max_items || opts.maxItems || 200;
    const prefix = feature.id_prefix || String(pluginId || 'item').split('.')[0];
    const fields = feature.fields || {};
    const seen = new Set();
    const items = [];

    roots.forEach((root) => {
      let matches = [];
      try {
        matches = Array.from(root.querySelectorAll(itemSelector));
      } catch (error) {
        errors.push(`invalid item_selector: ${error.message}`);
        return;
      }
      matches.forEach((element) => {
        if (seen.has(element) || items.length >= maxItems) return;
        seen.add(element);
        const id = `${prefix}-${items.length}`;
        try {
          element.setAttribute('data-lah-id', id);
        } catch (_) {
          /* read-only element */
        }
        const data = { id };
        if (Object.keys(fields).length) {
          Object.keys(fields).forEach((name) => {
            data[name] = readField(element, fields[name]);
          });
        } else {
          data.text = textOf(element);
        }
        items.push({ id, element, data });
      });
    });

    return { items, errors };
  }

  /** Download + downscale image fields through the background page. */
  async function resolveImages(items, defaults) {
    const jobs = [];
    items.forEach((item) => {
      Object.keys(item.data).forEach((name) => {
        const value = item.data[name];
        if (value && typeof value === 'object' && '__lahImage' in value) {
          jobs.push({ item, name, spec: value });
        }
      });
    });

    const errors = [];
    const concurrency = 3;
    let cursor = 0;
    async function worker() {
      while (cursor < jobs.length) {
        const job = jobs[cursor++];
        const url = job.spec.__lahImage;
        if (!url) {
          job.item.data[job.name] = '';
          errors.push(`${job.item.id}: no image url`);
          continue;
        }
        try {
          const reply = await LAH.send(LAH.KIND.FETCH_IMAGE, {
            url,
            maxSize: job.spec.max_size || (defaults && defaults.imageMaxSize),
            quality: job.spec.quality || (defaults && defaults.imageQuality),
          });
          if (reply && reply.ok) {
            job.item.data[job.name] = reply.image.data_base64;
            job.item.imageMeta = Object.assign({}, reply.image, { data_base64: undefined });
          } else {
            job.item.data[job.name] = '';
            errors.push(`${job.item.id}: ${(reply && reply.error) || 'image fetch failed'}`);
          }
        } catch (error) {
          job.item.data[job.name] = '';
          errors.push(`${job.item.id}: ${LAH.errorMessage(error)}`);
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(concurrency, jobs.length) }, worker));
    return { imageCount: jobs.length, errors };
  }

  function pageFacts() {
    return {
      title: document.title || '',
      url: location.href,
      domain: location.hostname,
      text: textOf(document.body).slice(0, 4000),
      lang: document.documentElement.lang || '',
    };
  }

  /** Map extracted items onto the plugin's input schema. */
  function buildInput(feature, items, page) {
    const mapping = feature.input;
    const dataList = items.map((item) => item.data);
    if (!mapping || typeof mapping !== 'object') return { items: dataList };

    const output = {};
    Object.keys(mapping).forEach((key) => {
      const value = mapping[key];
      if (value === '@items') {
        output[key] = dataList;
      } else if (value === '@count') {
        output[key] = items.length;
      } else if (typeof value === 'string' && value.startsWith('@page.')) {
        output[key] = page[value.slice(6)] || '';
      } else if (typeof value === 'string' && value.startsWith('@selector:')) {
        const element = document.querySelector(value.slice(10));
        output[key] = element ? textOf(element) : '';
      } else {
        output[key] = value;
      }
    });
    return output;
  }

  global.LAHExtract = { extractItems, resolveImages, buildInput, pageFacts, textOf };
})(globalThis);
