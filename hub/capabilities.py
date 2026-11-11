"""Per-plugin capability enforcement.

A manifest declares what a plugin intends to use::

    "capabilities": ["llm.generate", "llm.structured"]

The engine checks every model call against that declaration *before* it
reaches the model runtime.  This is a least-privilege guard rail, not a
sandbox: plugins are still trusted local programs (see docs/SECURITY.md).
What it does buy you is that a plugin cannot silently reach a capability it
never advertised — a compromised or careless plugin that only asked to
classify text cannot start sending images to a vision model.

Capability vocabulary
---------------------

===================  ==========================================================
``llm.generate``     text inference via ``generate`` (and ``chat``)
``llm.chat``         chat inference via ``chat``
``llm.vision``       may pass ``images``; implies model access
``llm.structured``   may pass ``format`` (``"json"`` or a JSON schema)
===================  ==========================================================

``llm.vision`` implies model access so a vision-only plugin (``images.tag``)
does not also have to declare ``llm.generate``.

Unknown capability names are reported at discovery time but do not block a
plugin: a newer plugin must still load on an older engine.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Everything the engine knows how to enforce today.
KNOWN_CAPABILITIES = frozenset(
    {"llm.generate", "llm.chat", "llm.vision", "llm.structured"}
)

#: Capabilities that grant access to the model at all.
_MODEL_ACCESS = frozenset({"llm.generate", "llm.chat", "llm.vision"})


def unknown(capabilities: Iterable[str]) -> list[str]:
    """Capability names this engine does not recognise (informational)."""
    return sorted({str(name) for name in capabilities or ()} - KNOWN_CAPABILITIES)


def denial(
    capabilities: Iterable[str], method: str, params: dict[str, Any] | None
) -> str | None:
    """Return a human-readable reason to deny this call, or ``None`` to allow.

    ``method`` is ``generate`` or ``chat``; ``params`` is the plugin's request
    payload.  Pure function: no I/O, trivially unit-testable.
    """
    granted = {str(name) for name in capabilities or ()}
    params = params or {}

    if not granted & _MODEL_ACCESS:
        return (
            "plugin declares no model capability "
            "(add 'llm.generate', 'llm.chat' or 'llm.vision' to its manifest)"
        )

    if method == "chat" and not granted & {"llm.chat", "llm.generate", "llm.vision"}:
        return "chat requires the 'llm.chat' capability"

    if method == "generate" and not granted & {"llm.generate", "llm.vision"}:
        return "generate requires the 'llm.generate' capability"

    if params.get("images") and "llm.vision" not in granted:
        return "passing images requires the 'llm.vision' capability"

    if params.get("format") and "llm.structured" not in granted:
        return "structured output (format) requires the 'llm.structured' capability"

    # A chat message may carry images too.
    if method == "chat" and "llm.vision" not in granted:
        for message in params.get("messages") or ():
            if isinstance(message, dict) and message.get("images"):
                return "passing images requires the 'llm.vision' capability"

    return None