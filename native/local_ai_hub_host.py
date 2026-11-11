#!/usr/bin/env python3
"""Firefox Native Messaging host for Local AI Hub.

Firefox launches this program (through ``bin/local-ai-hub-host``) when the
extension calls ``browser.runtime.connectNative("local_ai_hub")``.

Wire format: 4-byte little-endian length prefix + UTF-8 JSON.
``stdout`` carries protocol frames ONLY.  Logs go to stderr and to
``~/Library/Application Support/LocalAIHub/logs/hub.log``.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path

# Make the bundled engine importable no matter how Firefox launches us.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
os.environ.setdefault("LOCAL_AI_HUB_HOME", str(_ROOT))

from hub import protocol  # noqa: E402
from hub.config import load_config  # noqa: E402
from hub.engine import Engine  # noqa: E402
from hub.logging_util import get_logger, setup_logging  # noqa: E402

log = get_logger("host")


class NativeHost:
    def __init__(self) -> None:
        self.config = load_config()
        self.logger = setup_logging(self.config)
        self._write_lock = threading.Lock()
        self._stdout = sys.stdout.buffer
        self._stdin = sys.stdin.buffer
        self._shutdown = threading.Event()
        self.engine = Engine(self.config, emit=self._send_async)
        self._workers: list[threading.Thread] = []
        self._max_workers = 2

    # ------------------------------------------------------------------
    def send(self, message: dict) -> None:
        try:
            frame = protocol.encode_native_message(message)
        except protocol.ProtocolError as exc:
            log.error("dropping oversized message: %s", exc)
            if message.get("id"):
                fallback = protocol.make_error(
                    message["id"], protocol.ErrorCode.MESSAGE_TOO_LARGE, str(exc)
                )
                frame = protocol.encode_native_message(fallback)
            else:
                return
        with self._write_lock:
            try:
                self._stdout.write(frame)
                self._stdout.flush()
            except (BrokenPipeError, OSError):
                self._shutdown.set()

    def _send_async(self, message: dict) -> None:
        self.send(message)

    # ------------------------------------------------------------------
    def _process(self, message: dict) -> None:
        try:
            response = self.engine.handle(message)
        except Exception as exc:  # noqa: BLE001
            log.error("unhandled error: %s\n%s", exc, traceback.format_exc())
            response = protocol.make_error(
                (message or {}).get("id", "-"), protocol.ErrorCode.INTERNAL_ERROR, str(exc)
            )
        self.send(response)
        if isinstance(message, dict) and message.get("type") == "shutdown":
            self._shutdown.set()

    def _worker(self, tasks: "queue.Queue[dict]") -> None:
        while not self._shutdown.is_set():
            try:
                message = tasks.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(message)
            finally:
                tasks.task_done()

    # ------------------------------------------------------------------
    def run(self) -> int:
        log.info("native host started (pid %d, root %s)", os.getpid(), _ROOT)
        tasks: "queue.Queue[dict]" = queue.Queue(maxsize=64)
        for index in range(self._max_workers):
            worker = threading.Thread(target=self._worker, args=(tasks,), name=f"task-{index}", daemon=True)
            worker.start()
            self._workers.append(worker)

        self.send(
            protocol.make_event(
                "host.ready",
                {"version": self.engine.handle({"type": "ping"})["output"]["version"], "pid": os.getpid()},
            )
        )

        try:
            while not self._shutdown.is_set():
                try:
                    message = self.engine_read()
                except protocol.ProtocolError as exc:
                    log.error("protocol error: %s", exc)
                    self.send(protocol.make_error("-", protocol.ErrorCode.BAD_REQUEST, str(exc)))
                    continue
                if message is None:
                    log.info("stdin closed by Firefox; shutting down")
                    break
                kind = message.get("type") if isinstance(message, dict) else None
                if kind in protocol.INLINE_CONTROL_TYPES:
                    # control messages stay responsive even during a long task
                    # (task.cancel in particular must not queue behind its own task)
                    self._process(message)
                else:
                    tasks.put(message)
        except KeyboardInterrupt:
            log.info("interrupted")
        finally:
            self._shutdown.set()
            try:
                self.engine.shutdown()
            except Exception:  # noqa: BLE001
                log.debug("shutdown cleanup failed", exc_info=True)
            log.info("native host stopped")
        return 0

    def engine_read(self) -> dict | None:
        return protocol.read_native_message(self._stdin)


def main() -> int:
    try:
        return NativeHost().run()
    except Exception as exc:  # noqa: BLE001 - last resort, stderr only
        print(f"[local-ai-hub] fatal: {exc}\n{traceback.format_exc()}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
