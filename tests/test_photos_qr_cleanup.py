from __future__ import annotations

import hashlib
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.photos_qr_cleanup import (
    ImportedPhotosQr,
    PHOTOS_CLEANUP_BUNDLE_ID,
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
                    with patch("app.photos_qr_cleanup._stop_delete_confirmation_watcher") as mocked_stop:
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
                    with patch("app.photos_qr_cleanup._stop_delete_confirmation_watcher") as mocked_stop:
                        with patch(
                            "app.photos_qr_cleanup.subprocess.run",
                            side_effect=subprocess.TimeoutExpired(["/usr/bin/open"], 45),
                        ):
                            with self.assertRaisesRegex(PhotosQrCleanupError, "failed to run"):
                                delete_imported_qr_from_photos(imported_qr)

        mocked_stop.assert_called_once_with(watcher)

    def test_delete_confirmation_script_requires_exact_helper_and_destructive_dialog(self) -> None:
        self.assertIn(PHOTOS_CLEANUP_BUNDLE_ID, PHOTOS_DELETE_CONFIRM_SCRIPT)
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
        self.assertNotIn('perform action "AXPress"', PHOTOS_DELETE_CONFIRM_SCRIPT)

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
