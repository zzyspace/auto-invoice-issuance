from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path


PHOTOS_CLEANUP_TIMEOUT_SECONDS = 45.0
PHOTOS_CLEANUP_BUNDLE_ID = "com.fuzzy.tax-portal.photos-qr-cleanup"
PHOTOS_CLEANUP_DISPLAY_NAME = "Tax Portal Photos QR Cleanup"
PHOTOS_ASSET_EXISTS_SCRIPT = '''
on run argv
    set assetID to item 1 of argv
    tell application "/System/Applications/Photos.app"
        return exists media item id assetID
    end tell
end run
'''.strip()
PHOTOS_DELETE_CONFIRM_SCRIPT = f'''
on run argv
    set deadline to (current date) + (item 1 of argv as real)
    set clickAttempts to 0
    set absentPolls to 0
    set clickMethod to "click"
    set lastStatus to "watching"
    log lastStatus
    repeat while (current date) < deadline
        try
            with timeout of 2 seconds
                set scanResult to my scanDeleteDialog(clickMethod)
            end timeout
        on error errorMessage number errorNumber
            set scanResult to {{"scan-error", errorNumber as text}}
        end try
        set scanStatus to item 1 of scanResult
        set scanDetail to item 2 of scanResult
        if scanStatus is "clicked" or scanStatus is "click-error" then
            set clickAttempts to clickAttempts + 1
            set absentPolls to 0
            log scanStatus & " attempt=" & clickAttempts & " method=" & clickMethod & " " & scanDetail
            if clickMethod is "click" then
                set clickMethod to "AXPress"
            else
                set clickMethod to "click"
            end if
            -- A successful accessibility call does not prove that the dialog closed.
            delay 0.6
        else
            if scanStatus is "not-found" and clickAttempts > 0 then
                set absentPolls to absentPolls + 1
                if absentPolls >= 3 then return "dialog-dismissed attempts=" & clickAttempts
            else
                set absentPolls to 0
            end if
            if scanStatus is not lastStatus then log scanStatus & " " & scanDetail
            delay 0.2
        end if
        set lastStatus to scanStatus
    end repeat
    return "timed-out status=" & lastStatus & " attempts=" & clickAttempts
end run

on isCleanupDeleteDialog(dialogText, hasDenyButton)
    return hasDenyButton and (dialogText contains "{PHOTOS_CLEANUP_BUNDLE_ID}" or dialogText contains "photos-qr-cleanup-" or dialogText contains "{PHOTOS_CLEANUP_DISPLAY_NAME}") and (dialogText contains "删除这张照片" or dialogText contains "delete this photo" or dialogText contains "Delete This Photo" or dialogText contains "This photo will be deleted from both iCloud")
end isCleanupDeleteDialog

on scanDeleteDialog(clickMethod)
    set hadScanError to false
    tell application "System Events"
        set candidateProcesses to (application processes whose bundle identifier is "com.apple.UserNotificationCenter")
        set candidateProcesses to candidateProcesses & (application processes whose bundle identifier is "{PHOTOS_CLEANUP_BUNDLE_ID}")
        set candidateProcesses to candidateProcesses & (application processes whose name is "photos-qr-cleanup")
        set candidateProcesses to candidateProcesses & (application processes whose bundle identifier is "com.apple.Photos")
        repeat with currentProcess in candidateProcesses
            try
                repeat with currentWindow in windows of currentProcess
                    set dialogElements to entire contents of currentWindow
                    set dialogText to ""
                    set hasDenyButton to false
                    try
                        set dialogText to dialogText & " " & (name of currentWindow as text)
                    end try
                    try
                        set dialogText to dialogText & " " & (description of currentWindow as text)
                    end try
                    repeat with currentElement in dialogElements
                        try
                            set elementName to name of currentElement
                            if elementName is not missing value then
                                set dialogText to dialogText & " " & (elementName as text)
                                if role of currentElement is "AXButton" and ((elementName as text) is "不允许" or (elementName as text) is "Don't Allow" or (elementName as text) is "Don’t Allow") then
                                    set hasDenyButton to true
                                end if
                            end if
                        end try
                        try
                            set elementDescription to description of currentElement
                            if elementDescription is not missing value then
                                set dialogText to dialogText & " " & (elementDescription as text)
                                if role of currentElement is "AXButton" and ((elementDescription as text) is "不允许" or (elementDescription as text) is "Don't Allow" or (elementDescription as text) is "Don’t Allow") then
                                    set hasDenyButton to true
                                end if
                            end if
                        end try
                        try
                            set elementValue to value of currentElement
                            if elementValue is not missing value then set dialogText to dialogText & " " & (elementValue as text)
                        end try
                    end repeat
                    if my isCleanupDeleteDialog(dialogText, hasDenyButton) then
                        repeat with currentElement in dialogElements
                            try
                                set elementName to name of currentElement as text
                            on error
                                set elementName to ""
                            end try
                            try
                                set elementDescription to description of currentElement as text
                            on error
                                set elementDescription to ""
                            end try
                            try
                                if role of currentElement is "AXButton" and (elementName is "删除" or elementName is "Delete" or elementDescription is "删除" or elementDescription is "Delete") then
                                    if not (enabled of currentElement) then return {{"button-disabled", ""}}
                                    try
                                        set frontmost of currentProcess to true
                                    end try
                                    try
                                        if clickMethod is "AXPress" then
                                            perform action "AXPress" of currentElement
                                        else
                                            click currentElement
                                        end if
                                        return {{"clicked", ""}}
                                    on error errorMessage number errorNumber
                                        return {{"click-error", errorNumber as text}}
                                    end try
                                end if
                            on error
                                set hadScanError to true
                            end try
                        end repeat
                        return {{"button-not-found", ""}}
                    end if
                end repeat
            on error
                set hadScanError to true
            end try
        end repeat
    end tell
    if hadScanError then return {{"scan-error", "unable to read a candidate window"}}
    return {{"not-found", ""}}
end scanDeleteDialog
'''.strip()


class PhotosQrCleanupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportedPhotosQr:
    qr_path: Path
    asset_id: str
    original_filename: str
    sha256: str
    width: int
    height: int


def describe_imported_qr(qr_path: Path, asset_id: str) -> ImportedPhotosQr:
    normalized_asset_id = asset_id.strip()
    if not normalized_asset_id or "\n" in normalized_asset_id or "\r" in normalized_asset_id:
        raise PhotosQrCleanupError("Photos import did not return one valid media-item identifier.")
    try:
        payload = qr_path.read_bytes()
    except OSError as exc:
        raise PhotosQrCleanupError(f"Unable to read imported QR file: {exc}") from exc
    if len(payload) < 24 or payload[:8] != b"\x89PNG\r\n\x1a\n" or payload[12:16] != b"IHDR":
        raise PhotosQrCleanupError("Imported QR file is not a valid PNG with an IHDR header.")
    width, height = struct.unpack(">II", payload[16:24])
    return ImportedPhotosQr(
        qr_path=qr_path,
        asset_id=normalized_asset_id,
        original_filename=qr_path.name,
        sha256=hashlib.sha256(payload).hexdigest(),
        width=width,
        height=height,
    )


def delete_imported_qr_from_photos(imported_qr: ImportedPhotosQr) -> str:
    helper = _ensure_photos_cleanup_helper()
    if not _photos_asset_exists(imported_qr.asset_id):
        return "already-missing"
    app_bundle = helper.parents[2]
    if app_bundle.suffix != ".app":
        raise PhotosQrCleanupError(f"Photos QR cleanup helper is not inside an app bundle: {helper}")
    command = [
        "/usr/bin/open",
        "-W",
        "-g",
        str(app_bundle),
        "--args",
        imported_qr.asset_id,
        imported_qr.original_filename,
        imported_qr.sha256,
        str(imported_qr.width),
        str(imported_qr.height),
    ]
    confirmation_watcher = _start_delete_confirmation_watcher()
    helper_error: OSError | subprocess.TimeoutExpired | None = None
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=PHOTOS_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        helper_error = exc
    finally:
        confirmation_diagnostics = _stop_delete_confirmation_watcher(confirmation_watcher)
    diagnostic_suffix = f" Confirmation watcher: {confirmation_diagnostics}"
    if helper_error is not None:
        raise PhotosQrCleanupError(
            f"Photos QR cleanup helper failed to run: {helper_error}.{diagnostic_suffix}"
        ) from helper_error
    error_output = (completed.stderr or "").strip()
    if completed.returncode != 0:
        detail = error_output or f"exit status {completed.returncode}"
        raise PhotosQrCleanupError(detail + diagnostic_suffix)
    if _photos_asset_exists(imported_qr.asset_id):
        raise PhotosQrCleanupError(
            "Photos QR cleanup app exited but the verified asset is still present." + diagnostic_suffix
        )
    return "deleted"


def _photos_asset_exists(asset_id: str) -> bool:
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", PHOTOS_ASSET_EXISTS_SCRIPT, asset_id],
            check=False,
            capture_output=True,
            text=True,
            timeout=PHOTOS_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PhotosQrCleanupError(f"Unable to verify Photos QR asset: {exc}") from exc
    output = (completed.stdout or "").strip().lower()
    error_output = (completed.stderr or "").strip()
    if completed.returncode != 0:
        raise PhotosQrCleanupError(error_output or f"Photos asset verification exited {completed.returncode}.")
    if output == "true":
        return True
    if output == "false":
        return False
    raise PhotosQrCleanupError(f"Photos asset verification returned unexpected output: {output!r}")


def _start_delete_confirmation_watcher() -> subprocess.Popen[str]:
    try:
        return subprocess.Popen(
            [
                "/usr/bin/osascript", "-e", PHOTOS_DELETE_CONFIRM_SCRIPT,
                str(PHOTOS_CLEANUP_TIMEOUT_SECONDS),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise PhotosQrCleanupError(f"Unable to start Photos delete confirmation watcher: {exc}") from exc


def _stop_delete_confirmation_watcher(watcher: subprocess.Popen[str] | None) -> str:
    if watcher is None:
        return "not-started"
    try:
        if watcher.poll() is None:
            watcher.terminate()
        stdout, stderr = watcher.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        watcher.kill()
        stdout, stderr = watcher.communicate()
    # Keep the last attempts and any AppleScript error without flooding runner logs.
    output = " | ".join((stderr or "").splitlines() + (stdout or "").splitlines())
    return output[-2000:] or "stopped without diagnostic output"


def _ensure_photos_cleanup_helper() -> Path:
    source = Path(__file__).with_name("photos_qr_cleanup.m")
    if not source.is_file():
        raise PhotosQrCleanupError(f"Photos QR cleanup source is missing: {source}")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    tool_dir = Path(__file__).resolve().parents[1] / "data" / "tax-portal-tools"
    app_bundle = tool_dir / f"photos-qr-cleanup-{source_digest[:12]}.app"
    helper = app_bundle / "Contents" / "MacOS" / "photos-qr-cleanup"
    launch_services = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if helper.is_file():
        try:
            subprocess.run(
                [str(launch_services), "-f", str(app_bundle)],
                check=True,
                capture_output=True,
                text=True,
                timeout=PHOTOS_CLEANUP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            raise PhotosQrCleanupError(
                f"Unable to register Photos QR cleanup app: {str(stderr).strip() or exc}"
            ) from exc
        return helper

    tool_dir.mkdir(parents=True, exist_ok=True)
    temporary_bundle = tool_dir / f".photos-qr-cleanup-{os.getpid()}.app"
    temporary_contents = temporary_bundle / "Contents"
    temporary_macos = temporary_contents / "MacOS"
    temporary_macos.mkdir(parents=True, exist_ok=False)
    info_plist = temporary_contents / "Info.plist"
    temporary_helper = temporary_macos / "photos-qr-cleanup"
    with info_plist.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleExecutable": "photos-qr-cleanup",
                "CFBundleIdentifier": PHOTOS_CLEANUP_BUNDLE_ID,
                "CFBundleName": PHOTOS_CLEANUP_DISPLAY_NAME,
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "1.0",
                "CFBundleVersion": "1",
                "LSUIElement": True,
                "NSPhotoLibraryUsageDescription": (
                    "Delete only the tax portal QR image imported by the current login run."
                ),
            },
            handle,
        )
    compile_command = [
        "/usr/bin/clang",
        "-fobjc-arc",
        "-fblocks",
        str(source),
        "-o",
        str(temporary_helper),
        "-framework",
        "Foundation",
        "-framework",
        "Photos",
    ]
    try:
        subprocess.run(
            compile_command,
            check=True,
            capture_output=True,
            text=True,
            timeout=PHOTOS_CLEANUP_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [
                "/usr/bin/codesign",
                "--force",
                "--sign",
                "-",
                "--identifier",
                PHOTOS_CLEANUP_BUNDLE_ID,
                str(temporary_bundle),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=PHOTOS_CLEANUP_TIMEOUT_SECONDS,
        )
        temporary_bundle.replace(app_bundle)
        subprocess.run(
            [str(launch_services), "-f", str(app_bundle)],
            check=True,
            capture_output=True,
            text=True,
            timeout=PHOTOS_CLEANUP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise PhotosQrCleanupError(
            f"Unable to build Photos QR cleanup helper: {str(stderr).strip() or exc}"
        ) from exc
    finally:
        try:
            shutil.rmtree(temporary_bundle, ignore_errors=True)
        except OSError:
            pass
    return helper
