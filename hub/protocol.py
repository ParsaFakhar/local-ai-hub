"""Universal task protocol (v1.0).

Every interface speaks the same JSON envelopes.

Request::

    {"protocol":"1.0","id":"req-1","type":"task",
     "plugin":"comments.classify",
     "input":{"comments":[{"id":"c1","text":"..."}]},
     "options":{"labels":["ok","spam"]},
     "context":{"domain":"example-board.com","url":"https://...","interface":"firefox"}}

Result::

    {"protocol":"1.0","id":"req-1","type":"result","ok":true,
     "output":{...},"meta":{"plugin":"...","model":"...","duration_ms":812}}

Error::

    {"protocol":"1.0","id":"req-1","type":"result","ok":false,
     "error":{"code":"plugin_failed","message":"..."}}

Event (unsolicited, may arrive before the result)::

    {"protocol":"1.0","type":"event","event":"progress","id":"req-1","data":{...}}

Control types: ``ping``, ``status``, ``plugins.list``, ``domains.get``,
``domains.list``, ``models.list``, ``models.unload``, ``config.get``,
``tasks.list``, ``tasks.get``, ``task.cancel``, ``cache.clear``, ``shutdown``.
"""

from __future__ import annotations

import json
import struct
import time
import uuid
from typing import Any, BinaryIO

PROTOCOL_VERSION = "1.0"

# Firefox native messaging limits: host -> browser 1 MiB per message.
MAX_OUTGOING_BYTES = 1024 * 1024
MAX_INCOMING_BYTES = 64 * 1024 * 1024

CONTROL_TYPES = {
    "ping",
    "status",
    "plugins.list",
    "domains.get",
    "domains.list",
    "models.list",
    "models.unload",
    "config.get",
    "tasks.list",
    "tasks.get",
    "task.cancel",
    "cache.clear",
    "shutdown",
}

#: Control types that must stay responsive while a long task is running.
#: The native host handles these inline instead of queueing them behind tasks.
INLINE_CONTROL_TYPES = {
    "ping",
    "status",
    "shutdown",
    "task.cancel",
    "tasks.list",
    "tasks.get",
}


class ErrorCode:
    BAD_REQUEST = "bad_request"
    UNKNOWN_TYPE = "unknown_type"
    UNKNOWN_PLUGIN = "unknown_plugin"
    PLUGIN_DISABLED = "plugin_disabled"
    INVALID_INPUT = "invalid_input"
    INPUT_TOO_LARGE = "input_too_large"
    PLUGIN_FAILED = "plugin_failed"
    PLUGIN_TIMEOUT = "plugin_timeout"
    RUNTIME_MISSING = "runtime_missing"
    MODEL_ERROR = "model_error"
    MODEL_UNAVAILABLE = "model_unavailable"
    INTERNAL_ERROR = "internal_error"
    CAPABILITY_DENIED = "capability_denied"
    TASK_CANCELLED = "task_cancelled"
    UNKNOWN_TASK = "unknown_task"
    MESSAGE_TOO_LARGE = "message_too_large"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"


#: Interfaces that relay requests originating inside a webpage.  These are the
#: ones ``security.allow_page_supplied_config`` governs: they may submit input,
#: never configuration.  Local interfaces (firefox background, cli, http, your
#: own Python program) are trusted because they are the user's own programs.
PAGE_INTERFACES = {"tampermonkey", "userscript", "page", "web"}


def new_id(prefix: str = "task") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def now_ms() -> int:
    return int(time.time() * 1000)


def make_result(request_id: str, output: Any, meta: dict | None = None) -> dict:
    return {
        "protocol": PROTOCOL_VERSION,
        "id": request_id,
        "type": "result",
        "ok": True,
        "output": output,
        "meta": meta or {},
    }


def make_error(request_id: str, code: str, message: str, details: Any = None) -> dict:
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return {
        "protocol": PROTOCOL_VERSION,
        "id": request_id,
        "type": "result",
        "ok": False,
        "error": error,
    }


def make_event(name: str, data: dict | None = None, request_id: str | None = None) -> dict:
    message = {"protocol": PROTOCOL_VERSION, "type": "event", "event": name, "data": data or {}}
    if request_id:
        message["id"] = request_id
    return message


class ProtocolError(ValueError):
    pass


def validate_request(message: Any) -> dict:
    """Normalise and sanity-check an incoming envelope."""
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")

    request_id = message.get("id") or new_id("req")
    if not isinstance(request_id, str):
        raise ProtocolError("'id' must be a string")

    kind = message.get("type") or ("task" if message.get("plugin") else None)
    if not isinstance(kind, str) or not kind:
        raise ProtocolError("'type' is required")

    version = message.get("protocol", PROTOCOL_VERSION)
    if str(version).split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
        raise ProtocolError(f"unsupported protocol version {version!r}")

    request = dict(message)
    request["id"] = request_id
    request["type"] = kind
    request["protocol"] = PROTOCOL_VERSION
    request.setdefault("input", {})
    request.setdefault("options", {})
    request.setdefault("context", {})

    if kind == "task":
        plugin = request.get("plugin")
        if not isinstance(plugin, str) or not plugin:
            raise ProtocolError("'plugin' is required for task messages")
        if not isinstance(request["input"], (dict, list)):
            raise ProtocolError("'input' must be an object or array")
    return request


# --------------------------------------------------------------------------
# Native messaging framing: 4-byte little-endian length + UTF-8 JSON payload
# --------------------------------------------------------------------------


def read_native_message(stream: BinaryIO) -> dict | None:
    header = stream.read(4)
    if not header or len(header) < 4:
        return None  # EOF: browser closed the port
    (length,) = struct.unpack("<I", header)
    if length > MAX_INCOMING_BYTES:
        raise ProtocolError(f"incoming message too large: {length} bytes")
    payload = stream.read(length)
    if len(payload) != length:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"invalid JSON payload: {exc}") from exc


def encode_native_message(message: dict) -> bytes:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_OUTGOING_BYTES:
        raise ProtocolError(
            f"outgoing message too large ({len(payload)} bytes > {MAX_OUTGOING_BYTES}); "
            "reduce batch size or image resolution"
        )
    return struct.pack("<I", len(payload)) + payload


def write_native_message(stream: BinaryIO, message: dict) -> None:
    stream.write(encode_native_message(message))
    stream.flush()
