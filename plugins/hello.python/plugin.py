#!/usr/bin/env python3
"""hello.python — reference Python plugin.

Demonstrates: input schema defaults, config merging, logging, progress,
optional LLM access through the engine, structured output.
"""

from __future__ import annotations

import platform
import sys

from local_ai_hub import Plugin


def main(task, hub):
    name = task.input.get("name") or "world"
    hub.log(f"greeting {name!r} from Python {platform.python_version()}")
    hub.progress(0.5, "building greeting")

    template = task.option("greeting_template", "Hello, {name}!")
    output = {
        "greeting": template.format(name=name),
        "runtime": "python",
        "python_version": sys.version.split()[0],
        "platform": platform.system().lower(),
        "plugin": task.plugin_id,
        "plugin_version": task.plugin_version,
        "context": {
            "domain": task.context.get("domain") or None,
            "interface": task.context.get("interface") or "unknown",
        },
        "received": task.input,
        "effective_config": task.config,
    }

    if task.input.get("use_llm") or task.option("use_llm", False):
        hub.progress(0.7, "asking the local model")
        output["model_text"] = hub.generate(
            f"Write one short, friendly sentence greeting {name}.",
            model=task.option("model"),
        )
        output["model"] = hub.last_model
        output["mock_model"] = hub.mock

    hub.progress(1.0, "done")
    return output


if __name__ == "__main__":
    Plugin(main).run()
