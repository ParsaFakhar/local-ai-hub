#!/usr/bin/env node
/**
 * hello.javascript — reference Node.js plugin.
 * Mirrors hello.python so both runtimes can be compared side by side.
 */

'use strict';

const os = require('os');
const { Plugin } = require('local_ai_hub');

Plugin(async (task, hub) => {
  const name = task.input.name || 'world';
  hub.log(`greeting ${JSON.stringify(name)} from Node ${process.version}`);
  hub.progress(0.5, 'building greeting');

  const template = task.option('greeting_template', 'Hello, {name}!');
  const output = {
    greeting: template.replace(/\{name\}/g, name),
    runtime: 'javascript',
    node_version: process.version,
    platform: os.platform(),
    plugin: task.pluginId,
    plugin_version: task.pluginVersion,
    context: {
      domain: task.context.domain || null,
      interface: task.context.interface || 'unknown',
    },
    received: task.input,
    effective_config: task.config,
  };

  if (task.input.use_llm || task.option('use_llm', false)) {
    hub.progress(0.7, 'asking the local model');
    output.model_text = await hub.generate(`Write one short, friendly sentence greeting ${name}.`, {
      model: task.option('model'),
    });
    output.model = hub.lastModel;
    output.mock_model = hub.mock;
  }

  hub.progress(1, 'done');
  return output;
}).run();
