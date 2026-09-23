from __future__ import annotations

import ctypes
import gc
import unittest
import weakref
from collections import Counter
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.portal_local_login import (
    AXNode,
    AX_ERROR_CANNOT_COMPLETE,
    MacAccessibilityClient,
    PortalAccessibilityError,
    PortalMacLoginAutomator,
)
from app.portal_runner import TaxPortalRunner


class FakeCoreFoundation:
    """Owned CF values and fresh references to logically identical AX elements."""

    ARRAY = 1
    STRING = 2
    ELEMENT = 3

    def __init__(self):
        self.values = {}
        self.references = Counter()
        self.next_pointer = 1
        self.force_hash_collision = False

    def allocate(self, kind, value):
        pointer = self.next_pointer
        self.next_pointer += 1
        self.values[pointer] = (kind, value)
        self.references[pointer] = 1
        return pointer

    def element(self, name):
        return self.allocate(self.ELEMENT, name)

    def string(self, value):
        return self.allocate(self.STRING, value)

    def array(self, names):
        # The array owns each freshly created child until the array is released.
        return self.allocate(self.ARRAY, [self.element(name) for name in names])

    def CFGetTypeID(self, pointer):
        return self.values[pointer][0]

    def CFArrayGetCount(self, pointer):
        return len(self.values[pointer][1])

    def CFArrayGetValueAtIndex(self, pointer, index):
        return self.values[pointer][1][index]

    def CFRetain(self, pointer):
        assert self.references[pointer] > 0, "retaining a freed CF value"
        self.references[pointer] += 1
        return pointer

    def CFRelease(self, pointer):
        assert self.references[pointer] > 0, "double release"
        self.references[pointer] -= 1
        kind, value = self.values[pointer]
        if not self.references[pointer] and kind == self.ARRAY:
            for child in value:
                self.CFRelease(child)

    def CFHash(self, pointer):
        assert self.references[pointer] > 0, "hashing a freed element"
        return 1 if self.force_hash_collision else hash(self.values[pointer])

    def CFEqual(self, left, right):
        assert self.references[left] > 0 and self.references[right] > 0
        return self.values[left] == self.values[right]


class FakeAXApplication:
    def __init__(self, core, children):
        self.core = core
        self.children = children
        self.timeouts = []
        self.read_calls = []
        self.action_calls = []
        self.write_calls = []
        self.action_result = 0
        self.write_result = 0
        self.read_result = 0
        self.timeout_result = 0
        self.on_read = lambda: None

    def AXUIElementCreateApplication(self, pid):
        return self.core.element("application")

    def AXUIElementSetMessagingTimeout(self, element, timeout):
        self.timeouts.append((element, float(timeout)))
        return self.timeout_result

    def AXUIElementCopyAttributeValue(self, element, attribute, output):
        name = self.core.values[element][1]
        attr = self.core.values[attribute][1]
        self.read_calls.append((name, attr))
        self.on_read()
        if self.read_result:
            return self.read_result
        if attr == "AXWindows" and name == "application":
            value = self.core.array([0])
        elif attr == "AXChildren":
            value = self.core.array(self.children(name))
        elif attr == "AXRole":
            value = self.core.string("AXWindow" if name == 0 else "AXButton")
        elif attr == "AXTitle":
            value = self.core.string(f"node {name}")
        else:
            return -25212  # kAXErrorNoValue is an ordinary absent attribute.
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = value
        return 0

    def AXUIElementPerformAction(self, element, action):
        self.action_calls.append((element, self.core.values[action][1]))
        return self.action_result

    def AXUIElementSetAttributeValue(self, element, attribute, value):
        self.write_calls.append((element, self.core.values[attribute][1]))
        return self.write_result


class AdapterClient(MacAccessibilityClient):
    """Use the real scan, CF ownership, attribute, deadline and action code."""

    def __init__(self, children=lambda _: ()):
        self.core = FakeCoreFoundation()
        self.app = FakeAXApplication(self.core, children)
        self._cf_array_type = self.core.ARRAY
        self._cf_string_type = self.core.STRING
        self._diagnostics = None
        self._active_scan = None
        self._deadline = None
        self._scan_sequence = 0
        self._last_scan = {}
        self._last_pid = None

    def _cf_string(self, value):
        return self.core.string(value)

    def _cf_to_text(self, pointer):
        return self.core.values[pointer][1]


def make_automator(client):
    automator = object.__new__(PortalMacLoginAutomator)
    automator._ax = client
    automator._diagnostics = None
    automator._activate_application = Mock()
    automator._find_process_pids = Mock(return_value=[42])
    automator._logger = Mock()
    automator.store_key = "test"
    return automator


class PortalAccessibilityTests(unittest.TestCase):
    def test_logically_equal_nodes_with_different_pointers_stop_cycle(self):
        client = AdapterClient(lambda name: [1] if name == 0 else [0])
        nodes = client.find_nodes(42)
        self.assertEqual(["node 0", "node 1"], [node.texts[0] for node in nodes])
        self.assertEqual(2, client.diagnostic_state()["node_count"])
        self.assertEqual(1, client.diagnostic_state()["duplicate_count"])
        roots = [pointer for pointer, value in client.core.values.items()
                 if value == (client.core.ELEMENT, 0)]
        self.assertEqual(2, len(roots))
        self.assertNotEqual(*roots)

    def test_hash_collision_does_not_discard_distinct_elements(self):
        client = AdapterClient(lambda name: [1, 2] if name == 0 else [])
        client.core.force_hash_collision = True
        nodes = client.find_nodes(42)
        self.assertEqual(3, len(nodes))
        self.assertEqual(0, client.diagnostic_state()["duplicate_count"])

    def test_infinite_unique_chain_hits_total_node_budget(self):
        client = AdapterClient(lambda name: [name + 1])
        with patch("app.portal_local_login.AX_SCAN_MAX_NODES", 4), patch(
            "app.portal_local_login.AX_SCAN_MAX_DEPTH", 100
        ):
            with self.assertRaisesRegex(PortalAccessibilityError, "nodes=4"):
                client.find_nodes(42)
        self.assertEqual(4, client.diagnostic_state()["node_count"])
        self.assertLess(len(client.app.read_calls), 50)

    def test_deep_tree_hits_depth_budget(self):
        client = AdapterClient(lambda name: [name + 1])
        with patch("app.portal_local_login.AX_SCAN_MAX_DEPTH", 2):
            with self.assertRaisesRegex(PortalAccessibilityError, "depth=3"):
                client.find_nodes(42)
        self.assertEqual(3, client.diagnostic_state()["node_count"])

    def test_wide_child_array_is_rejected_before_traversing_children(self):
        client = AdapterClient(lambda name: range(1, 7) if name == 0 else [])
        with patch("app.portal_local_login.AX_SCAN_MAX_NODES", 5):
            with self.assertRaisesRegex(PortalAccessibilityError, "child list.*count=6"):
                client.find_nodes(42)
        self.assertEqual(1, client.diagnostic_state()["node_count"])
        self.assertFalse(any(name in range(1, 7) for name, _ in client.app.read_calls))

    def test_remaining_time_is_passed_to_native_calls_and_scan_stops(self):
        client = AdapterClient()
        clock = SimpleNamespace(now=100.0)
        client.app.on_read = lambda: setattr(clock, "now", clock.now + 0.1)
        with patch("app.portal_local_login.monotonic", side_effect=lambda: clock.now):
            with client.bounded(0.25):
                with self.assertRaisesRegex(PortalAccessibilityError, "time budget"):
                    client.find_nodes(42)
        timeouts = [seconds for _, seconds in client.app.timeouts]
        self.assertEqual(3, len(timeouts))
        for actual, expected in zip(timeouts, [0.25, 0.15, 0.05]):
            self.assertAlmostEqual(expected, actual)
        self.assertIsNone(client._deadline)
        self.assertIsNone(client._active_scan)

    def test_native_timeout_never_exceeds_one_second(self):
        client = AdapterClient()
        with client.bounded(30):
            nodes = client.find_nodes(42)
        self.assertTrue(nodes)
        self.assertTrue(all(0 < seconds <= 1.0 for _, seconds in client.app.timeouts))

    def test_cannot_set_timeout_prevents_native_read(self):
        client = AdapterClient()
        client.app.timeout_result = -25200
        with self.assertRaisesRegex(PortalAccessibilityError, "Cannot set AX messaging timeout"):
            client.find_nodes(42)
        self.assertEqual([], client.app.read_calls)

    def test_action_cannot_complete_does_not_repeat_coordinate_click(self):
        client = AdapterClient()
        node = client.find_nodes(42)[0]
        # Old ctypes bindings yield the unsigned representation of AXError.
        client.app.action_result = ctypes.c_uint32(AX_ERROR_CANNOT_COMPLETE).value
        with patch.object(client, "click_at") as click:
            with self.assertRaisesRegex(PortalAccessibilityError, "outcome unconfirmed"):
                client.click_node(node)
        click.assert_not_called()
        self.assertEqual(1, len(client.app.action_calls))

    def test_write_cannot_complete_does_not_retry_keyboard_or_coordinate(self):
        client = AdapterClient()
        node = client.find_nodes(42)[0]
        client.app.write_result = AX_ERROR_CANNOT_COMPLETE
        automator = make_automator(client)
        with patch.object(automator, "_find_text_field_node", return_value=node), patch.object(
            automator, "_click_login_field_relative"
        ) as relative_click, patch.object(client, "click_node") as click, patch.object(
            client, "send_modified_key"
        ) as key, patch.object(client, "type_text") as text:
            with self.assertRaises(PortalAccessibilityError):
                automator._set_login_account_value("test.bundle", "test-account")
        relative_click.assert_not_called()
        click.assert_not_called()
        key.assert_not_called()
        text.assert_not_called()
        self.assertEqual(1, len(client.app.write_calls))

    def test_node_reference_keeps_entire_scan_lease_alive_until_last_node_dies(self):
        client = AdapterClient(lambda name: [1] if name == 0 else [])
        nodes = client.find_nodes(42)
        survivor = nodes[1]
        lease = weakref.ref(survivor._owner)
        del nodes
        gc.collect()
        self.assertIsNotNone(lease())
        self.assertGreater(client.core.references[survivor.element], 0)
        self.assertTrue(client.click_node(survivor))
        del survivor
        gc.collect()
        self.assertIsNone(lease())
        self.assertEqual(0, sum(client.core.references.values()))

    def test_failed_scan_releases_owned_references(self):
        client = AdapterClient(lambda name: [name + 1])
        with patch("app.portal_local_login.AX_SCAN_MAX_NODES", 2):
            with self.assertRaises(PortalAccessibilityError):
                client.find_nodes(42)
        gc.collect()
        self.assertEqual(0, sum(client.core.references.values()))

    def test_native_read_error_survives_optional_element_helper(self):
        client = AdapterClient()
        client.app.read_result = AX_ERROR_CANNOT_COMPLETE
        automator = make_automator(client)
        with patch.object(client, "click_node") as click:
            with self.assertRaisesRegex(PortalAccessibilityError, "ax.read"):
                automator._maybe_click_named_element("test.bundle", ("登录",), timeout_seconds=1)
        click.assert_not_called()
        self.assertEqual(1, len(client.app.read_calls))

    def test_recovery_helpers_do_not_hide_accessibility_failure(self):
        cases = [
            ("_startup_reminder_visible_from_ax_text", "_collect_visible_texts", ("test.bundle",)),
            ("_ocr_startup_reminder_visible", "_capture_startup_reminder_screenshot", ("test.bundle",)),
            ("_ocr_home_portal_area_text", "_capture_home_portal_area_screenshot", ("test.bundle",)),
            ("_select_latest_qr_from_album", "_select_latest_qr_in_internal_picker", ("test.bundle",)),
            ("_is_scan_page_visible", "_collect_visible_texts", ("test.bundle",)),
            ("_is_internal_photo_picker_visible", "_collect_visible_texts", ("test.bundle",)),
            ("_is_photos_picker_visible", "_collect_visible_texts", ()),
            ("_is_login_confirmation_visible", "_collect_visible_texts", ("test.bundle",)),
            ("_select_role_option", "_click_named_element", ("test.bundle",)),
            ("_confirm_role_dialog", "_click_named_element", ("test.bundle",)),
            ("_dismiss_fingerprint_prompt", "_click_named_element", ("test.bundle",)),
            ("_confirm_switch_success_dialog", "_collect_visible_texts", ("test.bundle",)),
        ]
        for method, dependency, arguments in cases:
            with self.subTest(method=method):
                automator = make_automator(AdapterClient())
                automator.role_label = "法定代表人"
                error = PortalAccessibilityError("native timeout")
                with ExitStack() as stack:
                    stack.enter_context(patch.object(automator, dependency, side_effect=error))
                    stack.enter_context(patch.object(automator, "_click_switch_success_dialog_confirm_relative"))
                    fallback = stack.enter_context(patch.object(automator, "_click_at_for_bundle"))
                    with self.assertRaises(PortalAccessibilityError) as raised:
                        getattr(automator, method)(*arguments)
                    self.assertIs(error, raised.exception)
                    fallback.assert_not_called()

    def test_runner_stops_instead_of_falling_back_to_manual_login(self):
        runner = object.__new__(TaxPortalRunner)
        runner.config = SimpleNamespace()
        runner._diagnostics = None
        runner._log = Mock()
        result = SimpleNamespace(store_key="test", portal_company_role="legal_representative", artifacts_dir=None)
        error = PortalAccessibilityError("native timeout")
        error.portal_diagnostic_step = "app_wait_post_login"
        with patch("app.portal_runner.PortalMacLoginAutomator") as constructor:
            constructor.return_value.automate.side_effect = error
            with self.assertRaises(PortalAccessibilityError) as raised:
                runner._attempt_local_app_login(object(), result)
        self.assertIs(error, raised.exception)
        self.assertEqual("app_wait_post_login", raised.exception.portal_diagnostic_step)
        self.assertFalse(any("manual" in call.args[1] for call in runner._log.call_args_list))

    def test_failure_snapshot_uses_cached_scan_without_any_native_calls(self):
        client = AdapterClient()
        nodes = client.find_nodes(42)
        cached = client.diagnostic_state()
        automator = make_automator(client)
        automator._diagnostics = Mock()
        with patch.object(client.app, "AXUIElementCopyAttributeValue", side_effect=AssertionError("new AX read")), patch.object(
            client, "find_nodes", side_effect=AssertionError("new scan")
        ):
            automator._diagnostic_failure(PortalAccessibilityError("native timeout"), ("test.bundle",))
        event = automator._diagnostics.emit.call_args
        self.assertEqual("app.snapshot", event.args[0])
        self.assertEqual("cached_ax_scan", event.kwargs["source"])
        self.assertEqual(cached, event.kwargs["scan"])
        self.assertTrue(nodes)


if __name__ == "__main__":
    unittest.main()
