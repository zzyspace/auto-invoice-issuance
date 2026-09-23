from __future__ import annotations

import ctypes
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from app.portal_diagnostics import PortalDiagnostics
from app.portal_local_login import AXNode, MacAccessibilityClient, PortalMacLoginAutomator


def make_client(diagnostics=None):
    # No framework loading, subprocesses, application activation or native UI calls.
    client = object.__new__(MacAccessibilityClient)
    client.app = Mock()
    client.core = Mock()
    client._cf_string = Mock(return_value=101)
    client._diagnostics = diagnostics
    client._diagnostic_scan = None
    client._diagnostic_scan_sequence = 0
    client._last_scan = {}
    return client


def make_diagnostics(failure=None):
    context = MagicMock()
    context.__exit__.return_value = False
    diagnostics = Mock()
    diagnostics.operation.return_value = context
    error = RuntimeError("diagnostic observer unavailable")
    if failure == "operation":
        diagnostics.operation.side_effect = error
    elif failure == "enter":
        context.__enter__.side_effect = error
    elif failure == "exit":
        context.__exit__.side_effect = error
    elif failure == "emit":
        diagnostics.emit.side_effect = error
    return diagnostics, context


def make_node():
    return AXNode(23, "AXButton", "", ("登录",), (10, 20), (30, 40))


class PortalAXDiagnosticsTests(unittest.TestCase):
    def test_nonzero_native_read_still_returns_none(self):
        for code in (1, -25204, ctypes.c_uint32(-25204).value, -25205, -25212):
            with self.subTest(code=code):
                diagnostics, _ = make_diagnostics()
                client = make_client(diagnostics)
                client.app.AXUIElementCopyAttributeValue.return_value = code
                self.assertIsNone(client._attribute_value(23, "AXWindows"))
                client.app.AXUIElementCopyAttributeValue.assert_called_once()
                self.assertEqual(23, client.app.AXUIElementCopyAttributeValue.call_args.args[0])

    def test_successful_native_read_returns_original_pointer(self):
        client = make_client(make_diagnostics()[0])

        def native(_element, _attribute, output):
            ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 12345
            return 0

        client.app.AXUIElementCopyAttributeValue.side_effect = native
        self.assertEqual(12345, client._attribute_value(23, "AXWindows"))
        client.app.AXUIElementCopyAttributeValue.assert_called_once()

    def test_nonzero_native_write_still_returns_false(self):
        for code in (1, ctypes.c_uint32(-25204).value, -25205):
            with self.subTest(code=code):
                client = make_client(make_diagnostics()[0])
                client.app.AXUIElementSetAttributeValue.return_value = code
                self.assertIs(False, client.set_text_value(make_node(), "test input"))
                client.app.AXUIElementSetAttributeValue.assert_called_once()

    def test_all_nonzero_axpress_results_keep_original_coordinate_fallback(self):
        for code in (1, ctypes.c_uint32(-25204).value, -25206, -25208):
            with self.subTest(code=code):
                client = make_client(make_diagnostics()[0])
                client.app.AXUIElementPerformAction.return_value = code
                with patch.object(client, "click_at") as click:
                    self.assertTrue(client.click_node(make_node()))
                client.app.AXUIElementPerformAction.assert_called_once()
                click.assert_called_once_with(25.0, 40.0)

    def test_successful_axpress_does_not_add_coordinate_click(self):
        client = make_client(make_diagnostics()[0])
        client.app.AXUIElementPerformAction.return_value = 0
        with patch.object(client, "click_at") as click:
            self.assertTrue(client.click_node(make_node()))
        click.assert_not_called()
        client.app.AXUIElementPerformAction.assert_called_once()

    def test_observation_preserves_raw_unsigned_return_and_logs_signed_code_only(self):
        diagnostics, _ = make_diagnostics()
        client = make_client(diagnostics)
        unsigned = ctypes.c_uint32(-25204).value
        native = Mock(return_value=unsigned)
        returned = client._observed_ax_call("ax.read", 23, "AXWindows", native, 101, 102)
        self.assertIs(unsigned, returned)
        native.assert_called_once_with(23, 101, 102)
        self.assertEqual(-25204, diagnostics.emit.call_args.kwargs["return_code"])
        self.assertEqual("ax.call.returned_error", diagnostics.emit.call_args.args[0])
        client.app.AXUIElementPerformAction.return_value = unsigned
        self.assertEqual(unsigned, client._perform_action(23, "AXPress"))

    def test_native_exception_object_is_preserved_even_if_observer_suppresses_it(self):
        for native_error in (RuntimeError("original native failure"), KeyboardInterrupt()):
            with self.subTest(error_type=type(native_error).__name__):
                diagnostics, context = make_diagnostics()
                context.__exit__.return_value = True
                client = make_client(diagnostics)
                native = Mock(side_effect=native_error)
                with self.assertRaises(type(native_error)) as raised:
                    client._observed_ax_call("ax.read", 23, "AXWindows", native)
                self.assertIs(native_error, raised.exception)
                native.assert_called_once_with(23)
                self.assertIs(native_error, context.__exit__.call_args.args[1])

    def test_observer_failures_never_change_native_result_or_call_count(self):
        for failure in ("operation", "enter", "exit", "emit"):
            for result in (0, ctypes.c_uint32(-25204).value):
                with self.subTest(failure=failure, result=result):
                    diagnostics, _ = make_diagnostics(failure)
                    client = make_client(diagnostics)
                    native = Mock(return_value=result)
                    self.assertIs(result, client._observed_ax_call("ax.read", 23, "AXWindows", native, 101))
                    native.assert_called_once_with(23, 101)

    def test_observer_enter_and_exit_failure_cannot_replace_native_exception(self):
        for failure in ("operation", "enter", "exit", "emit"):
            with self.subTest(failure=failure):
                diagnostics, _ = make_diagnostics(failure)
                client = make_client(diagnostics)
                error = ValueError("original error")
                native = Mock(side_effect=error)
                with self.assertRaises(ValueError) as raised:
                    client._observed_ax_call("ax.read", 23, "AXWindows", native)
                self.assertIs(error, raised.exception)
                native.assert_called_once_with(23)

    def test_find_nodes_observation_preserves_depth_first_order_and_pointer_dedup(self):
        expected = [1, 2, 4, 3, 5]
        for observer_mode in ("disabled", "enabled", "emit_failure"):
            with self.subTest(observer_mode=observer_mode):
                diagnostics = None if observer_mode == "disabled" else make_diagnostics(
                    "emit" if observer_mode == "emit_failure" else None
                )[0]
                client = make_client(diagnostics)
                client.app_element = Mock(return_value=100)
                graph = {100: [1, 5], 1: [2, 3], 2: [4], 3: [2], 4: [1], 5: []}
                client._children_from_attribute = Mock(side_effect=lambda element, _attr: graph[element])
                visited = []

                def attribute(element, name):
                    if name == "AXRole":
                        visited.append(element)
                    return "AXWindow" if element in (1, 5) else "AXButton"

                client._attribute_text = Mock(side_effect=attribute)
                client._texts_for_element = Mock(return_value=())
                client._point_attribute = Mock(return_value=None)
                client._size_attribute = Mock(return_value=None)
                nodes = client.find_nodes(42)
                self.assertEqual(expected, [node.element for node in nodes])
                self.assertEqual(expected, visited)
                self.assertEqual([100] + expected, [call.args[0] for call in client._children_from_attribute.call_args_list])
                self.assertIsNone(client._diagnostic_scan)
                if diagnostics is not None:
                    self.assertEqual(5, client.diagnostic_state()["node_count"])
                    self.assertEqual(42, client.diagnostic_state()["pid"])
                    self.assertEqual(["ax.scan.started", "ax.scan.finished"],
                                     [call.args[0] for call in diagnostics.emit.call_args_list])

    def test_long_native_read_is_observed_by_watchdog_without_extra_ui_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = PortalDiagnostics(Path(directory), heartbeat_seconds=0.01, watchdog_seconds=0.03)
            client = make_client(diagnostics)
            client._diagnostic_scan = dict(pid=42, scan_id="scan-7", node_count=9)
            entered = threading.Event()
            release = threading.Event()
            blocked = threading.Event()
            failures = []
            results = []
            emit = diagnostics.emit

            def record(event, **fields):
                emit(event, **fields)
                if event == "operation.blocked":
                    blocked.set()

            def blocked_native_read(_element, _attribute, _output):
                entered.set()
                release.wait(2)
                return 0

            def worker():
                try:
                    results.append(client._attribute_value(23, "AXWindows"))
                except BaseException as error:
                    failures.append(error)

            client.app.AXUIElementCopyAttributeValue.side_effect = blocked_native_read
            with patch.object(diagnostics, "emit", side_effect=record):
                thread = threading.Thread(target=worker)
                thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(blocked.wait(2))
                finally:
                    release.set()
                    thread.join(2)
                    diagnostics.finish("success")
            self.assertFalse(thread.is_alive())
            self.assertEqual([], failures)
            self.assertEqual([None], results)
            client.app.AXUIElementCopyAttributeValue.assert_called_once()
            events = [json.loads(line) for line in (diagnostics.directory / "events.jsonl").read_text().splitlines()]
            stalled = next(event for event in events if event["event"] == "operation.blocked")
            operation = stalled["operation"]
            self.assertEqual("ax.read", operation["name"])
            metadata = operation["metadata"]
            self.assertEqual(42, metadata["pid"])
            self.assertEqual("0x17", metadata["element_id"])
            self.assertEqual("AXWindows", metadata["attribute"])
            self.assertEqual("scan-7", metadata["scan_id"])
            self.assertEqual(9, metadata["node_count"])
            self.assertTrue(any(frame["function"] == "blocked_native_read" for frame in stalled["stack"]))
            self.assertTrue(all(set(frame) == {"file", "function", "line"} for frame in stalled["stack"]))

    def test_failure_snapshot_only_reads_cache_without_ax_or_process_probe(self):
        client = make_client()
        cached = dict(pid=42, scan_id="7", node_count=9, attribute="AXWindows")
        client._last_scan = cached.copy()
        client.find_nodes = Mock(side_effect=AssertionError("new UI scan"))
        client.app.AXUIElementCopyAttributeValue.side_effect = AssertionError("new native read")
        automator = object.__new__(PortalMacLoginAutomator)
        automator._ax = client
        automator._diagnostics = Mock()
        automator._find_process_pids = Mock(side_effect=AssertionError("new process probe"))
        automator._diagnostic_failure(RuntimeError("failed action"), ("test.bundle",))
        client.find_nodes.assert_not_called()
        client.app.AXUIElementCopyAttributeValue.assert_not_called()
        automator._find_process_pids.assert_not_called()
        event = automator._diagnostics.emit.call_args
        self.assertEqual("app.snapshot", event.args[0])
        self.assertEqual("cached_ax_scan", event.kwargs["source"])
        self.assertEqual(cached, event.kwargs["scan"])


if __name__ == "__main__":
    unittest.main()
