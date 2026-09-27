from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import struct
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


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
property diagnosticEnabled : false
property diagnosticStartedAt : missing value
property diagnosticScan : 0
property diagnosticContext : ""
property diagnosticOperation : ""

on run argv
    set diagnosticStartedAt to current date
    set deadline to diagnosticStartedAt + (item 1 of argv as real)
    set diagnosticEnabled to (count of argv) > 1
    set clickAttempts to 0
    set absentPolls to 0
    set clickMethod to "click"
    set lastStatus to "watching"
    log lastStatus
    repeat while (current date) < deadline
        set diagnosticScan to diagnosticScan + 1
        set diagnosticContext to ""
        my traceBegin("scan")
        try
            with timeout of 2 seconds
                set scanResult to my scanDeleteDialog(clickMethod)
            end timeout
        on error errorMessage number errorNumber
            my traceEvent("scan.error", "error_code=" & errorNumber)
            set scanResult to {{"scan-error", errorNumber as text}}
        end try
        set scanStatus to item 1 of scanResult
        set scanDetail to item 2 of scanResult
        my traceEvent("scan.end", "status=" & scanStatus & " detail=" & scanDetail & " method=" & clickMethod)
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
                if absentPolls >= 3 then
                    my traceEvent("watcher.end", "reason=dialog-dismissed attempts=" & clickAttempts)
                    return "dialog-dismissed attempts=" & clickAttempts
                end if
            else
                set absentPolls to 0
            end if
            if scanStatus is not lastStatus then log scanStatus & " " & scanDetail
            delay 0.2
        end if
        set lastStatus to scanStatus
    end repeat
    my traceEvent("watcher.end", "reason=timed-out status=" & lastStatus & " attempts=" & clickAttempts)
    return "timed-out status=" & lastStatus & " attempts=" & clickAttempts
end run

on traceEvent(eventName, detailText)
    if not diagnosticEnabled then return
    try
        log "PHOTOS_DIAG elapsed_s=" & ((current date) - diagnosticStartedAt) & " scan=" & diagnosticScan & " " & diagnosticContext & " operation=" & diagnosticOperation & " event=" & eventName & " " & detailText
    end try
end traceEvent

on traceBegin(operationName)
    set diagnosticOperation to operationName
    my traceEvent("begin", "")
end traceBegin

on traceText(textValue)
    set oldDelimiters to AppleScript's text item delimiters
    try
        set safeValue to textValue as text
        if (length of safeValue) > 180 then set safeValue to text 1 thru 180 of safeValue
        repeat with separator in {{return, linefeed, tab}}
            set AppleScript's text item delimiters to contents of separator
            set parts to text items of safeValue
            set AppleScript's text item delimiters to " "
            set safeValue to parts as text
        end repeat
        set AppleScript's text item delimiters to oldDelimiters
        return safeValue
    on error
        set AppleScript's text item delimiters to oldDelimiters
        return "<unavailable>"
    end try
end traceText

on traceValue(valueRead)
    if not diagnosticEnabled then return
    try
        if valueRead is missing value then
            my traceEvent("end", "value=missing")
            return
        end if
        set valueText to valueRead as text
        -- Do not dump names/descriptions/values of unrelated photos or albums.
        if valueText contains "删除" or valueText contains "delete" or valueText contains "不允许" or valueText contains "Don't Allow" or valueText contains "Don’t Allow" or valueText is "取消" or valueText is "Cancel" or valueText contains "photos-qr-cleanup" or valueText contains "Tax Portal Photos QR Cleanup" then
            my traceEvent("end", "text=[" & my traceText(valueText) & "]")
        else
            my traceEvent("end", "text_length=" & (length of valueText))
        end if
    end try
end traceValue

on traceField(fieldName, fieldValue)
    if not diagnosticEnabled then return
    try
        my traceEvent("end", fieldName & "=[" & my traceText(fieldValue) & "]")
    end try
end traceField

on traceMatch(dialogText, hasDenyButton)
    if not diagnosticEnabled then return
    try
        set hasHelper to dialogText contains "{PHOTOS_CLEANUP_BUNDLE_ID}" or dialogText contains "photos-qr-cleanup-" or dialogText contains "{PHOTOS_CLEANUP_DISPLAY_NAME}"
        set hasDeletePhrase to dialogText contains "删除这张照片" or dialogText contains "delete this photo" or dialogText contains "Delete This Photo" or dialogText contains "This photo will be deleted from both iCloud"
        my traceEvent("window.match", "deny=" & hasDenyButton & " helper=" & hasHelper & " delete_phrase=" & hasDeletePhrase & " matched=" & my isCleanupDeleteDialog(dialogText, hasDenyButton))
    end try
end traceMatch

on isCleanupDeleteDialog(dialogText, hasDenyButton)
    return hasDenyButton and (dialogText contains "{PHOTOS_CLEANUP_BUNDLE_ID}" or dialogText contains "photos-qr-cleanup-" or dialogText contains "{PHOTOS_CLEANUP_DISPLAY_NAME}") and (dialogText contains "删除这张照片" or dialogText contains "delete this photo" or dialogText contains "Delete This Photo" or dialogText contains "This photo will be deleted from both iCloud")
end isCleanupDeleteDialog

on scanDeleteDialog(clickMethod)
    set hadScanError to false
    tell application "System Events"
        my traceBegin("candidates.UserNotificationCenter")
        set candidateProcesses to (application processes whose bundle identifier is "com.apple.UserNotificationCenter")
        my traceEvent("end", "")
        my traceBegin("candidates.cleanup_bundle")
        set candidateProcesses to candidateProcesses & (application processes whose bundle identifier is "{PHOTOS_CLEANUP_BUNDLE_ID}")
        my traceEvent("end", "")
        my traceBegin("candidates.cleanup_name")
        set candidateProcesses to candidateProcesses & (application processes whose name is "photos-qr-cleanup")
        my traceEvent("end", "")
        my traceBegin("candidates.Photos")
        set candidateProcesses to candidateProcesses & (application processes whose bundle identifier is "com.apple.Photos")
        my traceEvent("end", "")
        set processIndex to 0
        repeat with currentProcess in candidateProcesses
            set processIndex to processIndex + 1
            set diagnosticContext to "process_index=" & processIndex
            -- Diagnostic identity reads cannot alter matching or retry decisions.
            if diagnosticEnabled then
                try
                    my traceBegin("process.name")
                    set processLabel to name of currentProcess as text
                    set diagnosticContext to diagnosticContext & " process=[" & my traceText(processLabel) & "]"
                    my traceBegin("process.pid")
                    set processPID to unix id of currentProcess
                    set diagnosticContext to diagnosticContext & " pid=" & processPID
                    my traceBegin("process.bundle")
                    set processBundle to bundle identifier of currentProcess as text
                    set diagnosticContext to diagnosticContext & " bundle=[" & my traceText(processBundle) & "]"
                    my traceEvent("process.identity", "")
                on error errorMessage number errorNumber
                    my traceEvent("diagnostic.error", "error_code=" & errorNumber)
                end try
            end if
            set processContext to diagnosticContext
            set windowIndex to 0
            try
                my traceBegin("process.windows")
                repeat with currentWindow in windows of currentProcess
                    set windowIndex to windowIndex + 1
                    set diagnosticContext to processContext & " window_index=" & windowIndex
                    set windowContext to diagnosticContext
                    my traceBegin("window.entire_contents")
                    set dialogElements to entire contents of currentWindow
                    if diagnosticEnabled then my traceEvent("end", "element_count=" & (count of dialogElements))
                    set dialogText to ""
                    set hasDenyButton to false
                    try
                        my traceBegin("window.name")
                        set windowName to name of currentWindow as text
                        set dialogText to dialogText & " " & windowName
                        my traceEvent("window.title", "text=[" & my traceText(windowName) & "]")
                    on error errorMessage number errorNumber
                        my traceEvent("attribute.error", "error_code=" & errorNumber)
                    end try
                    try
                        my traceBegin("window.description")
                        set windowDescription to description of currentWindow as text
                        set dialogText to dialogText & " " & windowDescription
                        my traceEvent("window.description", "text=[" & my traceText(windowDescription) & "]")
                    on error errorMessage number errorNumber
                        my traceEvent("attribute.error", "error_code=" & errorNumber)
                    end try
                    set elementIndex to 0
                    repeat with currentElement in dialogElements
                        set elementIndex to elementIndex + 1
                        set diagnosticContext to windowContext & " element_index=" & elementIndex
                        try
                            my traceBegin("element.name")
                            set elementName to name of currentElement
                            my traceValue(elementName)
                            if elementName is not missing value then
                                set dialogText to dialogText & " " & (elementName as text)
                                my traceBegin("element.role_from_name")
                                set elementRole to role of currentElement
                                my traceField("role", elementRole)
                                if elementRole is "AXButton" and ((elementName as text) is "不允许" or (elementName as text) is "Don't Allow" or (elementName as text) is "Don’t Allow") then
                                    set hasDenyButton to true
                                end if
                            end if
                        on error errorMessage number errorNumber
                            my traceEvent("attribute.error", "error_code=" & errorNumber)
                        end try
                        try
                            my traceBegin("element.description")
                            set elementDescription to description of currentElement
                            my traceValue(elementDescription)
                            if elementDescription is not missing value then
                                set dialogText to dialogText & " " & (elementDescription as text)
                                my traceBegin("element.role_from_description")
                                set elementRole to role of currentElement
                                my traceField("role", elementRole)
                                if elementRole is "AXButton" and ((elementDescription as text) is "不允许" or (elementDescription as text) is "Don't Allow" or (elementDescription as text) is "Don’t Allow") then
                                    set hasDenyButton to true
                                end if
                            end if
                        on error errorMessage number errorNumber
                            my traceEvent("attribute.error", "error_code=" & errorNumber)
                        end try
                        try
                            my traceBegin("element.value")
                            set elementValue to value of currentElement
                            my traceValue(elementValue)
                            if elementValue is not missing value then set dialogText to dialogText & " " & (elementValue as text)
                        on error errorMessage number errorNumber
                            my traceEvent("attribute.error", "error_code=" & errorNumber)
                        end try
                    end repeat
                    set diagnosticContext to windowContext
                    my traceMatch(dialogText, hasDenyButton)
                    if my isCleanupDeleteDialog(dialogText, hasDenyButton) then
                        set elementIndex to 0
                        repeat with currentElement in dialogElements
                            set elementIndex to elementIndex + 1
                            set diagnosticContext to windowContext & " element_index=" & elementIndex
                            try
                                my traceBegin("button.name")
                                set elementName to name of currentElement as text
                                my traceValue(elementName)
                            on error errorMessage number errorNumber
                                my traceEvent("attribute.error", "error_code=" & errorNumber)
                                set elementName to ""
                            end try
                            try
                                my traceBegin("button.description")
                                set elementDescription to description of currentElement as text
                                my traceValue(elementDescription)
                            on error errorMessage number errorNumber
                                my traceEvent("attribute.error", "error_code=" & errorNumber)
                                set elementDescription to ""
                            end try
                            try
                                my traceBegin("button.role")
                                set elementRole to role of currentElement
                                my traceField("role", elementRole)
                                if elementRole is "AXButton" and (elementName is "删除" or elementName is "Delete" or elementDescription is "删除" or elementDescription is "Delete") then
                                    my traceBegin("button.enabled")
                                    set buttonEnabled to enabled of currentElement
                                    my traceField("enabled", buttonEnabled)
                                    if not buttonEnabled then return {{"button-disabled", ""}}
                                    try
                                        my traceBegin("process.frontmost")
                                        set frontmost of currentProcess to true
                                        my traceEvent("end", "")
                                    on error errorMessage number errorNumber
                                        my traceEvent("frontmost.error", "error_code=" & errorNumber)
                                    end try
                                    try
                                        my traceBegin("button.click." & clickMethod)
                                        if clickMethod is "AXPress" then
                                            perform action "AXPress" of currentElement
                                        else
                                            click currentElement
                                        end if
                                        my traceEvent("click.returned", "method=" & clickMethod)
                                        return {{"clicked", ""}}
                                    on error errorMessage number errorNumber
                                        my traceEvent("click.error", "method=" & clickMethod & " error_code=" & errorNumber)
                                        return {{"click-error", errorNumber as text}}
                                    end try
                                end if
                            on error errorMessage number errorNumber
                                my traceEvent("button.error", "error_code=" & errorNumber)
                                set hadScanError to true
                            end try
                        end repeat
                        my traceEvent("button.not_found", "")
                        return {{"button-not-found", ""}}
                    end if
                    set diagnosticContext to windowContext
                    my traceEvent("window.end", "")
                end repeat
                set diagnosticContext to processContext
                my traceEvent("process.end", "windows_seen=" & windowIndex)
            on error errorMessage number errorNumber
                my traceEvent("process.error", "error_code=" & errorNumber)
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


def _cleanup_log(logger: Callable[[str], None] | None, message: str) -> None:
    try:
        if logger is not None:
            logger(message)
    except Exception:
        pass


def delete_imported_qr_from_photos(
    imported_qr: ImportedPhotosQr, *, logger: Callable[[str], None] | None = None,
) -> str:
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
    log_path = imported_qr.qr_path.with_suffix(".photos-confirm.log")
    _cleanup_log(logger, f"Photos confirmation diagnostics requested path={log_path}")
    confirmation_watcher = _start_delete_confirmation_watcher(log_path=log_path)
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
        _cleanup_log(logger, f"Photos confirmation watcher finished: {confirmation_diagnostics}")
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


def _start_delete_confirmation_watcher(*, log_path: Path | None = None) -> subprocess.Popen[str]:
    stream = None
    log_error = None
    if log_path is not None:
        try:
            stream = log_path.open("ab", buffering=0)
            stream.write((f"PHOTOS_PARENT timestamp={datetime.now(timezone.utc).isoformat()} event=watcher.start\n").encode())
        except OSError as exc:
            log_error = type(exc).__name__
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
                stream = None
    try:
        watcher = subprocess.Popen(
            [
                "/usr/bin/osascript", "-e", PHOTOS_DELETE_CONFIRM_SCRIPT,
                str(PHOTOS_CLEANUP_TIMEOUT_SECONDS),
            ] + (["diagnostics"] if stream is not None else []),
            # Direct file output is durable during a stalled scan and cannot fill a pipe.
            # If opening it failed, disable detailed tracing and retain the legacy small output.
            stdout=stream if stream is not None else subprocess.PIPE,
            stderr=subprocess.STDOUT if stream is not None else subprocess.PIPE,
            text=True,
        )
        if stream is not None:
            watcher._photos_confirmation_log_path = log_path
        if log_error is not None:
            watcher._photos_confirmation_log_error = log_error
        return watcher
    except OSError as exc:
        raise PhotosQrCleanupError(f"Unable to start Photos delete confirmation watcher: {exc}") from exc
    finally:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _stop_delete_confirmation_watcher(watcher: subprocess.Popen[str] | None) -> str:
    if watcher is None:
        return "not-started"
    stopped_running = False
    killed = False
    try:
        if watcher.poll() is None:
            stopped_running = True
            watcher.terminate()
        stdout, stderr = watcher.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        killed = True
        watcher.kill()
        stdout, stderr = watcher.communicate()
    # Keep the last attempts and any AppleScript error without flooding runner logs.
    output = " | ".join((stderr or "").splitlines() + (stdout or "").splitlines())
    log_path = getattr(watcher, "_photos_confirmation_log_path", None)
    if isinstance(log_path, Path):
        try:
            with log_path.open("ab", buffering=0) as stream:
                stream.write((f"PHOTOS_PARENT timestamp={datetime.now(timezone.utc).isoformat()} "
                              f"event=watcher.stop pid={watcher.pid} returncode={watcher.returncode} "
                              f"stop_requested={stopped_running} killed={killed}\n").encode())
            with log_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 4000))
                tail = stream.read().decode("utf-8", errors="replace")
            output = " | ".join(tail.splitlines())[-2000:]
            return f"path={log_path} returncode={watcher.returncode} tail={output}"
        except Exception as exc:
            return f"path={log_path} diagnostics_read_error={type(exc).__name__}; {output[-2000:]}"
    log_error = getattr(watcher, "_photos_confirmation_log_error", None)
    if isinstance(log_error, str):
        output = f"diagnostics_file_unavailable={log_error}; {output}"
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
