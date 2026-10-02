from __future__ import annotations

import hashlib
import tempfile
import unittest
from itertools import count
from pathlib import Path
from unittest.mock import Mock, patch

from app.photos_qr_cleanup import ImportedPhotosQr
from app.portal_local_login import AXNode, MacAccessibilityClient, PortalLocalLoginError, PortalMacLoginAutomator


def node(element, label, *, role="AXStaticText", parent=10, position=None, size=None):
    return AXNode(element, role, "", (label,), position, size, parent)


class PortalControlActionsTests(unittest.TestCase):
    def setUp(self):
        # No framework loading, active app reads, screen capture or native actions.
        self.a = object.__new__(PortalMacLoginAutomator)
        self.a._diagnostics = None
        self.a._ax = Mock(spec=MacAccessibilityClient)
        self.a._ax.node_enabled.return_value = True
        self.a._ax._perform_action.return_value = 0
        self.a._ax.same_element.side_effect = lambda left, right: left.element == right.element
        self.a._activate_application = Mock()
        self.a._wait_before_bundle_click = Mock()
        self.a._find_process_pids = Mock(return_value=[42])
        self.a._run_command = Mock()
        self.a._log = Mock()
        self.a.imported_qr = None
        for mocked in [patch("app.portal_local_login.sleep"),
                       patch("app.portal_local_login.monotonic", side_effect=count(0, 0.25).__next__)]:
            mocked.start()
            self.addCleanup(mocked.stop)

    def tearDown(self):
        self.a._ax.click_at.assert_not_called()
        self.a._ax.click_node.assert_not_called()
        self.a._ax.find_focused_nodes.assert_not_called()

    def test_role_confirm_uses_heading_and_parent_not_first_confirm(self):
        self.a._ax.find_nodes.return_value = [node(1, "确认"), node(2, "请选择身份类型"),
                                             node(3, "确认"), node(4, "确认", parent=99)]
        self.a._confirm_role_dialog("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(3, "AXPress")

    def test_role_confirmation_waits_until_enabled(self):
        self.a._ax.find_nodes.return_value = [node(1, "请选择身份类型"), node(2, "确认")]
        self.a._ax.node_enabled.side_effect = [False, True]
        self.a._confirm_role_dialog("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(2, "AXPress")

    def test_role_confirmation_never_presses_behind_success_or_fingerprint(self):
        for blocker in ("切换成功", "是否开启指纹快捷登录？"):
            with self.subTest(blocker=blocker):
                self.a._ax.find_nodes.return_value = [node(1, "请选择身份类型"), node(2, "确认"), node(3, blocker)]
                self.a._confirm_role_dialog("test.bundle")
        self.a._ax._perform_action.assert_not_called()

    def test_role_ambiguous_or_missing_control_fails_without_fallback(self):
        for items in ([], [node(1, "请选择身份类型"), node(2, "确认"), node(3, "确认")]):
            self.a._ax.find_nodes.return_value = items
            with self.assertRaisesRegex(PortalLocalLoginError, "unique enabled"):
                self.a._confirm_role_dialog("test.bundle")
        self.a._ax._perform_action.assert_not_called()

    def test_role_native_error_is_not_a_coordinate_click(self):
        self.a._ax.find_nodes.return_value = [node(1, "请选择身份类型"), node(2, "确认")]
        self.a._ax._perform_action.return_value = -25204
        with self.assertRaisesRegex(PortalLocalLoginError, "AXPress.*-25204"):
            self.a._confirm_role_dialog("test.bundle")
        self.a._ax._perform_action.assert_called_once()

    def test_fingerprint_skip_waits_for_real_home_after_unknown_reads(self):
        self.a._ax.find_nodes.side_effect = [
            [node(1, "是否开启指纹快捷登录？"), node(2, "暂不设置", role="AXButton"), node(3, "开启", role="AXButton")],
            [], [node(4, "立即登录")], [node(5, "功能名称"), node(6, "身份切换")],
        ]
        self.a._dismiss_fingerprint_prompt("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(2, "AXPress")
        self.assertEqual(4, self.a._ax.find_nodes.call_count)

    def test_fingerprint_action_return_without_dismissal_is_failure(self):
        self.a._ax.find_nodes.return_value = [node(1, "指纹快捷登录"), node(2, "暂不设置", role="AXButton")]
        with self.assertRaisesRegex(PortalLocalLoginError, "verifying fingerprint"):
            self.a._dismiss_fingerprint_prompt("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(2, "AXPress")

    @staticmethod
    def home_scan_nodes(offset=(0, 0), scale=1):
        def geometry(x, y):
            return (offset[0] + scale * x, offset[1] + scale * y)
        anchor = node(1, "功能名称", position=geometry(100, 100), size=(40 * scale, 12 * scale))
        scan = node(2, "", role="AXButton", position=geometry(200, 90), size=(36 * scale, 32 * scale))
        left = node(3, "", role="AXButton", position=geometry(20, 90), size=(36 * scale, 32 * scale))
        background = node(4, "", role="AXButton", position=geometry(200, 20), size=(300 * scale, 200 * scale))
        other_parent = node(5, "", role="AXButton", parent=99, position=scan.position, size=scan.size)
        window_button = node(6, "", role="AXButton", position=scan.position, size=scan.size)
        window_button.subrole = "AXCloseButton"
        return [anchor, scan, left, background, other_parent, window_button]

    def test_home_scanner_uses_small_sibling_control_when_moved_or_scaled(self):
        for offset, scale in [((0, 0), 1), ((1152, 169), 1), ((300, 200), 2)]:
            with self.subTest(offset=offset, scale=scale):
                self.a._ax._perform_action.reset_mock()
                self.a._ax.find_nodes.return_value = self.home_scan_nodes(offset, scale)
                self.a._click_etax_scan_icon("test.bundle")
                self.a._ax._perform_action.assert_called_once_with(2, "AXPress")

    def test_home_scanner_waits_for_delayed_enabled_control(self):
        self.a._ax.find_nodes.side_effect = [[], self.home_scan_nodes(), self.home_scan_nodes()]
        self.a._ax.node_enabled.side_effect = [False, True]
        self.a._click_etax_scan_icon("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(2, "AXPress")

    def test_home_scanner_refuses_missing_or_ambiguous_control(self):
        base = self.home_scan_nodes()
        for nodes in [[], base[1:], base + [node(7, "", role="AXButton", position=(250, 90), size=(36, 32))],
                      base + [node(8, "功能名称", position=(100, 100), size=(40, 12))]]:
            with self.subTest(nodes=len(nodes)):
                self.a._ax.find_nodes.return_value = nodes
                with self.assertRaisesRegex(PortalLocalLoginError, "unique enabled home scan button"):
                    self.a._click_etax_scan_icon("test.bundle")
        self.a._ax._perform_action.assert_not_called()

    def test_home_scanner_requires_anchor_parent_and_same_row(self):
        for variant in ("no parent", "wrong row", "no geometry"):
            with self.subTest(variant=variant):
                nodes = self.home_scan_nodes()[:2]
                if variant == "no parent":
                    nodes[0].parent_element = None
                elif variant == "wrong row":
                    nodes[1].position = (200, 300)
                else:
                    nodes[0].size = None
                self.assertIsNone(self.a._home_scan_icon_node(nodes))

    def test_home_scanner_native_error_never_uses_mouse_fallback(self):
        self.a._ax.find_nodes.return_value = self.home_scan_nodes()
        self.a._ax._perform_action.return_value = -25206
        with self.assertRaisesRegex(PortalLocalLoginError, "AXPress.*-25206"):
            self.a._click_etax_scan_icon("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(2, "AXPress")

    def test_scan_flow_verifies_page_before_opening_album(self):
        events = []
        self.a._open_home_tab = Mock(side_effect=lambda b: events.append("home"))
        self.a._click_etax_scan_icon = Mock(side_effect=lambda b: events.append("AXPress"))
        self.a._wait_for_scan_page_ready = Mock(side_effect=lambda b: events.append("scanner ready"))
        self.a._open_album_from_scan_page = Mock(side_effect=lambda b: events.append("album"))
        self.a._maybe_click_named_element = Mock(side_effect=AssertionError("Old name-only route must not run"))
        self.a._open_scan_flow("test.bundle")
        self.assertEqual(["home", "AXPress", "scanner ready", "album"], events)

    def test_scan_flow_stops_if_action_or_page_verification_fails(self):
        for failed_stage in ("action", "page"):
            with self.subTest(stage=failed_stage):
                self.a._open_home_tab = Mock()
                self.a._click_etax_scan_icon = Mock(side_effect=PortalLocalLoginError("action failed") if failed_stage == "action" else None)
                self.a._wait_for_scan_page_ready = Mock(side_effect=PortalLocalLoginError("page missing") if failed_stage == "page" else None)
                self.a._open_album_from_scan_page = Mock()
                with self.assertRaises(PortalLocalLoginError):
                    self.a._open_scan_flow("test.bundle")
                self.a._open_album_from_scan_page.assert_not_called()

    def prepare_qr(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = Path(temp.name) / "source.png"
        source.write_bytes(b"isolated QR fixture; decoder mocked in workflow tests")
        self.a.imported_qr = ImportedPhotosQr(source, "test-asset", source.name,
                                             hashlib.sha256(source.read_bytes()).hexdigest(), 100, 100)
        self.bounds = (100, 50, 400, 600)
        self.match = {"x": 0.7, "y": 0.2, "width": 0.1, "height": 0.1}
        self.images = [node(1, "照片"), node(2, "精选集"),
                       node(3, "PXGGridLayout-Info", role="AXImage", position=(100, 100), size=(100, 100)),
                       node(4, "PXGGridLayout-Info", role="AXImage", position=(350, 150), size=(100, 100))]
        self.a._ax.find_nodes.return_value = self.images
        self.a._ax.window_capture_target.return_value = (9, self.bounds)
        self.a._wait_for_login_confirmation_ready = Mock()
        helper = patch("app.portal_local_login.ensure_qr_match_helper", return_value=Path(temp.name) / "helper")
        self.ensure = helper.start()
        self.addCleanup(helper.stop)
        match = patch("app.portal_local_login.match_qr_image", return_value=[self.match])
        self.match_image = match.start()
        self.addCleanup(match.stop)

    def test_qr_content_selects_matching_image_not_first_shared_identifier(self):
        self.prepare_qr()
        self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(4, "AXPress")
        self.a._wait_for_login_confirmation_ready.assert_called_once_with("test.bundle")
        command = self.a._run_command.call_args.args[0]
        self.assertEqual(["/usr/sbin/screencapture", "-x", "-o", "-l", "9"], command[:-1])
        self.assertFalse(self.match_image.call_args.args[1].exists(), "Temporary QR copy should be removed")

    def test_qr_duplicate_matches_refuse_selection(self):
        self.prepare_qr()
        self.match_image.return_value = [self.match, self.match]
        with self.assertRaisesRegex(PortalLocalLoginError, "Multiple visible"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_not_called()

    def test_qr_repeated_same_reference_selects_once(self):
        self.prepare_qr()
        self.a._ax.find_nodes.return_value = self.images + [self.images[-1]]
        self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(4, "AXPress")

    def test_qr_native_aliases_with_different_parents_select_once(self):
        self.prepare_qr()
        alias = node(40, "PXGGridLayout-Info", role="AXImage", parent=99,
                     position=(350, 150), size=(100, 100))
        self.a._ax.find_nodes.return_value = self.images + [alias]
        self.a._ax.same_element.side_effect = lambda left, right: {left.element, right.element} <= {4, 40}
        self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(4, "AXPress")
        self.a._wait_for_login_confirmation_ready.assert_called_once_with("test.bundle")
        messages = "\n".join(call.args[0] for call in self.a._log.call_args_list)
        self.assertIn("reason=candidate_ready", messages)
        self.assertIn("raw_candidate_count=2", messages)
        self.assertIn("unique_candidate_count=1", messages)

    def test_qr_aliases_do_not_hide_a_second_real_control(self):
        self.prepare_qr()
        self.a._ax.find_nodes.return_value = self.images + [
            node(40, "PXGGridLayout-Info", role="AXImage", parent=99, position=(350, 150), size=(100, 100)),
            node(41, "PXGGridLayout-Info", role="AXImage", parent=99, position=(350, 150), size=(100, 100)),
        ]
        self.a._ax.same_element.side_effect = lambda left, right: (
            left.element == right.element or {left.element, right.element} <= {4, 40}
        )
        with self.assertRaisesRegex(PortalLocalLoginError, "ambiguous_ax_images"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_not_called()
        messages = "\n".join(call.args[0] for call in self.a._log.call_args_list)
        self.assertIn("raw_candidate_count=3", messages)
        self.assertIn("unique_candidate_count=2", messages)

    def test_qr_null_reference_is_never_selected_or_compared(self):
        self.prepare_qr()
        counts = {}
        invalid = node(0, "PXGGridLayout-Info", role="AXImage", position=(350, 150), size=(100, 100))
        self.assertIsNone(self.a._qr_image_node([invalid], self.match, self.bounds, counts=counts))
        self.assertEqual({"raw_candidate_count": 0, "unique_candidate_count": 0}, counts)
        self.a._ax.same_element.assert_not_called()

    def test_qr_alias_dedup_does_not_bypass_enabled_check(self):
        self.prepare_qr()
        self.a._ax.find_nodes.return_value = self.images + [
            node(40, "PXGGridLayout-Info", role="AXImage", parent=99, position=(350, 150), size=(100, 100)),
        ]
        self.a._ax.same_element.side_effect = lambda left, right: {left.element, right.element} <= {4, 40}
        self.a._ax.node_enabled.return_value = False
        with self.assertRaisesRegex(PortalLocalLoginError, "control_not_enabled"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_not_called()

    def test_qr_rejection_logging_is_bounded_and_explains_timeout(self):
        self.prepare_qr()
        self.match_image.return_value = []
        with self.assertRaisesRegex(PortalLocalLoginError, "no_visual_match"):
            self.a._select_latest_qr_from_album("test.bundle")
        messages = [call.args[0] for call in self.a._log.call_args_list
                    if call.args[0].startswith("QR image selection")]
        self.assertEqual(2, len(messages))
        self.assertIn("reason=no_visual_match", messages[0])
        self.assertIn("reason=timeout", messages[1])
        self.assertIn("last_reason=no_visual_match", messages[1])
        self.assertNotIn("isolated QR fixture", "\n".join(messages))

    def test_qr_diagnostic_failures_do_not_escape(self):
        self.a._diagnostics = Mock()
        self.a._diagnostics.emit.side_effect = RuntimeError("diagnostics unavailable")
        self.a._log.side_effect = RuntimeError("logger unavailable")
        self.a._record_qr_selection({"reason": "no_visual_match", "match_count": 0})
        self.a._diagnostics.emit.assert_called_once_with("qr.selection", reason="no_visual_match", match_count=0)

    def test_qr_final_slow_scan_preserves_prior_rejection_and_counts(self):
        self.prepare_qr()
        self.a._ax.find_nodes.return_value = self.images + [
            node(40, "PXGGridLayout-Info", role="AXImage", position=(350, 150), size=(100, 100)),
        ]
        now, attempts = [0.0], [0]
        def match(*args):
            attempts[0] += 1
            if attempts[0] == 2:
                now[0] = 21.0
            return [self.match]
        self.match_image.side_effect = match
        with patch("app.portal_local_login.monotonic", side_effect=lambda: now[0]), patch(
            "app.portal_local_login.sleep", side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)
        ):
            with self.assertRaisesRegex(PortalLocalLoginError, "ambiguous_ax_images"):
                self.a._select_latest_qr_from_album("test.bundle")
        messages = [call.args[0] for call in self.a._log.call_args_list
                    if call.args[0].startswith("QR image selection")]
        self.assertEqual(3, len(messages))
        self.assertIn("reason=deadline_before_selection", messages[-2])
        self.assertIn("last_reason=ambiguous_ax_images", messages[-1])
        self.assertIn("raw_candidate_count=2", messages[-1])
        self.assertIn("unique_candidate_count=2", messages[-1])
        self.a._ax._perform_action.assert_not_called()

    def test_qr_missing_matches_or_ambiguous_ax_images_never_guess(self):
        self.prepare_qr()
        self.match_image.return_value = []
        with self.assertRaisesRegex(PortalLocalLoginError, "locating the verified"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.match_image.return_value = [self.match]
        self.a._ax.find_nodes.return_value = self.images + [node(5, "PXGGridLayout-Info", role="AXImage", position=(350, 150), size=(100, 100))]
        with self.assertRaisesRegex(PortalLocalLoginError, "locating the verified"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_not_called()

    def test_qr_changed_source_stops_before_capture(self):
        self.prepare_qr()
        self.a.imported_qr.qr_path.write_bytes(b"changed image")
        with self.assertRaisesRegex(PortalLocalLoginError, "changed after"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._run_command.assert_not_called()
        self.a._ax._perform_action.assert_not_called()

    def test_qr_window_movement_invalidates_image_match(self):
        self.prepare_qr()
        targets = iter([(9, self.bounds), (9, (200, 50, 400, 600))])
        self.a._ax.window_capture_target.side_effect = lambda pid: next(targets, None)
        with self.assertRaisesRegex(PortalLocalLoginError, "locating the verified"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_not_called()

    def test_qr_missing_login_confirmation_does_not_select_another_image(self):
        self.prepare_qr()
        self.a._wait_for_login_confirmation_ready.side_effect = PortalLocalLoginError("no login confirmation")
        with self.assertRaisesRegex(PortalLocalLoginError, "no login confirmation"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(4, "AXPress")
        self.match_image.assert_called_once()

    def test_qr_action_error_does_not_fall_back_or_wait_for_login(self):
        self.prepare_qr()
        self.a._ax._perform_action.return_value = -25204
        with self.assertRaisesRegex(PortalLocalLoginError, "AXPress.*-25204"):
            self.a._select_latest_qr_from_album("test.bundle")
        self.a._ax._perform_action.assert_called_once_with(4, "AXPress")
        self.a._wait_for_login_confirmation_ready.assert_not_called()
