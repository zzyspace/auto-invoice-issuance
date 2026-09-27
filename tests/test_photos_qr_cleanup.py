from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.photos_qr_cleanup import (
    ImportedPhotosQr,
    PHOTOS_CLEANUP_BUNDLE_ID,
    PHOTOS_CLEANUP_DISPLAY_NAME,
    PHOTOS_CLEANUP_TIMEOUT_SECONDS,
    PHOTOS_ASSET_EXISTS_SCRIPT,
    PHOTOS_DELETE_CONFIRM_SCRIPT,
    PhotosQrCleanupError,
    _start_delete_confirmation_watcher,
    _stop_delete_confirmation_watcher,
    delete_imported_qr_from_photos,
    describe_imported_qr,
)


def png_bytes(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


class PhotosQrCleanupTests(unittest.TestCase):
    def test_transcript_is_live_and_large_output_does_not_fill_a_pipe(self) -> None:
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "confirmation.log"

            def harmless_child(command, **kwargs):
                self.assertEqual("diagnostics", command[-1])
                self.assertNotEqual(subprocess.PIPE, kwargs["stdout"])
                self.assertEqual(subprocess.STDOUT, kwargs["stderr"])
                return real_popen(
                    [sys.executable, "-u", "-c",
                     "import time; print('x' * 150000, flush=True); "
                     "print('PHOTOS_DIAG event=begin operation=element.value element_index=42', flush=True); "
                     "time.sleep(30)"], **kwargs,
                )

            with patch("app.photos_qr_cleanup.subprocess.Popen", side_effect=harmless_child):
                watcher = _start_delete_confirmation_watcher(log_path=path)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if "element_index=42" in path.read_text():
                        break
                    time.sleep(0.01)
                self.assertIsNone(watcher.poll())
                self.assertIn("element_index=42", path.read_text())
            finally:
                summary = _stop_delete_confirmation_watcher(watcher)
            self.assertGreater(path.stat().st_size, 150000)
            self.assertIn("event=watcher.start", path.read_text())
            self.assertIn("stop_requested=True", path.read_text())
            self.assertIn(str(path), summary)

    def test_success_keeps_watcher_evidence_for_auto_manual_and_watcher_error(self) -> None:
        for running, code, last_line in (
            (False, 0, "dialog-dismissed attempts=1"),
            (True, -15, "PHOTOS_DIAG event=begin operation=element.value element_index=42"),
            (False, 1, "syntax error: simulated watcher failure"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp_dir:
                qr = ImportedPhotosQr(Path(tmp_dir) / "login-qr.png", "asset-id", "login-qr.png", "a" * 64, 240, 240)
                path = qr.qr_path.with_suffix(".photos-confirm.log")
                path.write_text("first-scan-evidence\n" + last_line + "\n")
                watcher = Mock(pid=123, returncode=code)
                watcher._photos_confirmation_log_path = path
                watcher.poll.return_value = None if running else code
                watcher.communicate.return_value = (None, None)
                helper = Path("/tools/photos-qr-cleanup.app/Contents/MacOS/photos-qr-cleanup")
                logger = Mock()
                with (
                    patch("app.photos_qr_cleanup._ensure_photos_cleanup_helper", return_value=helper),
                    patch("app.photos_qr_cleanup._photos_asset_exists", side_effect=[True, False]),
                    patch("app.photos_qr_cleanup._start_delete_confirmation_watcher", return_value=watcher) as start,
                    patch("app.photos_qr_cleanup.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")),
                ):
                    self.assertEqual("deleted", delete_imported_qr_from_photos(qr, logger=logger))
                start.assert_called_once_with(log_path=path)
                self.assertIn("first-scan-evidence", path.read_text())
                self.assertIn(last_line, path.read_text())
                self.assertIn(f"returncode={code}", path.read_text())
                self.assertTrue(any(last_line in call.args[0] for call in logger.call_args_list))
                if running:
                    watcher.terminate.assert_called_once()
                else:
                    watcher.terminate.assert_not_called()

    def test_log_file_failure_disables_verbose_trace_without_preventing_watcher(self) -> None:
        watcher = Mock()
        watcher.poll.return_value = 0
        watcher.communicate.return_value = ("timed-out status=not-found attempts=0", "watching")
        with patch.object(Path, "open", side_effect=PermissionError("read only")), patch(
            "app.photos_qr_cleanup.subprocess.Popen", return_value=watcher
        ) as popen:
            self.assertIs(watcher, _start_delete_confirmation_watcher(log_path=Path("denied.log")))
        self.assertNotIn("diagnostics", popen.call_args.args[0])
        self.assertEqual(subprocess.PIPE, popen.call_args.kwargs["stdout"])
        self.assertIn("diagnostics_file_unavailable=PermissionError", _stop_delete_confirmation_watcher(watcher))

    def test_reporting_failure_does_not_turn_verified_deletion_into_failure(self) -> None:
        qr = ImportedPhotosQr(Path("/tmp/login-qr.png"), "asset-id", "login-qr.png", "a" * 64, 240, 240)
        helper = Path("/tools/photos-qr-cleanup.app/Contents/MacOS/photos-qr-cleanup")
        with (
            patch("app.photos_qr_cleanup._ensure_photos_cleanup_helper", return_value=helper),
            patch("app.photos_qr_cleanup._photos_asset_exists", side_effect=[True, False]),
            patch("app.photos_qr_cleanup._start_delete_confirmation_watcher", return_value=None),
            patch("app.photos_qr_cleanup.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")),
        ):
            self.assertEqual("deleted", delete_imported_qr_from_photos(qr, logger=Mock(side_effect=OSError("disk full"))))

    def test_describe_imported_qr_records_exact_identity_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            qr_path = Path(tmp_dir) / "login-qr-123.png"
            payload = png_bytes(320, 280)
            qr_path.write_bytes(payload)

            imported_qr = describe_imported_qr(qr_path, "asset-id-123")

        self.assertEqual("asset-id-123", imported_qr.asset_id)
        self.assertEqual("login-qr-123.png", imported_qr.original_filename)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), imported_qr.sha256)
        self.assertEqual((320, 280), (imported_qr.width, imported_qr.height))

    def test_describe_imported_qr_rejects_missing_asset_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            qr_path = Path(tmp_dir) / "login-qr.png"
            qr_path.write_bytes(png_bytes(240, 240))

            with self.assertRaisesRegex(PhotosQrCleanupError, "identifier"):
                describe_imported_qr(qr_path, "")

    def test_delete_imported_qr_passes_all_safety_fields_to_helper(self) -> None:
        imported_qr = ImportedPhotosQr(
            qr_path=Path("/tmp/login-qr.png"),
            asset_id="asset-id",
            original_filename="login-qr.png",
            sha256="a" * 64,
            width=240,
            height=241,
        )
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        helper = Path("/tools/photos-qr-cleanup.app/Contents/MacOS/photos-qr-cleanup")
        with patch("app.photos_qr_cleanup._ensure_photos_cleanup_helper", return_value=helper):
            with patch("app.photos_qr_cleanup._photos_asset_exists", side_effect=[True, False]):
                with patch("app.photos_qr_cleanup._start_delete_confirmation_watcher", return_value=object()):
                    with patch(
                        "app.photos_qr_cleanup._stop_delete_confirmation_watcher",
                        return_value="dialog-dismissed attempts=1",
                    ) as mocked_stop:
                        with patch("app.photos_qr_cleanup.subprocess.run", return_value=completed) as mocked_run:
                            status = delete_imported_qr_from_photos(imported_qr)

        self.assertEqual("deleted", status)
        self.assertEqual(
            [
                "/usr/bin/open",
                "-W",
                "-g",
                "/tools/photos-qr-cleanup.app",
                "--args",
                "asset-id",
                "login-qr.png",
                "a" * 64,
                "240",
                "241",
            ],
            mocked_run.call_args.args[0],
        )
        mocked_stop.assert_called_once()

    def test_delete_imported_qr_never_accepts_asset_still_present(self) -> None:
        imported_qr = ImportedPhotosQr(
            qr_path=Path("/tmp/login-qr.png"),
            asset_id="asset-id",
            original_filename="login-qr.png",
            sha256="a" * 64,
            width=240,
            height=240,
        )
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        helper = Path("/tools/photos-qr-cleanup.app/Contents/MacOS/photos-qr-cleanup")
        with patch("app.photos_qr_cleanup._ensure_photos_cleanup_helper", return_value=helper):
            with patch("app.photos_qr_cleanup._photos_asset_exists", side_effect=[True, True]):
                with patch("app.photos_qr_cleanup._start_delete_confirmation_watcher", return_value=None):
                    with patch("app.photos_qr_cleanup.subprocess.run", return_value=completed):
                        with self.assertRaisesRegex(PhotosQrCleanupError, "still present"):
                            delete_imported_qr_from_photos(imported_qr)

    def test_delete_imported_qr_always_stops_watcher_when_helper_fails(self) -> None:
        imported_qr = ImportedPhotosQr(
            qr_path=Path("/tmp/login-qr.png"),
            asset_id="asset-id",
            original_filename="login-qr.png",
            sha256="a" * 64,
            width=240,
            height=240,
        )
        watcher = object()

        helper = Path("/tools/photos-qr-cleanup.app/Contents/MacOS/photos-qr-cleanup")
        with patch("app.photos_qr_cleanup._ensure_photos_cleanup_helper", return_value=helper):
            with patch("app.photos_qr_cleanup._photos_asset_exists", return_value=True):
                with patch("app.photos_qr_cleanup._start_delete_confirmation_watcher", return_value=watcher):
                    with patch(
                        "app.photos_qr_cleanup._stop_delete_confirmation_watcher",
                        return_value="button-disabled",
                    ) as mocked_stop:
                        with patch(
                            "app.photos_qr_cleanup.subprocess.run",
                            side_effect=subprocess.TimeoutExpired(["/usr/bin/open"], 45),
                        ):
                            with self.assertRaisesRegex(
                                PhotosQrCleanupError, "failed to run.*Confirmation watcher: button-disabled"
                            ):
                                delete_imported_qr_from_photos(imported_qr)

        mocked_stop.assert_called_once_with(watcher)

    def test_delete_confirmation_script_requires_exact_helper_and_destructive_dialog(self) -> None:
        self.assertIn(PHOTOS_CLEANUP_BUNDLE_ID, PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn(PHOTOS_CLEANUP_DISPLAY_NAME, PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn('dialogText contains "photos-qr-cleanup-"', PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn(
            'bundle identifier is "com.apple.UserNotificationCenter"',
            PHOTOS_DELETE_CONFIRM_SCRIPT,
        )
        self.assertIn(
            f'bundle identifier is "{PHOTOS_CLEANUP_BUNDLE_ID}"',
            PHOTOS_DELETE_CONFIRM_SCRIPT,
        )
        self.assertIn('whose name is "photos-qr-cleanup"', PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn('bundle identifier is "com.apple.Photos"', PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertNotIn("whose visible is true", PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn('dialogText contains "删除这张照片"', PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn("name of currentWindow", PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn("description of currentWindow", PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn('(elementName as text) is "不允许"', PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn('elementName is "删除"', PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn("click currentElement", PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn('perform action "AXPress"', PHOTOS_DELETE_CONFIRM_SCRIPT)
        self.assertIn("enabled of currentElement", PHOTOS_DELETE_CONFIRM_SCRIPT)

    def test_photos_asset_exists_script_checks_exact_asset_id(self) -> None:
        self.assertIn("item 1 of argv", PHOTOS_ASSET_EXISTS_SCRIPT)
        self.assertIn("exists media item id assetID", PHOTOS_ASSET_EXISTS_SCRIPT)

    def test_start_delete_confirmation_watcher_runs_short_lived_osascript(self) -> None:
        watcher = object()
        with patch("app.photos_qr_cleanup.subprocess.Popen", return_value=watcher) as mocked_popen:
            started = _start_delete_confirmation_watcher()

        self.assertIs(watcher, started)
        self.assertEqual("/usr/bin/osascript", mocked_popen.call_args.args[0][0])
        self.assertEqual(PHOTOS_DELETE_CONFIRM_SCRIPT, mocked_popen.call_args.args[0][2])
        self.assertEqual(str(PHOTOS_CLEANUP_TIMEOUT_SECONDS), mocked_popen.call_args.args[0][3])

    def test_cleanup_does_not_launch_helper_when_watcher_cannot_start(self) -> None:
        imported_qr = ImportedPhotosQr(Path("/tmp/login-qr.png"), "asset-id", "login-qr.png", "a" * 64, 240, 240)
        helper = Path("/tools/photos-qr-cleanup.app/Contents/MacOS/photos-qr-cleanup")
        with (
            patch("app.photos_qr_cleanup._ensure_photos_cleanup_helper", return_value=helper),
            patch("app.photos_qr_cleanup._photos_asset_exists", return_value=True),
            patch("app.photos_qr_cleanup.subprocess.Popen", side_effect=OSError("launch failed")),
            patch("app.photos_qr_cleanup.subprocess.run") as mocked_run,
        ):
            with self.assertRaisesRegex(PhotosQrCleanupError, "Unable to start.*watcher"):
                delete_imported_qr_from_photos(imported_qr)
        mocked_run.assert_not_called()

    def test_missing_asset_does_not_start_confirmation_watcher(self) -> None:
        imported_qr = ImportedPhotosQr(Path("/tmp/login-qr.png"), "asset-id", "login-qr.png", "a" * 64, 240, 240)
        with (
            patch("app.photos_qr_cleanup._ensure_photos_cleanup_helper"),
            patch("app.photos_qr_cleanup._photos_asset_exists", return_value=False),
            patch("app.photos_qr_cleanup._start_delete_confirmation_watcher") as mocked_start,
        ):
            self.assertEqual("already-missing", delete_imported_qr_from_photos(imported_qr))
        mocked_start.assert_not_called()

    def test_stop_delete_confirmation_watcher_terminates_only_when_running(self) -> None:
        class FakeWatcher:
            def __init__(self) -> None:
                self.terminated = False
                self.communicated = False

            def poll(self) -> None:
                return None

            def terminate(self) -> None:
                self.terminated = True

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                self.communicated = True
                return ("", "")

        watcher = FakeWatcher()
        _stop_delete_confirmation_watcher(watcher)  # type: ignore[arg-type]

        self.assertTrue(watcher.terminated)
        self.assertTrue(watcher.communicated)

    def test_stop_finished_watcher_preserves_failure_diagnostics(self) -> None:
        watcher = Mock()
        watcher.poll.return_value = 0
        watcher.communicate.return_value = ("timed-out status=button-disabled\n", "watching\nbutton-disabled\n")

        diagnostics = _stop_delete_confirmation_watcher(watcher)

        watcher.terminate.assert_not_called()
        self.assertEqual("watching | button-disabled | timed-out status=button-disabled", diagnostics)

    def test_stop_stuck_watcher_kills_and_collects_diagnostics(self) -> None:
        watcher = Mock()
        watcher.poll.return_value = None
        watcher.communicate.side_effect = [
            subprocess.TimeoutExpired("osascript", 2),
            ("", "click-error attempt=1 method=click -25204\n"),
        ]

        diagnostics = _stop_delete_confirmation_watcher(watcher)

        watcher.terminate.assert_called_once()
        watcher.kill.assert_called_once()
        self.assertIn("click-error attempt=1", diagnostics)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("osascript"), "requires macOS AppleScript")
class PhotosConfirmationAppleScriptTests(unittest.TestCase):
    """Execute the actual polling logic with a simulated UI; never access Photos."""

    def run_simulated_scans(self, statuses: list[str], timeout: str = "10", *, diagnostics: bool = False) -> subprocess.CompletedProcess[str]:
        # Remove the ONLY handler that talks to System Events before executing anything.
        controller = PHOTOS_DELETE_CONFIRM_SCRIPT.split("\non scanDeleteDialog(", 1)[0]
        self.assertNotIn('tell application', controller)
        states = ", ".join(f'"{status}"' for status in statuses)
        fake_scan = f'''
property testStatuses : {{{states}}}
property testScanIndex : 0
on scanDeleteDialog(clickMethod)
    set testScanIndex to testScanIndex + 1
    log "test-scan=" & testScanIndex & " method=" & clickMethod
    if testScanIndex > (count of testStatuses) then return {{"not-found", ""}}
    return {{item testScanIndex of testStatuses, ""}}
end scanDeleteDialog
'''
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", controller + fake_scan, timeout] + (["diagnostics"] if diagnostics else []),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed

    def test_full_confirmation_script_compiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            completed = subprocess.run(
                ["/usr/bin/osacompile", "-o", str(Path(tmp_dir) / "confirmation.scpt"), "-"],
                input=PHOTOS_DELETE_CONFIRM_SCRIPT, capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_verbose_controller_records_scan_and_confirmation_outcome(self) -> None:
        completed = self.run_simulated_scans(
            ["not-found", "clicked", "not-found", "not-found", "not-found"], diagnostics=True,
        )
        self.assertEqual("dialog-dismissed attempts=1", completed.stdout.strip())
        self.assertIn("PHOTOS_DIAG elapsed_s=", completed.stderr)
        self.assertIn("scan=1", completed.stderr)
        self.assertIn("event=scan.end status=not-found", completed.stderr)
        self.assertIn("event=watcher.end reason=dialog-dismissed attempts=1", completed.stderr)

    def test_osascript_trace_is_persisted_while_simulated_scan_is_still_waiting(self) -> None:
        controller = PHOTOS_DELETE_CONFIRM_SCRIPT.split("\non scanDeleteDialog(", 1)[0]
        simulated = controller + '''
on scanDeleteDialog(clickMethod)
    set diagnosticContext to "pid=123 window_index=1"
    my traceBegin("window.entire_contents")
    delay 30
    return {"not-found", ""}
end scanDeleteDialog
'''
        self.assertNotIn('tell application', simulated)
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "scan.log"
            with patch("app.photos_qr_cleanup.PHOTOS_DELETE_CONFIRM_SCRIPT", simulated):
                watcher = _start_delete_confirmation_watcher(log_path=path)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if "operation=window.entire_contents event=begin" in path.read_text():
                        break
                    time.sleep(0.01)
                self.assertIsNone(watcher.poll())
                self.assertIn("pid=123 window_index=1 operation=window.entire_contents event=begin", path.read_text())
            finally:
                _stop_delete_confirmation_watcher(watcher)

    def test_trace_helpers_explain_mismatch_without_dumping_unrelated_text(self) -> None:
        properties = PHOTOS_DELETE_CONFIRM_SCRIPT.split("\non run argv", 1)[0]
        helpers = "on traceEvent(" + PHOTOS_DELETE_CONFIRM_SCRIPT.split("\non traceEvent(", 1)[1].split("\non scanDeleteDialog(", 1)[0]
        script = properties + "\n" + helpers + '''
on run argv
    set diagnosticEnabled to true
    set diagnosticStartedAt to current date
    set diagnosticScan to 3
    set diagnosticContext to "pid=123 window_index=2 element_index=7"
    my traceBegin("element.name")
    my traceValue("private unrelated album title")
    my traceValue("取消")
    my traceMatch("Tax Portal Photos QR Cleanup 想要删除1张照片", true)
    my traceMatch("Tax Portal Photos QR Cleanup 想要删除这张照片", true)
    return my traceText("line1" & linefeed & "line2")
end run
'''
        self.assertNotIn('tell application', script)
        completed = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("line1 line2", completed.stdout.strip())
        self.assertIn("pid=123 window_index=2 element_index=7", completed.stderr)
        self.assertNotIn("private unrelated album title", completed.stderr)
        self.assertIn("text=[取消]", completed.stderr)
        self.assertIn("delete_phrase=false matched=false", completed.stderr)
        self.assertIn("delete_phrase=true matched=true", completed.stderr)

    def test_late_disabled_dialog_and_ineffective_first_click_are_retried(self) -> None:
        completed = self.run_simulated_scans([
            "not-found", "not-found", "button-disabled", "clicked", "clicked",
            "not-found", "not-found", "not-found",
        ])
        self.assertEqual("dialog-dismissed attempts=2", completed.stdout.strip())
        self.assertIn("test-scan=4 method=click", completed.stderr)
        self.assertIn("test-scan=5 method=AXPress", completed.stderr)
        self.assertIn("test-scan=8", completed.stderr)

    def test_scan_error_and_temporary_absence_do_not_end_confirmation_early(self) -> None:
        completed = self.run_simulated_scans([
            "click-error", "not-found", "not-found", "scan-error", "not-found", "not-found",
            "clicked", "not-found", "not-found", "not-found",
        ])
        self.assertEqual("dialog-dismissed attempts=2", completed.stdout.strip())
        self.assertIn("test-scan=7 method=AXPress", completed.stderr)
        self.assertIn("test-scan=10", completed.stderr)

    def test_watcher_reports_missing_dialog_when_deadline_expires(self) -> None:
        completed = self.run_simulated_scans(["not-found"], timeout="1")
        self.assertEqual("timed-out status=not-found attempts=0", completed.stdout.strip())

    def test_dialog_matching_accepts_helper_names_but_rejects_other_prompts(self) -> None:
        matcher = PHOTOS_DELETE_CONFIRM_SCRIPT.split("\non isCleanupDeleteDialog(", 1)[1]
        matcher = "on isCleanupDeleteDialog(" + matcher.split("\non scanDeleteDialog(", 1)[0]
        script = matcher + '''
on run argv
    return my isCleanupDeleteDialog(item 1 of argv, item 2 of argv as boolean)
end run
'''
        cases = [
            (f"{PHOTOS_CLEANUP_BUNDLE_ID} 想要删除这张照片", "true", "true"),
            ("photos-qr-cleanup-4cc4730b56d5 wants to delete this photo", "true", "true"),
            (f"{PHOTOS_CLEANUP_DISPLAY_NAME} 想要删除这张照片", "true", "true"),
            ("Other App wants to delete this photo", "true", "false"),
            (f"{PHOTOS_CLEANUP_DISPLAY_NAME} wants full access to your Photos library", "true", "false"),
            (f"{PHOTOS_CLEANUP_DISPLAY_NAME} 想要删除这张照片", "false", "false"),
        ]
        for dialog_text, deny_button, expected in cases:
            with self.subTest(dialog=dialog_text, deny=deny_button):
                completed = subprocess.run(
                    ["/usr/bin/osascript", "-e", script, dialog_text, deny_button],
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(expected, completed.stdout.strip())
