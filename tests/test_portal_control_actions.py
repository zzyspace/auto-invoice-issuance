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
