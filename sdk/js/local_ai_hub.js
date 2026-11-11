/**
 * Local AI Hub — JavaScript plugin SDK (Node.js, no dependencies).
 *
 *   const { Plugin } = require('local_ai_hub');
 *
 *   Plugin(async (task, hub) => {
 *     return { greeting: `Hello, ${task.input.name || 'world'}!` };
 *   }).run();
 *
 * task : { id, input, config, context, models, limits, pluginId,
 *          stateDir, statePath(name) }
 * hub  : log(), progress(), generate(), chat(), json()
 *        every model call accepts { onToken } for streaming partial output
 *
 * The engine owns the model runtime; plugins never call Ollama directly.
 */

'use strict';

const readline = require('readline');
const fs = require('fs');
const path = require('path');

/** Writable per-plugin state directory (LOCAL_AI_HUB_DATA is set by the engine). */
function stateDir(pluginId) {
  const base = process.env.LOCAL_AI_HUB_DATA;
  const root = base ? base : path.join(process.env.HOME || '.', '.local-ai-hub');
  const safe = String(pluginId || 'plugin').replace(/[^a-zA-Z0-9._-]/g, '_');
  const directory = path.join(root, 'plugin-state', safe);
  fs.mkdirSync(directory, { recursive: true });
  return directory;
}

class LLMError extends Error {}
class PluginError extends Error {
  constructor(message, code = 'plugin_failed', details = null) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

function parseJSON(text) {
  const raw = (text || '').trim();
  if (!raw) throw new LLMError('model returned an empty response');
  try {
    return JSON.parse(raw);
  } catch (_) {
    /* fall through */
  }
  const fenced = raw.match(/```(?:json)?\s*([\s\S]+?)```/);
  if (fenced) {
    try {
      return JSON.parse(fenced[1].trim());
    } catch (_) {
      /* fall through */
    }
  }
  for (const [open, close] of [['{', '}'], ['[', ']']]) {
    const start = raw.indexOf(open);
    const end = raw.lastIndexOf(close);
    if (start !== -1 && end > start) {
      try {
        return JSON.parse(raw.slice(start, end + 1));
      } catch (_) {
        /* keep trying */
      }
    }
  }
  throw new LLMError(`model did not return valid JSON: ${raw.slice(0, 200)}`);
}

class Hub {
  constructor(write) {
    this._write = write;
    this._counter = 0;
    this._pending = new Map();
    this.lastModel = null;
    this.mock = false;
  }

  _send(message) {
    this._write(JSON.stringify(message) + '\n');
  }

  _resolveToken(message) {
    const pending = this._pending.get(message.id);
    if (!pending || typeof pending.onToken !== 'function') return;
    try {
      pending.onToken(message.text || '', message.index);
    } catch (_) {
      /* a broken plugin callback must not kill the task */
    }
  }

  _resolveResponse(message) {
    const pending = this._pending.get(message.id);
    if (!pending) return;
    this._pending.delete(message.id);
    if (!message.ok) {
      pending.reject(new LLMError((message.error && message.error.message) || 'model call failed'));
      return;
    }
    const result = message.result || {};
    this.lastModel = result.model || null;
    this.mock = Boolean(result.mock);
    pending.resolve(result);
  }

  log(message, level = 'info') {
    this._send({ type: 'log', level, message: String(message) });
  }

  progress(value, message = null) {
    this._send({ type: 'progress', value, message });
  }

  _call(method, params, onToken = null) {
    const id = String(++this._counter);
    const payload = typeof onToken === 'function' ? Object.assign({}, params, { stream: true }) : params;
    return new Promise((resolve, reject) => {
      this._pending.set(id, { resolve, reject, onToken });
      this._send({ type: 'llm.request', id, method, params: payload });
    });
  }

  async generate(prompt, { model = null, system = null, images = null, options = null, onToken = null } = {}) {
    const params = { prompt, model, system, options };
    if (images && images.length) params.images = images;
    const result = await this._call('generate', params, onToken);
    return result.text || '';
  }

  async chat(messages, { model = null, options = null, onToken = null } = {}) {
    const result = await this._call('chat', { messages, model, options }, onToken);
    return result.text || '';
  }

  async json(prompt, { schema = null, model = null, system = null, images = null, options = null, onToken = null } = {}) {
    const params = { prompt, model, system, options, format: schema || 'json' };
    if (images && images.length) params.images = images;
    const result = await this._call('generate', params, onToken);
    return parseJSON(result.text || '');
  }
}

function makeTask(payload) {
  const plugin = payload.plugin || {};
  return {
    id: payload.task_id || '-',
    payload,
    input: payload.input || {},
    config: payload.config || {},
    context: payload.context || {},
    models: payload.models || {},
    limits: payload.limits || {},
    pluginId: plugin.id || 'unknown',
    pluginVersion: plugin.version || '0',
    option(key, fallback = null) {
      return Object.prototype.hasOwnProperty.call(this.config, key) ? this.config[key] : fallback;
    },
    get stateDir() {
      return stateDir(plugin.id || 'unknown');
    },
    statePath(name) {
      const raw = String(name || '');
      if (path.isAbsolute(raw)) return raw;
      const directory = stateDir(plugin.id || 'unknown');
      const resolved = path.resolve(directory, raw);
      if (resolved !== directory && !resolved.startsWith(directory + path.sep)) {
        throw new PluginError(`state file ${raw} escapes the plugin state directory`, 'invalid_input');
      }
      fs.mkdirSync(path.dirname(resolved), { recursive: true });
      return resolved;
    },
  };
}

class PluginRunner {
  constructor(handler) {
    this.handler = handler;
  }

  run() {
    const write = (line) => process.stdout.write(line);
    const hub = new Hub(write);
    const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
    let invoked = false;

    const finish = (message) => {
      rl.close();
      process.stdout.write(JSON.stringify(message) + '\n', () => process.exit(0));
    };

    rl.on('line', (line) => {
      const trimmed = line.trim();
      if (!trimmed) return;
      let message;
      try {
        message = JSON.parse(trimmed);
      } catch (_) {
        return;
      }
      if (message.type === 'llm.token') {
        hub._resolveToken(message);
        return;
      }
      if (message.type === 'llm.response') {
        hub._resolveResponse(message);
        return;
      }
      if (message.type !== 'invoke' || invoked) return;
      invoked = true;
      const task = makeTask(message);
      Promise.resolve()
        .then(() => this.handler(task, hub))
        .then((output) => finish({ type: 'result', ok: true, output: output == null ? {} : output }))
        .catch((error) =>
          finish({
            type: 'result',
            ok: false,
            error: {
              code: error && error.code ? error.code : 'plugin_failed',
              message: String((error && error.message) || error),
              details: (error && error.details) || (error && error.stack) || null,
            },
          })
        );
    });

    rl.on('close', () => {
      if (!invoked) process.exit(0);
    });
  }
}

/** Factory: ``Plugin(handler).run()`` (also works with ``new``). */
function Plugin(handler) {
  return new PluginRunner(handler);
}

module.exports = { Plugin, PluginRunner, Hub, LLMError, PluginError, parseJSON, stateDir };
