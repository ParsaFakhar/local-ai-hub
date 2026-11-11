"""Local AI Hub core engine.

A local, extensible AI automation runtime.  The engine knows nothing about
Firefox: it receives *tasks* through a universal protocol, routes them to
plugins, manages the local model runtime (Ollama) and returns results.

Interfaces (Firefox native messaging, CLI, HTTP API, ...) are thin adapters
around :class:`hub.engine.Engine`.
"""

__version__ = "0.2.0"
PROTOCOL_VERSION = "1.0"
APP_NAME = "Local AI Hub"
NATIVE_HOST_NAME = "local_ai_hub"
EXTENSION_ID = "local-ai-hub@localaihub.dev"

__all__ = [
    "__version__",
    "PROTOCOL_VERSION",
    "APP_NAME",
    "NATIVE_HOST_NAME",
    "EXTENSION_ID",
]
