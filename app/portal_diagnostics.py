from __future__ import annotations

import json
import os
import re
import sys
import threading
import traceback
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

    def __init__(self, root: Path, *, secrets=(), heartbeat_seconds: float = 15.0, **metadata):
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid4().hex[:8]
        self.directory = root.resolve() / "runs" / self.run_id
        self._secrets = sorted({str(value) for value in secrets if value}, key=len, reverse=True)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._started = self._step_started = monotonic()
        self._sequence = 0
        self._warned = False
        self._listeners = []
        self._pages = set()
        self._contexts = set()
        self.state = dict(run_id=self.run_id, pid=os.getpid(), ppid=os.getppid(),
                          status="running", store_key=None, step="startup", **metadata)
        self.emit("run.started", **metadata)
        if heartbeat_seconds > 0:
            def heartbeat():
                while not self._stop.wait(heartbeat_seconds):
                    self.emit("heartbeat", step_elapsed_ms=round((monotonic() - self._step_started) * 1000))
            self._thread = threading.Thread(target=heartbeat, name="portal-diagnostics", daemon=True)
            self._thread.start()

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
        if self._thread is not None:
            self._thread.join(timeout=1)
        for source, event, callback in self._listeners:
            try:
                source.remove_listener(event, callback)
            except Exception:
                pass
        self._listeners.clear()
        self.state.update(status=status, **fields)
        self.emit("run.finished", status=status, **fields)
