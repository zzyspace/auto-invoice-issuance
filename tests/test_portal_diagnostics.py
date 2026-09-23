from __future__ import annotations

import json
import signal
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models import StoreConfig
from app.portal_diagnostics import PortalDiagnostics, diagnostic_step
from app.portal_runner import TaxPortalRunner
from app.state import StateStore


def records(directory):
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


class Emitter:
    def __init__(self, **attrs):
        self.handlers = {}
        self.__dict__.update(attrs)

    def on(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    def remove_listener(self, name, handler):
        self.handlers[name].remove(handler)

    def send(self, name, *args):
        for handler in self.handlers.get(name, []):
            handler(*args)


class PortalDiagnosticsTests(unittest.TestCase):
    def make_runner(self, root):
        config = SimpleNamespace(
            portal_browser_backend="playwright", portal_user_data_dir=root / "profile",
            portal_artifacts_dir=root / "artifacts", portal_sync_from_server=False,
            portal_action_timeout_ms=1000, portal_login_timeout_minutes=1,
            portal_block_on_empty_amount=True,
            portal_home_url_for_store=lambda _: "https://etax.example/loginb/",
        )
        runner = TaxPortalRunner(config, StateStore(root / "state.db"), submit=False)
        store = StoreConfig("test", "test", "", root / "missing.xlsx", 0,
                            portal_company_verify_name="test", portal_company_switch_name="test")
        return runner, store

    def test_redacts_secrets_in_logs_tracebacks_and_url_fragments(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0, secrets=["private-password"])
            with self.assertRaises(RuntimeError):
                with diagnostics.phase("wait_login"):
                    diagnostics.emit("log", message="https://tpass.example/#/login?state=private-state&code=private-code")
                    raise RuntimeError("private-password Authorization: Bearer private-token user@example.com")
            diagnostics.finish("failed")
            content = "\n".join(path.read_text() for path in diagnostics.directory.glob("*.json*"))
            for secret in ("private-password", "private-state", "private-code", "private-token", "user@example.com"):
                self.assertNotIn(secret, content)
            self.assertIn("Traceback", content)
            self.assertIn("RuntimeError", content)

    def test_heartbeat_remains_on_current_step_while_main_thread_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0.01)
            observed = threading.Event()
            original_emit = diagnostics.emit

            def emit(event, **fields):
                original_emit(event, **fields)
                if event == "heartbeat":
                    observed.set()

            with patch.object(diagnostics, "emit", side_effect=emit):
                with diagnostics.phase("app_wait_sms"):
                    self.assertTrue(observed.wait(2))
            diagnostics.finish("success")
            heartbeat = next(row for row in records(diagnostics.directory) if row["event"] == "heartbeat")
            self.assertEqual("app_wait_sms", heartbeat["step"])
            self.assertGreaterEqual(heartbeat["step_elapsed_ms"], 0)

    def test_logging_failure_does_not_replace_original_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "not-a-directory"
            root.write_text("occupied")
            with patch("builtins.print") as output:
                diagnostics = PortalDiagnostics(root, heartbeat_seconds=0)
                with self.assertRaisesRegex(ValueError, "original"):
                    with diagnostics.phase("import_workbook"):
                        raise ValueError("original")
                diagnostics.finish("failed")
            self.assertEqual(1, output.call_count)

    def test_long_phase_without_operation_includes_business_thread_stack_in_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0.01)
            observed = threading.Event()
            original_emit = diagnostics.emit

            def emit(event, **fields):
                original_emit(event, **fields)
                if event == "heartbeat":
                    observed.set()

            with patch.object(diagnostics, "emit", side_effect=emit):
                with diagnostics.phase("wait_external_login"):
                    with diagnostics._lock:
                        diagnostics._step_started -= 31.0
                    self.assertTrue(observed.wait(2))
            diagnostics.finish("success")
            events = records(diagnostics.directory)
            heartbeat = next(row for row in events if row["event"] == "heartbeat" and "step_stack" in row)
            self.assertEqual("wait_external_login", heartbeat["step"])
            self.assertEqual(threading.get_ident(), heartbeat["business_thread_id"])
            self.assertGreaterEqual(heartbeat["step_elapsed_ms"], 31000)
            self.assertIsNone(heartbeat["current_operation"])
            self.assertTrue(any(frame["function"] ==
                                "test_long_phase_without_operation_includes_business_thread_stack_in_heartbeat"
                                for frame in heartbeat["step_stack"]))
            self.assertTrue(all(set(frame) == {"file", "function", "line"} for frame in heartbeat["step_stack"]))
            self.assertFalse(any(row["event"].endswith(".failed") for row in events))
            self.assertFalse(diagnostics._thread.is_alive())

    def test_watchdog_records_real_blocked_thread_and_target_without_values(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0.01, watchdog_seconds=0.04)
            blocked = threading.Event()
            release = threading.Event()
            entered = threading.Event()
            original_emit = diagnostics.emit

            def emit(event, **fields):
                original_emit(event, **fields)
                if event == "operation.blocked":
                    blocked.set()

            def blocked_native_read():
                private_local = "private-value-only-in-local-variable"
                with diagnostics.phase("app_login"):
                    with diagnostics.operation("ax.read", pid=123, element_id="0xabc", attribute="AXValue",
                                               node_count=7, phase="scan", scan_id="scan-1", depth=2,
                                               path="root/0/3", role="AXTextField", return_code=-25204,
                                               value=private_local, password="never-log-this"):
                        entered.set()
                        release.wait(2)

            with patch.object(diagnostics, "emit", side_effect=emit):
                worker = threading.Thread(target=blocked_native_read)
                worker.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(blocked.wait(2))
                    events = records(diagnostics.directory)
                    stalled = next(row for row in events if row["event"] == "operation.blocked")
                    operation = stalled["operation"]
                    self.assertEqual(worker.ident, operation["thread_id"])
                    self.assertEqual("AXValue", operation["metadata"]["attribute"])
                    self.assertEqual("root/0/3", operation["metadata"]["path"])
                    self.assertEqual("AXTextField", operation["metadata"]["role"])
                    self.assertEqual(-25204, operation["metadata"]["return_code"])
                    self.assertEqual("app_login", operation["step"])
                    self.assertGreaterEqual(operation["duration_ms"], 40)
                    self.assertTrue(any(frame["function"] == "blocked_native_read" for frame in stalled["stack"]))
                    self.assertTrue(any(frame["function"] == "wait" for frame in stalled["stack"]))
                    self.assertTrue(all(set(frame) == {"file", "function", "line"} for frame in stalled["stack"]))
                    heartbeat = next(row for row in events if row["event"] == "heartbeat" and row["current_operation"])
                    self.assertEqual("AXValue", heartbeat["current_operation"]["metadata"]["attribute"])
                    status = json.loads((diagnostics.directory / "status.json").read_text())
                    self.assertEqual("ax.read", status["current_operation"]["name"])
                    # The same call is reported once, although many monitor ticks run.
                    self.assertFalse(release.wait(0.08))
                    self.assertEqual(1, sum(row["event"] == "operation.blocked" for row in records(diagnostics.directory)))
                finally:
                    release.set()
                    worker.join(2)
                    diagnostics.finish("success")
            self.assertFalse(worker.is_alive())
            self.assertFalse(diagnostics._thread.is_alive())
            status = json.loads((diagnostics.directory / "status.json").read_text())
            self.assertIsNone(status["current_operation"])
            self.assertEqual("finished", status["last_progress"]["status"])
            self.assertEqual({}, diagnostics._operations)
            content = "\n".join(path.read_text() for path in diagnostics.directory.glob("*.json*"))
            self.assertNotIn("private-value-only-in-local-variable", content)
            self.assertNotIn("never-log-this", content)
            self.assertNotIn("watchdog_seconds", content)

    def test_fast_operations_remain_in_bounded_memory_until_heartbeat(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0)
            initial_events = records(diagnostics.directory)
            for index in range(30):
                with diagnostics.operation("ax.read", node_count=index, attribute="AXChildren"):
                    pass
            self.assertEqual(initial_events, records(diagnostics.directory))
            self.assertEqual(12, len(diagnostics._recent_operations))
            diagnostics.emit("heartbeat")
            heartbeat = records(diagnostics.directory)[-1]
            self.assertIsNone(heartbeat["current_operation"])
            self.assertEqual(list(range(18, 30)), [row["metadata"]["node_count"] for row in heartbeat["recent_operations"]])
            self.assertEqual(29, heartbeat["last_progress"]["metadata"]["node_count"])
            self.assertIsNone(diagnostics._thread)
            diagnostics.finish("success")

    def test_nested_operation_restores_outer_call_and_failure_omits_exception_text(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0)
            original = RuntimeError("sensitive-value-that-is-not-a-configured-secret")
            with diagnostics.operation("ax.scan", phase="scan"):
                try:
                    with diagnostics.operation("ax.read", attribute="AXValue", value="unknown-private-value",
                                               role="private-role-text", path="password/private"):
                        raise original
                except RuntimeError as observed:
                    self.assertIs(original, observed)
                diagnostics.emit("heartbeat")
                self.assertEqual("ax.scan", records(diagnostics.directory)[-1]["current_operation"]["name"])
            diagnostics.finish("failed")
            events = records(diagnostics.directory)
            failure = next(row for row in events if row["event"] == "operation.failed")
            self.assertEqual("RuntimeError", failure["operation"]["error_type"])
            self.assertEqual({"attribute": "AXValue"}, failure["operation"]["metadata"])
            content = "\n".join(path.read_text() for path in diagnostics.directory.glob("*.json*"))
            for secret in (str(original), "unknown-private-value", "private-role-text", "password/private"):
                self.assertNotIn(secret, content)

    def test_operation_write_failure_preserves_business_exception_and_clears_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "not-a-directory"
            root.write_text("occupied")
            original = ValueError("original-business-error")
            with patch("builtins.print") as output:
                diagnostics = PortalDiagnostics(root, heartbeat_seconds=0)
                with self.assertRaises(ValueError) as captured:
                    with diagnostics.operation("ax.read", attribute="AXValue"):
                        raise original
                self.assertIs(original, captured.exception)
                self.assertEqual({}, diagnostics._operations)
                self.assertIsNone(diagnostics.state["current_operation"])
                diagnostics.finish("failed")
            self.assertEqual(1, output.call_count)

    def test_finish_clears_inflight_call_and_late_completion_does_not_reopen_run(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0.01, watchdog_seconds=0.03)
            entered = threading.Event()
            release = threading.Event()

            def blocked_native_read():
                with diagnostics.operation("ax.read", attribute="AXChildren"):
                    entered.set()
                    release.wait(2)

            worker = threading.Thread(target=blocked_native_read)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                diagnostics.finish("interrupted")
                finished_events = records(diagnostics.directory)
            finally:
                release.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(diagnostics._thread.is_alive())
            self.assertEqual(finished_events, records(diagnostics.directory))
            status = json.loads((diagnostics.directory / "status.json").read_text())
            self.assertEqual("interrupted", status["status"])
            self.assertIsNone(status["current_operation"])
            self.assertEqual({}, diagnostics._operations)

    def test_new_pages_redirects_and_network_errors_are_recorded_once_and_detached(self):
        with tempfile.TemporaryDirectory() as temp:
            diagnostics = PortalDiagnostics(Path(temp), heartbeat_seconds=0)
            context = Emitter(pages=[])
            diagnostics.observe_context(context)
            diagnostics.observe_context(context)
            page = Emitter(url="https://example.test/login", main_frame=SimpleNamespace(url="https://example.test/login"))
            context.send("page", page)
            diagnostics.observe_page(page)
            request = SimpleNamespace(resource_type="document", url=page.url, method="GET",
                                      redirected_from=SimpleNamespace(url="https://example.test/home"), failure="net::ERR_ABORTED")
            page.send("request", request)
            page.send("response", SimpleNamespace(request=request, url=page.url, status=412))
            page.send("requestfailed", request)
            page.send("framenavigated", page.main_frame)
            diagnostics.finish("failed")
            events = records(diagnostics.directory)
            self.assertEqual(1, sum(row["event"] == "page.observed" for row in events))
            self.assertTrue(any(row.get("status") == 412 for row in events))
            self.assertTrue(any(row.get("error") == "net::ERR_ABORTED" for row in events))
            self.assertTrue(any(row.get("redirected_from") == "https://example.test/home" for row in events))
            self.assertTrue(all(not handlers for handlers in page.handlers.values()))
            self.assertEqual([], context.handlers["page"])

    def test_workbook_failure_before_result_creation_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner, store = self.make_runner(root)
            context = Emitter(pages=[])
            with patch.object(runner, "_run_browser", side_effect=lambda stores: [runner._run_store(context, object(), stores[0])]):
                with self.assertRaises(FileNotFoundError):
                    runner.run([store])
            directory = next((root / "artifacts/runs").iterdir())
            self.assertEqual("failed", json.loads((directory / "status.json").read_text())["status"])
            with sqlite3.connect(root / "state.db") as db:
                status, step = db.execute("SELECT last_status, current_step FROM portal_issue_state").fetchone()
            self.assertEqual(("failed", "prepare_workbook"), (status, step))
            self.assertTrue(any(row["event"] == "step.failed" and "FileNotFoundError" in row["traceback"]
                                for row in records(directory)))

    def test_interruption_records_precise_step_and_history_and_restores_handlers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner, store = self.make_runner(root)
            page = SimpleNamespace(url="https://example.test/login")
            context = Emitter(pages=[])
            previous = signal.getsignal(signal.SIGTERM)

            @diagnostic_step("app_wait_sms")
            def interrupted(owner, *_args):
                signal.raise_signal(signal.SIGTERM)

            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("1"))
            with patch.object(runner, "_prepare_workbook", return_value=([object()], summary, "abc")), \
                 patch.object(runner, "_goto"), \
                 patch.object(runner, "_ensure_logged_in", side_effect=lambda *args: interrupted(runner, *args)), \
                 patch.object(runner, "_run_browser", side_effect=lambda stores: [runner._run_store(context, page, stores[0])]):
                with self.assertRaisesRegex(KeyboardInterrupt, "SIGTERM"):
                    runner.run([store])
            self.assertIs(previous, signal.getsignal(signal.SIGTERM))
            directory = next((root / "artifacts/runs").iterdir())
            self.assertEqual("interrupted", json.loads((directory / "status.json").read_text())["status"])
            with sqlite3.connect(root / "state.db") as db:
                row = db.execute("SELECT status, step, submitted_count FROM portal_issue_history").fetchone()
            self.assertEqual(("failed", "app_wait_sms", 0), row)

    def test_failure_snapshot_precedes_page_cleanup_and_retains_innermost_step(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner, store = self.make_runner(root)
            order = []
            page = Emitter(url="https://example.test/batch")
            page.evaluate = lambda _: order.append("snapshot") or {"readyState": "complete"}
            page.screenshot = lambda **_: order.append("screenshot")
            page.locator = lambda _: object()
            page.close = lambda: order.append("close")
            summary = SimpleNamespace(row_count=1, total_amount_including_tax=Decimal("1"))

            @diagnostic_step("import_workbook")
            def import_failure(owner, *args):
                raise RuntimeError("upload rejected")

            with patch.object(runner, "_prepare_workbook", return_value=([object()], summary, "abc")), \
                 patch.object(runner, "_goto"), \
                 patch.object(runner, "_ensure_logged_in", return_value=page), \
                 patch.object(runner, "_ensure_authenticated_home_page", return_value=page), \
                 patch.object(runner, "_ensure_company", return_value=page), \
                 patch.object(runner, "_wait_for_home_page_ready"), \
                 patch.object(runner, "_wait_before_open_batch_page"), \
                 patch.object(runner, "_wait_for_batch_page", return_value=page), \
                 patch.object(runner, "_ensure_batch_page_clean"), \
                 patch.object(runner, "_import_workbook", side_effect=lambda *args: import_failure(runner, *args)), \
                 patch.object(runner, "_capture_artifact"), \
                 patch.object(runner, "_run_browser", side_effect=lambda stores: [runner._run_store(Emitter(pages=[]), page, stores[0])]):
                result = runner.run([store])[0]
            self.assertEqual("import_workbook", result.step)
            self.assertEqual("failed", result.status)
            self.assertLess(order.index("screenshot"), order.index("close"))
            self.assertEqual(1, order.count("snapshot"))


if __name__ == "__main__":
    unittest.main()
