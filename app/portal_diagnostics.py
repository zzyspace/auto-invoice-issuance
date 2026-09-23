from __future__ import annotations

import json
import os
import re
import sys
import threading
import traceback
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from time import monotonic
from uuid import uuid4


# Deliberately omit input values, cookies, storage, request/response bodies and QR data.
PAGE_SNAPSHOT_JS = """() => {
  const nav = performance.getEntriesByType('navigation')[0];
  const text = document.body?.innerText || '';
  const markers = ['打开电子税务局APP扫一扫', '二维码已失效', '二维码已过期',
    '登录确认', '登录成功', '身份切换', '发票业务', '蓝字发票开具', '批量开票',
    '导入完成', '批量开具结果', '开具成功', '开具失败'];
  return {
    url: location.href, title: document.title, readyState: document.readyState,
    navigationStatus: nav?.responseStatus || 0, bodyLength: text.length,
    markers: markers.filter(value => text.includes(value)),
    resources: performance.getEntriesByType('resource').filter(item =>
      item.responseStatus >= 400 || ['fetch', 'xmlhttprequest'].includes(item.initiatorType)
    ).slice(-20).map(item => ({url: item.name, status: item.responseStatus || 0,
      duration_ms: Math.round(item.duration), type: item.initiatorType}))
  };
}"""


def diagnostic_step(name: str):
    """Record a phase without logging arguments (which can contain credentials)."""
    def decorate(method):
        @wraps(method)
        def wrapped(self, *args, **kwargs):
            diagnostics = getattr(self, "_diagnostics", None)
            if diagnostics is None:
                return method(self, *args, **kwargs)
            changed = getattr(self, "_diagnostic_step_changed", None)
            with diagnostics.phase(name, changed):
                try:
                    return method(self, *args, **kwargs)
                except BaseException as exc:
                    # Preserve the innermost failing phase through cleanup and rethrows.
                    if not getattr(exc, "portal_diagnostic_step", None):
                        exc.portal_diagnostic_step = name
                        capture = getattr(self, "_diagnostic_failure", None)
                        if capture is not None:
                            try:
                                capture(exc, args)
                            except Exception as capture_error:
                                diagnostics.emit("capture.failed", error=str(capture_error))
                    raise
        return wrapped
    return decorate


class PortalDiagnostics:
    """Best-effort, local, flushed diagnostics independent of terminal scrollback."""

    def __init__(self, root: Path, *, secrets=(), heartbeat_seconds: float = 15.0,
                 watchdog_seconds: float = 2.0, **metadata):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid4().hex[:8]
        self.directory = root.resolve() / "runs" / self.run_id
        self._secrets = sorted({str(value) for value in secrets if value}, key=len, reverse=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._business_thread_id = threading.get_ident()
        self._started = self._step_started = monotonic()
        self._sequence = 0
        self._operation_sequence = 0
        self._operations = {}
        self._recent_operations = deque(maxlen=12)
        self._last_progress = None
        self._watchdog_seconds = watchdog_seconds
        self._finished = False
        self._warned = False
        self._listeners = []
        self._pages = set()
        self._contexts = set()
        self.state = dict(run_id=self.run_id, pid=os.getpid(), ppid=os.getppid(),
                          status="running", store_key=None, step="startup", **metadata)
        self.emit("run.started", **metadata)
        if heartbeat_seconds > 0:
            def heartbeat():
                next_heartbeat = monotonic() + heartbeat_seconds
                interval = min(heartbeat_seconds, 0.5)
                if watchdog_seconds > 0:
                    interval = min(interval, max(0.01, watchdog_seconds / 2))
                while not self._stop.wait(interval):
                    try:
                        now = monotonic()
                        self._check_blocked_operations(now)
                        if now >= next_heartbeat:
                            self.emit("heartbeat", step_elapsed_ms=round((now - self._step_started) * 1000))
                            next_heartbeat = now + heartbeat_seconds
                    except Exception:
                        # Diagnostics must not interrupt the business thread or its monitor.
                        pass
            self._thread = threading.Thread(target=heartbeat, name="portal-diagnostics", daemon=True)
            self._thread.start()

    @staticmethod
    def _safe_operation_metadata(metadata):
        """Accept identifiers only, never UI values or object representations."""
        safe = {}
        for key in ("pid", "element_id", "attribute", "action", "node_count", "phase",
                    "scan_id", "depth", "path", "role", "return_code"):
            value = metadata.get(key)
            if key in {"pid", "node_count", "depth"}:
                if type(value) is int and value >= 0:
                    safe[key] = value
            elif key == "return_code" and type(value) is int:
                safe[key] = value
            elif key == "element_id" and type(value) is int:
                safe[key] = value
            elif key == "path":
                if isinstance(value, str) and re.fullmatch(r"(?:root)?[0-9./:-]{0,256}", value):
                    safe[key] = value
            elif key == "role":
                if isinstance(value, str) and re.fullmatch(r"AX[A-Za-z0-9_]{1,64}", value):
                    safe[key] = value
            elif isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                safe[key] = value
        return safe

    @staticmethod
    def _operation_snapshot(operation, now):
        return {key: value for key, value in operation.items() if not key.startswith("_")} | {
            "duration_ms": round((now - operation["_started"]) * 1000),
        }

    def _operation_progress(self, now):
        active = [stack[-1] for stack in self._operations.values() if stack]
        current = max(active, key=lambda operation: operation["operation_id"], default=None)
        return {
            "current_operation": self._operation_snapshot(current, now) if current else None,
            "recent_operations": list(self._recent_operations),
            "last_progress": self._last_progress,
        }

    @staticmethod
    def _thread_stack(thread_id):
        """Capture locations only; formatted tracebacks can expose source/values."""
        frames = []
        frame = sys._current_frames().get(thread_id)
        try:
            while frame is not None and len(frames) < 64:
                frames.append({"file": frame.f_code.co_filename,
                               "function": frame.f_code.co_name, "line": frame.f_lineno})
                frame = frame.f_back
        finally:
            del frame
        return list(reversed(frames))

    def _check_blocked_operations(self, now):
        if self._watchdog_seconds <= 0:
            return
        with self._lock:
            if self._finished:
                return
            for operations in self._operations.values():
                if not operations:
                    continue
                operation = operations[-1]
                if now - operation["_started"] < self._watchdog_seconds:
                    continue
                last_reported = operation.get("_last_reported")
                if last_reported is not None and now - last_reported < max(5.0, self._watchdog_seconds * 5):
                    continue
                operation["_last_reported"] = now
                self.emit("operation.blocked", operation=self._operation_snapshot(operation, now),
                          stack=self._thread_stack(operation["thread_id"]))

    @contextmanager
    def operation(self, name: str, **metadata):
        """Track a native call in memory; persist slow calls, failures and heartbeats.

        Names and metadata must be static labels/identifiers, never field values.
        This context does not cancel calls, read UI state or capture screenshots.
        """
        operation = None
        try:
            with self._lock:
                if not self._finished:
                    self._operation_sequence += 1
                    operation = {
                        "operation_id": self._operation_sequence,
                        "name": name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name)
                        else "invalid-operation-name",
                        "thread_id": threading.get_ident(), "step": self.state["step"],
                        "started_at": datetime.now(timezone.utc).isoformat(),
                        "metadata": self._safe_operation_metadata(metadata), "_started": monotonic(),
                    }
                    self._operations.setdefault(operation["thread_id"], []).append(operation)
        except Exception:
            operation = None
        try:
            yield
        except BaseException as exc:
            self._complete_operation_safely(operation, error_type=type(exc).__name__)
            raise
        else:
            self._complete_operation_safely(operation)

    def _complete_operation_safely(self, operation, *, error_type=None):
        if operation is None:
            return
        try:
            with self._lock:
                if self._finished:
                    return
                stack = self._operations.get(operation["thread_id"], [])
                if operation in stack:
                    stack.remove(operation)
                if not stack:
                    self._operations.pop(operation["thread_id"], None)
                now = monotonic()
                completed = self._operation_snapshot(operation, now)
                completed.update(status="failed" if error_type else "finished",
                                 finished_at=datetime.now(timezone.utc).isoformat())
                if error_type:
                    completed["error_type"] = error_type
                self._recent_operations.append(completed)
                self._last_progress = completed
                self.state.update(self._operation_progress(now))
                slow = self._watchdog_seconds > 0 and now - operation["_started"] >= self._watchdog_seconds
                if error_type or slow or operation.get("_last_reported") is not None:
                    self.emit("operation.failed" if error_type else "operation.finished", operation=completed)
        except Exception:
            pass

    def redact(self, value):
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        if not isinstance(value, str):
            return value
        for secret in self._secrets:
            value = value.replace(secret, "<redacted>")
        # Redact all URL query values, including queries inside SPA fragments.
        value = re.sub(r"([?&][\w.%+-]+=)[^&#\s\"'<>\\]*", r"\1<redacted>", value)
        value = re.sub(r"(?i)(bearer\s+)[\w.\-]+", r"\1<redacted>", value)
        value = re.sub(
            r"(?i)((?:password|passwd|token|authorization|cookie|secret|验证码)\s*[\"']?\s*[:=：]\s*[\"']?)[^\s,;\"'<>]+",
            r"\1<redacted>", value,
        )
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "<email>", value)
        return value[:24000]

    def emit(self, event: str, **fields):
        with self._lock:
            try:
                now = monotonic()
                progress = self._operation_progress(now)
                self.state.update(progress)
                if event in {"heartbeat", "operation.blocked", "operation.failed", "operation.finished", "run.finished"}:
                    fields = {**progress, **fields}
                if event == "heartbeat":
                    fields["step_elapsed_ms"] = round((now - self._step_started) * 1000)
                    if now - self._step_started >= 30.0:
                        # Include non-AX waits too; observing a long step is not a failure.
                        fields["business_thread_id"] = self._business_thread_id
                        fields["step_stack"] = self._thread_stack(self._business_thread_id)
                self._sequence += 1
                record = self.redact({
                    "timestamp": datetime.now(timezone.utc).isoformat(), "seq": self._sequence,
                    "elapsed_ms": round((monotonic() - self._started) * 1000),
                    "run_id": self.run_id, "store_key": self.state["store_key"], "step": self.state["step"],
                    "event": event, **fields,
                })
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = self.directory / "events.jsonl"
                # Bound disk usage while retaining the most recent events.
                if path.exists() and path.stat().st_size > 8 * 1024 * 1024:
                    path.replace(self.directory / "events.previous.jsonl")
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.state.update(updated_at=record["timestamp"], last_event=event)
                if event == "log":
                    self.state["last_message"] = record.get("message")
                if event in {"operation.failed", "step.failed", "store.failed", "run.failed"}:
                    self.state["last_error"] = record
                temporary = self.directory / "status.tmp"
                temporary.write_text(json.dumps(self.redact(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(self.directory / "status.json")
            except Exception as exc:
                if not self._warned:
                    self._warned = True
                    print(f"[tax-portal][runner] diagnostics write failed: {type(exc).__name__}", file=sys.stderr, flush=True)

    def set_step(self, step: str, changed=None):
        with self._lock:
            self.state["step"] = step
            self._step_started = monotonic()
        if changed is not None:
            try:
                changed(step)
            except Exception as exc:
                self.emit("state_update.failed", error=str(exc))

    @contextmanager
    def phase(self, step: str, changed=None):
        previous = self.state["step"]
        started = monotonic()
        self.set_step(step, changed)
        self.emit("step.started")
        try:
            yield
        except BaseException as exc:
            self.emit("step.failed", error_type=type(exc).__name__, error=str(exc),
                      traceback=traceback.format_exc(), duration_ms=round((monotonic() - started) * 1000))
            raise
        else:
            self.emit("step.finished", duration_ms=round((monotonic() - started) * 1000))
        finally:
            self.set_step(previous, changed)

    def observe_context(self, context):
        if context is None or id(context) in self._contexts:
            return
        self._contexts.add(id(context))
        self._listen(context, "page", self.observe_page)
        try:
            for page in context.pages:
                self.observe_page(page)
        except Exception as exc:
            self.emit("observe.failed", error=str(exc))

    def _listen(self, source, event, callback):
        def safe_callback(*args):
            try:
                callback(*args)
            except Exception as exc:
                self.emit("observer.failed", observer=event, error=str(exc))
        if callable(getattr(source, "on", None)):
            try:
                source.on(event, safe_callback)
                self._listeners.append((source, event, safe_callback))
            except Exception as exc:
                self.emit("observer.failed", observer=event, error=str(exc))

    def observe_page(self, page):
        if id(page) in self._pages:
            return
        self._pages.add(id(page))
        label = f"page-{len(self._pages)}"
        self.emit("page.observed", page=label, url=str(getattr(page, "url", "")))
        pending = {}

        def request_started(request):
            # Avoid storing payloads and avoid unbounded tracking of unfinished requests.
            if len(pending) >= 2000:
                pending.pop(next(iter(pending)))
            pending[id(request)] = monotonic()
            if request.resource_type == "document":
                previous = request.redirected_from
                self.emit("navigation.request", page=label, method=request.method, url=request.url,
                          redirected_from=previous.url if previous else None)

        def response_received(response):
            request = response.request
            duration = round((monotonic() - pending.get(id(request), monotonic())) * 1000)
            if response.status >= 400 or request.resource_type == "document" or duration >= 800:
                self.emit("network.response", page=label, url=response.url, status=response.status,
                          method=request.method, resource_type=request.resource_type, duration_ms=duration)

        def request_failed(request):
            pending.pop(id(request), None)
            self.emit("network.failed", page=label, url=request.url, method=request.method,
                      resource_type=request.resource_type, error=request.failure)

        def frame_navigated(frame):
            if frame == page.main_frame:
                self.emit("navigation.committed", page=label, url=frame.url)

        self._listen(page, "request", request_started)
        self._listen(page, "response", response_received)
        self._listen(page, "requestfinished", lambda request: pending.pop(id(request), None))
        self._listen(page, "requestfailed", request_failed)
        self._listen(page, "framenavigated", frame_navigated)
        self._listen(page, "pageerror", lambda error: self.emit("page.failed", page=label, error=str(error)))
        self._listen(page, "console", lambda message: self.emit("console.error", page=label, text=message.text)
                     if message.type == "error" else None)
        self._listen(page, "crash", lambda *_: self.emit("page.crashed", page=label))
        self._listen(page, "close", lambda *_: self.emit("page.closed", page=label))

    def snapshot(self, page, *, reason: str, screenshot: bool = False):
        try:
            self.emit("page.snapshot", reason=reason, snapshot=page.evaluate(PAGE_SNAPSHOT_JS))
        except Exception as exc:
            self.emit("snapshot.failed", reason=reason, url=str(getattr(page, "url", "")), error=str(exc))
        if screenshot:
            path = self.directory / f"failure-{self._sequence}.png"
            try:
                # Mask input fields and QR challenges. Only capture the failed page.
                page.screenshot(path=str(path), timeout=3000, full_page=False,
                                mask=[page.locator("input, textarea, canvas, img, [class*='qr']")])
                self.emit("screenshot.saved", path=str(path))
            except Exception as exc:
                self.emit("screenshot.failed", error=str(exc))

    def finish(self, status: str, **fields):
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        for source, event, callback in self._listeners:
            try:
                source.remove_listener(event, callback)
            except Exception:
                pass
        self._listeners.clear()
        with self._lock:
            self._finished = True
            self._operations.clear()
            self.state.update(status=status, **fields)
            self.emit("run.finished", status=status, **fields)
