from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.portal_local_login import AXNode, MacAccessibilityClient, PortalLocalLoginError, PortalMacLoginAutomator


def node(element, label, role="AXStaticText", size=(40.0, 40.0)):
    return AXNode(element, role, "", (label,), (100.0, 200.0), size, 10)


def picker():
    # Real picker has no "取消" or "搜索你的图库"; image identifiers are exposed as AX text.
    return [node(1, "照片", "AXTabGroup"), node(2, "精选集", "AXRadioButton"),
            node(3, "PXGGridLayout-Info", "AXImage")]


def scanner():
    return [node(1, "识别二维码"), node(2, "相册"), node(3, "扫一扫")]


class PortalAlbumEntryTests(unittest.TestCase):
    def setUp(self):
        # Isolated clock and AX snapshots: no app activation, clicks or screen reads.
        self.now = 0.0
        self.a = object.__new__(PortalMacLoginAutomator)
        self.a._diagnostics = None
        self.a._ax = Mock(spec=MacAccessibilityClient)
        self.a._find_process_pids = Mock(return_value=[42])
        self.a._activate_application = Mock()
        self.a._click_scan_album_region = Mock()
        self.a._log = Mock()
        for mocked in [patch("app.portal_local_login.monotonic", side_effect=lambda: self.now),
                       patch("app.portal_local_login.sleep", side_effect=self.advance)]:
            mocked.start()
            self.addCleanup(mocked.stop)

    def advance(self, seconds):
        self.now += seconds

    def tearDown(self):
        self.a._ax.find_focused_nodes.assert_not_called()
        self.a._ax._perform_action.assert_not_called()  # Never use the ineffective text AXPress.

    def test_single_click_waits_for_slow_picker_without_reclick(self):
        for ready_at in (3.5, 4.0, 9.0):
            with self.subTest(ready_at=ready_at):
                self.now = 0
                self.a._click_scan_album_region.reset_mock()
                self.a._ax.find_nodes.side_effect = lambda pid: picker() if self.now >= ready_at else scanner()
                self.a._open_album_from_scan_page("test.bundle")
                self.a._click_scan_album_region.assert_called_once_with("test.bundle")
                self.assertGreaterEqual(self.now, ready_at)
                self.assertLessEqual(self.now, ready_at + 0.21)

    def test_scanner_disappearing_or_empty_shell_is_not_success(self):
        cases = {"empty AX read": [], "still scanner": scanner(), "picker shell only": picker()[:2],
                 "unrelated image": picker()[:2] + [node(4, "banner", "AXImage")],
                 "image without picker shell": picker()[2:],
                 "unlaid-out grid": picker()[:2] + [node(3, "PXGGridLayout-Info", "AXImage", size=(0, 40))]}
        for name, nodes in cases.items():
            with self.subTest(name=name):
                self.now = 0
                self.a._click_scan_album_region.reset_mock()
                self.a._ax.find_nodes.return_value = nodes
                with self.assertRaisesRegex(PortalLocalLoginError, "after one album icon click"):
                    self.a._open_album_from_scan_page("test.bundle")
                self.a._click_scan_album_region.assert_called_once_with("test.bundle")
                self.assertAlmostEqual(10.0, self.now)

    def test_read_errors_and_loading_are_reobserved_until_grid_ready(self):
        self.a._ax.find_nodes.side_effect = [PortalLocalLoginError("temporary read failure"), [], scanner(), picker()[:2], picker()]
        self.a._open_album_from_scan_page("test.bundle")
        self.a._click_scan_album_region.assert_called_once_with("test.bundle")
        self.assertEqual(5, self.a._ax.find_nodes.call_count)

    def test_existing_picker_does_not_receive_another_click(self):
        self.a._ax.find_nodes.return_value = picker()
        self.a._open_album_from_scan_page("test.bundle")
        self.a._click_scan_album_region.assert_not_called()
        self.assertEqual(0, self.now)

    def test_other_foreground_picker_is_not_adopted(self):
        self.a._find_process_pids.return_value = []
        self.a._ax.find_focused_nodes.return_value = picker()
        with self.assertRaises(PortalLocalLoginError):
            self.a._open_album_from_scan_page("test.bundle")
        self.a._ax.find_nodes.assert_not_called()
        self.a._click_scan_album_region.assert_called_once()

    def test_picker_read_returning_after_deadline_is_not_accepted(self):
        snapshots = iter([scanner()])
        def read(pid):
            first = next(snapshots, None)
            if first is not None:
                return first
            self.now = 11.0
            return picker()
        self.a._ax.find_nodes.side_effect = read
        with self.assertRaisesRegex(PortalLocalLoginError, "after one album icon click"):
            self.a._open_album_from_scan_page("test.bundle")
        self.a._click_scan_album_region.assert_called_once()

    def test_icon_click_error_is_not_retried(self):
        self.a._ax.find_nodes.return_value = scanner()
        self.a._click_scan_album_region.side_effect = PortalLocalLoginError("click failed")
        with self.assertRaisesRegex(PortalLocalLoginError, "click failed"):
            self.a._open_album_from_scan_page("test.bundle")
        self.a._click_scan_album_region.assert_called_once()
