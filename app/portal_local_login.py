from __future__ import annotations

import ctypes
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic, sleep
from typing import Callable, Iterable

from app.models import AppConfig
from app.utils import ensure_parent_dir

DEFAULT_ETAX_APP_PATH = Path("/Applications/电子税务局.app")
ETAX_APP_BUNDLE_ID_FALLBACK = "cn.gov.chinatax.gt4.app"
MESSAGES_BUNDLE_ID = "com.apple.MobileSMS"
PHOTOS_BUNDLE_ID = "com.apple.Photos"
VISIBLE_ELEMENT_POLL_SECONDS = 0.2
UI_ACTION_TIMEOUT_SECONDS = 20.0
OTP_AUTOFILL_TIMEOUT_SECONDS = 20.0
OTP_MESSAGES_FALLBACK_TIMEOUT_SECONDS = 60.0
PHOTOS_IMPORT_SETTLE_SECONDS = 1.0
QR_MIN_DIMENSION_PX = 120
OTP_REGEX = re.compile(r"【厦门税务】您的验证码是[:：]\s*(\d{6})")
OTP_DIGITS_REGEX = re.compile(r"(?<!\d)(\d{6})(?!\d)")
SMS_RESEND_COUNTDOWN_REGEX = re.compile(r"\d+秒重新获取")
SMS_REQUEST_RETRY_ATTEMPTS = 3
SMS_REQUEST_SETTLE_TIMEOUT_SECONDS = 3.0
POST_LOGIN_STATE_TIMEOUT_SECONDS = 15.0
ROLE_DIALOG_CONFIRM_ATTEMPTS = 3
ROLE_DIALOG_BEFORE_SELECT_SECONDS = 1.0
ROLE_DIALOG_SELECTION_SETTLE_SECONDS = 0.5
SCAN_ICON_X_RATIO = 0.91
SCAN_ICON_Y_RATIO = 0.11
SCAN_PAGE_READY_TIMEOUT_SECONDS = 8.0
SCAN_ALBUM_OPEN_ATTEMPTS = 4
SCAN_ALBUM_OPEN_SETTLE_SECONDS = 1.0
PHOTO_PICKER_SELECT_TIMEOUT_SECONDS = 20.0
PHOTOS_FIRST_ITEM_CLICK_X_RATIO = 0.30
PHOTOS_FIRST_ITEM_CLICK_Y_RATIO = 0.19
IN_APP_PHOTO_PICKER_CLICK_TARGETS = (
    (0.16, 0.29),
    (0.19, 0.29),
    (0.16, 0.33),
)
LOGIN_CONFIRMATION_TIMEOUT_SECONDS = 8.0
QR_LOCATOR_CANDIDATES = (
    ".qrcode canvas",
    ".qrcode img",
    "[class*='qr'] canvas",
    "[class*='qr'] img",
    "canvas",
    "img[alt*='二维码']",
    "img[src*='qr']",
    "img[src*='code']",
)
ETAX_PROCESS_PATTERN = r"cn\.gov\.chinatax\.gt4\.app|GT4\.app|电子税务局"
MESSAGES_PROCESS_PATTERN = r"Messages|信息"
PHOTOS_PROCESS_PATTERN = r"Photos|照片"
ETAX_CLICK_PRE_DELAY_SECONDS = 1.0
MOUSE_DOWN = 1
MOUSE_UP = 2
MOUSE_MOVED = 5
LEFT_MOUSE_BUTTON = 0
KEY_RETURN = 36
KEY_DOWN_ARROW = 125
KEY_ESCAPE = 53
KEY_TAB = 48
KEY_A = 0
KEY_DELETE = 51
AX_VALUE_CGPOINT = 1
AX_VALUE_CGSIZE = 2
CG_EVENT_FLAG_MASK_COMMAND = 0x00100000


@dataclass
class AXNode:
    element: int
    role: str
    subrole: str
    texts: tuple[str, ...]
    position: tuple[float, float] | None
    size: tuple[float, float] | None

    @property
    def center(self) -> tuple[float, float] | None:
        if self.position is None or self.size is None:
            return None
        return (self.position[0] + self.size[0] / 2.0, self.position[1] + self.size[1] / 2.0)


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class MacAccessibilityClient:
    STRING_ENCODING_UTF8 = 0x08000100

    def __init__(self) -> None:
        self.app = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        self.core = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.quartz = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        self.app.AXIsProcessTrusted.restype = ctypes.c_bool
        self.app.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        self.app.AXUIElementCreateApplication.restype = ctypes.c_void_p
        self.app.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        self.app.AXUIElementCopyAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self.app.AXUIElementCopyAttributeValue.restype = ctypes.c_uint32
        self.app.AXUIElementSetAttributeValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.app.AXUIElementSetAttributeValue.restype = ctypes.c_uint32
        self.app.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.app.AXUIElementPerformAction.restype = ctypes.c_uint32
        self.app.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.app.AXValueGetValue.restype = ctypes.c_bool
        self.core.CFGetTypeID.argtypes = [ctypes.c_void_p]
        self.core.CFGetTypeID.restype = ctypes.c_ulong
        self.core.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self.core.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.core.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.core.CFStringGetCString.restype = ctypes.c_bool
        self.core.CFStringGetTypeID.restype = ctypes.c_ulong
        self.core.CFArrayGetTypeID.restype = ctypes.c_ulong
        self.core.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self.core.CFArrayGetCount.restype = ctypes.c_long
        self.core.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self.core.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self.core.CFRetain.argtypes = [ctypes.c_void_p]
        self.core.CFRetain.restype = ctypes.c_void_p
        self.core.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.core.CFDictionaryGetValue.restype = ctypes.c_void_p
        self.core.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        self.core.CFNumberGetValue.restype = ctypes.c_bool
        self.core.CFRelease.argtypes = [ctypes.c_void_p]
        self.app.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
        self.app.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        self.app.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        self.app.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        self.app.CGEventKeyboardSetUnicodeString.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
        self.app.CGEventKeyboardSetUnicodeString.restype = None
        self.app.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong]
        self.app.CGEventSetFlags.restype = None
        self.app.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        self.app.CGEventPost.restype = None
        self.quartz.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        self.quartz.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
        self._cf_string_type = self.core.CFStringGetTypeID()
        self._cf_array_type = self.core.CFArrayGetTypeID()

    def is_process_trusted(self) -> bool:
        return bool(self.app.AXIsProcessTrusted())

    def app_element(self, pid: int) -> int:
        return int(self.app.AXUIElementCreateApplication(pid) or 0)

    def systemwide_element(self) -> int:
        return int(self.app.AXUIElementCreateSystemWide() or 0)

    def window_bounds(self, pid: int) -> tuple[float, float, float, float] | None:
        window_list = self.quartz.CGWindowListCopyWindowInfo(1 | 16, 0)
        if not window_list:
            return None
        try:
            if self.core.CFGetTypeID(window_list) != self._cf_array_type:
                return None
            count = self.core.CFArrayGetCount(window_list)
            for index in range(count):
                item = int(self.core.CFArrayGetValueAtIndex(window_list, index) or 0)
                if not item:
                    continue
                owner_pid = self._dictionary_number(item, "kCGWindowOwnerPID")
                if owner_pid != pid:
                    continue
                layer = self._dictionary_number(item, "kCGWindowLayer")
                if layer not in (None, 0):
                    continue
                bounds = self._dictionary_bounds(item, "kCGWindowBounds")
                if bounds is not None:
                    return bounds
        finally:
            self.core.CFRelease(window_list)
        return None

    def find_nodes(self, pid: int) -> list[AXNode]:
        root = self.app_element(pid)
        if not root:
            return []
        window_elements = self._children_from_attribute(root, "AXWindows")
        if not window_elements:
            focused = self._attribute_value(root, "AXFocusedWindow")
            if focused:
                window_elements = [focused]
        nodes: list[AXNode] = []
        seen: set[int] = set()
        for window in window_elements:
            self._collect_nodes(window, nodes, seen)
        return nodes

    def find_focused_nodes(self) -> list[AXNode]:
        systemwide = self.systemwide_element()
        if not systemwide:
            return []
        focused_app = self._attribute_value(systemwide, "AXFocusedApplication")
        if not focused_app:
            return []
        try:
            window_elements = self._children_from_attribute(focused_app, "AXWindows")
            if not window_elements:
                focused_window = self._attribute_value(focused_app, "AXFocusedWindow")
                if focused_window:
                    window_elements = [focused_window]
            nodes: list[AXNode] = []
            seen: set[int] = set()
            for window in window_elements:
                self._collect_nodes(window, nodes, seen)
            return nodes
        finally:
            self.core.CFRelease(focused_app)

    def click_node(self, node: AXNode) -> bool:
        if self._perform_action(node.element, "AXPress") == 0:
            return True
        return self.click_at_node_center(node)

    def click_at_node_center(self, node: AXNode) -> bool:
        center = node.center
        if center is None:
            return False
        self.click_at(*center)
        return True

    def click_at(self, x: float, y: float) -> None:
        point = CGPoint(x, y)
        down = self.app.CGEventCreateMouseEvent(None, MOUSE_DOWN, point, LEFT_MOUSE_BUTTON)
        up = self.app.CGEventCreateMouseEvent(None, MOUSE_UP, point, LEFT_MOUSE_BUTTON)
        try:
            self.app.CGEventPost(0, down)
            self.app.CGEventPost(0, up)
        finally:
            if down:
                self.core.CFRelease(down)
            if up:
                self.core.CFRelease(up)

    def send_key(self, key_code: int) -> None:
        down = self.app.CGEventCreateKeyboardEvent(None, key_code, True)
        up = self.app.CGEventCreateKeyboardEvent(None, key_code, False)
        try:
            self.app.CGEventPost(0, down)
            self.app.CGEventPost(0, up)
        finally:
            if down:
                self.core.CFRelease(down)
            if up:
                self.core.CFRelease(up)

    def send_modified_key(self, key_code: int, flags: int) -> None:
        down = self.app.CGEventCreateKeyboardEvent(None, key_code, True)
        up = self.app.CGEventCreateKeyboardEvent(None, key_code, False)
        try:
            self.app.CGEventSetFlags(down, flags)
            self.app.CGEventSetFlags(up, flags)
            self.app.CGEventPost(0, down)
            self.app.CGEventPost(0, up)
        finally:
            if down:
                self.core.CFRelease(down)
            if up:
                self.core.CFRelease(up)

    def type_text(self, text: str) -> None:
        code_units = (ctypes.c_uint16 * len(text))(*[ord(char) for char in text])
        down = self.app.CGEventCreateKeyboardEvent(None, 0, True)
        up = self.app.CGEventCreateKeyboardEvent(None, 0, False)
        try:
            self.app.CGEventKeyboardSetUnicodeString(down, len(text), code_units)
            self.app.CGEventKeyboardSetUnicodeString(up, len(text), code_units)
            self.app.CGEventPost(0, down)
            self.app.CGEventPost(0, up)
        finally:
            if down:
                self.core.CFRelease(down)
            if up:
                self.core.CFRelease(up)

    def set_text_value(self, node: AXNode, value: str) -> bool:
        cf_value = self._cf_string(value)
        cf_attr = self._cf_string("AXValue")
        try:
            return self.app.AXUIElementSetAttributeValue(node.element, cf_attr, cf_value) == 0
        finally:
            if cf_attr:
                self.core.CFRelease(cf_attr)
            if cf_value:
                self.core.CFRelease(cf_value)

    def _collect_nodes(self, element: int, output: list[AXNode], seen: set[int]) -> None:
        if not element or element in seen:
            return
        seen.add(element)
        output.append(
            AXNode(
                element=element,
                role=self._attribute_text(element, "AXRole"),
                subrole=self._attribute_text(element, "AXSubrole"),
                texts=self._texts_for_element(element),
                position=self._point_attribute(element, "AXPosition"),
                size=self._size_attribute(element, "AXSize"),
            )
        )
        for child in self._children_from_attribute(element, "AXChildren"):
            self._collect_nodes(child, output, seen)

    def _texts_for_element(self, element: int) -> tuple[str, ...]:
        values: list[str] = []
        for attr in ("AXTitle", "AXDescription", "AXValue", "AXIdentifier", "AXHelp"):
            text = self._attribute_text(element, attr)
            if text and text not in values:
                values.append(text)
        return tuple(values)

    def _children_from_attribute(self, element: int, attr: str) -> list[int]:
        value = self._attribute_value(element, attr)
        if not value or self.core.CFGetTypeID(value) != self._cf_array_type:
            return []
        count = self.core.CFArrayGetCount(value)
        items: list[int] = []
        for index in range(count):
            child = int(self.core.CFArrayGetValueAtIndex(value, index) or 0)
            if child:
                retained = int(self.core.CFRetain(child) or 0)
                items.append(retained or child)
        if value:
            self.core.CFRelease(value)
        return [item for item in items if item]

    def _attribute_text(self, element: int, attr: str) -> str:
        value = self._attribute_value(element, attr)
        if not value:
            return ""
        try:
            return self._cf_to_text(value)
        finally:
            self.core.CFRelease(value)

    def _point_attribute(self, element: int, attr: str) -> tuple[float, float] | None:
        value = self._attribute_value(element, attr)
        if not value:
            return None
        try:
            point = CGPoint()
            ok = self.app.AXValueGetValue(value, AX_VALUE_CGPOINT, ctypes.byref(point))
            if not ok:
                return None
            return (point.x, point.y)
        finally:
            self.core.CFRelease(value)

    def _size_attribute(self, element: int, attr: str) -> tuple[float, float] | None:
        value = self._attribute_value(element, attr)
        if not value:
            return None
        try:
            size = CGSize()
            ok = self.app.AXValueGetValue(value, AX_VALUE_CGSIZE, ctypes.byref(size))
            if not ok:
                return None
            return (size.width, size.height)
        finally:
            self.core.CFRelease(value)

    def _perform_action(self, element: int, action: str) -> int:
        cf_action = self._cf_string(action)
        try:
            return int(self.app.AXUIElementPerformAction(element, cf_action))
        finally:
            if cf_action:
                self.core.CFRelease(cf_action)

    def _attribute_value(self, element: int, attr: str) -> int | None:
        cf_attr = self._cf_string(attr)
        value = ctypes.c_void_p()
        result = self.app.AXUIElementCopyAttributeValue(element, cf_attr, ctypes.byref(value))
        if cf_attr:
            self.core.CFRelease(cf_attr)
        if result != 0 or not value.value:
            return None
        return int(value.value)

    def _dictionary_number(self, dictionary: int, key_name: str) -> float | None:
        key = self._cf_string(key_name)
        try:
            value = int(self.core.CFDictionaryGetValue(dictionary, key) or 0)
        finally:
            if key:
                self.core.CFRelease(key)
        if not value:
            return None
        double_value = ctypes.c_double()
        if self.core.CFNumberGetValue(value, 13, ctypes.byref(double_value)):
            return double_value.value
        int_value = ctypes.c_int()
        if self.core.CFNumberGetValue(value, 9, ctypes.byref(int_value)):
            return float(int_value.value)
        return None

    def _dictionary_bounds(self, dictionary: int, key_name: str) -> tuple[float, float, float, float] | None:
        key = self._cf_string(key_name)
        try:
            bounds_dict = int(self.core.CFDictionaryGetValue(dictionary, key) or 0)
        finally:
            if key:
                self.core.CFRelease(key)
        if not bounds_dict:
            return None
        x = self._dictionary_number(bounds_dict, "X")
        y = self._dictionary_number(bounds_dict, "Y")
        width = self._dictionary_number(bounds_dict, "Width")
        height = self._dictionary_number(bounds_dict, "Height")
        if None in (x, y, width, height):
            return None
        return (float(x), float(y), float(width), float(height))

    def _cf_to_text(self, value: int) -> str:
        type_id = self.core.CFGetTypeID(value)
        if type_id != self._cf_string_type:
            return ""
        buffer = ctypes.create_string_buffer(4096)
        ok = self.core.CFStringGetCString(value, buffer, len(buffer), self.STRING_ENCODING_UTF8)
        if not ok:
            return ""
        return buffer.value.decode("utf-8", errors="ignore")

    def _cf_string(self, value: str) -> int:
        return int(self.core.CFStringCreateWithCString(None, value.encode("utf-8"), self.STRING_ENCODING_UTF8) or 0)


class PortalLocalLoginError(RuntimeError):
    pass


class PortalMacLoginAutomator:
    def __init__(
        self,
        config: AppConfig,
        store_key: str,
        role_label: str,
        logger: Callable[[str, str], None],
    ) -> None:
        self.config = config
        self.store_key = store_key
        self.role_label = role_label
        self._logger = logger
        self._etax_app_path = config.portal_etax_app_path or DEFAULT_ETAX_APP_PATH
        self._ax = MacAccessibilityClient()
        self._etax_last_window_size: tuple[float, float] = (288.0, 552.0)

    @staticmethod
    def is_enabled(config: AppConfig) -> bool:
        return (
            sys.platform == "darwin"
            and config.portal_browser_backend == "chrome_cdp"
            and bool((config.portal_etax_app_username or "").strip())
            and bool((config.portal_etax_app_password or "").strip())
        )

    def automate(self, page: object, artifacts_dir: Path | None) -> Path:
        self._require_supported_environment()
        self._verify_gui_automation_prerequisites()
        qr_path = self._capture_qr_code(page, artifacts_dir)
        self._import_qr_into_photos(qr_path)
        bundle_id = self._resolve_app_bundle_identifier()
        self._launch_etax_app()
        self._wait_for_process(bundle_id, timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
        self._ensure_etax_session(bundle_id)
        self._open_scan_flow(bundle_id)
        self._select_latest_qr_from_album(bundle_id)
        self._confirm_scan_login(bundle_id)
        return qr_path

    def _require_supported_environment(self) -> None:
        if sys.platform != "darwin":
            raise PortalLocalLoginError("Portal local scan login automation is supported only on macOS.")
        if not (self.config.portal_etax_app_username or "").strip():
            raise PortalLocalLoginError("Missing TAX_PORTAL_ETAX_APP_USERNAME.")
        if not (self.config.portal_etax_app_password or "").strip():
            raise PortalLocalLoginError("Missing TAX_PORTAL_ETAX_APP_PASSWORD.")
        if not self._etax_app_path.exists():
            raise PortalLocalLoginError(
                f"电子税务局 app not found at {self._etax_app_path}. Set TAX_PORTAL_ETAX_APP_PATH explicitly."
            )

    def _verify_gui_automation_prerequisites(self) -> None:
        self._log("checking macOS GUI automation permissions")
        if not self._ax.is_process_trusted():
            raise PortalLocalLoginError(
                "macOS Accessibility is not enabled for the Python process that runs tax-portal. "
                "Enable Accessibility for the actual Python executable used by ./tax-portal, then retry."
            )
        try:
            self._run_applescript(
                'tell application "Photos" to count media items',
                timeout_seconds=5.0,
            )
        except PortalLocalLoginError as exc:
            raise PortalLocalLoginError(
                "Photos automation is not available for osascript. "
                "Grant osascript Automation access to Photos, then retry. "
                f"Original error: {exc}"
            ) from exc

    def _capture_qr_code(self, page: object, artifacts_dir: Path | None) -> Path:
        self._log("capturing tax portal login QR")
        target_dir = artifacts_dir or Path(mkdtemp(prefix="portal-local-login-"))
        ensure_parent_dir(target_dir / "placeholder.txt")
        target = target_dir / "login-qr.png"
        for selector in QR_LOCATOR_CANDIDATES:
            locator = getattr(page, "locator")(selector)
            try:
                count = locator.count()
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible():
                        continue
                    box = candidate.bounding_box()
                    if box is None:
                        continue
                    if box.get("width", 0) < QR_MIN_DIMENSION_PX or box.get("height", 0) < QR_MIN_DIMENSION_PX:
                        continue
                    candidate.screenshot(path=str(target))
                    self._log(f"captured tax portal login QR path={target}")
                    return target
                except Exception:
                    continue
        raise PortalLocalLoginError("Failed to locate a visible QR element on the tax portal login page.")

    def _import_qr_into_photos(self, qr_path: Path) -> None:
        self._log(f"importing tax portal login QR into Photos path={qr_path}")
        script = f"""
        tell application "Photos"
            import POSIX file {self._apple_string(str(qr_path))} skip check duplicates yes
        end tell
        """
        self._run_applescript(script, timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
        sleep(PHOTOS_IMPORT_SETTLE_SECONDS)
        self._log("imported tax portal login QR into Photos")

    def _resolve_app_bundle_identifier(self) -> str:
        info_plist = self._etax_app_path / "Contents" / "Info.plist"
        try:
            payload = plistlib.loads(info_plist.read_bytes())
        except Exception:
            return ETAX_APP_BUNDLE_ID_FALLBACK
        bundle_id = str(payload.get("CFBundleIdentifier") or "").strip()
        return bundle_id or ETAX_APP_BUNDLE_ID_FALLBACK

    def _launch_etax_app(self) -> None:
        self._log(f"launching 电子税务局 app path={self._etax_app_path}")
        self._run_command(["open", "-a", str(self._etax_app_path)], timeout_seconds=10.0)

    def _wait_for_process(self, bundle_id: str, *, timeout_seconds: float) -> None:
        self._log("waiting for 电子税务局 app process")
        deadline = monotonic() + timeout_seconds
        saw_process = False
        warned_window_pending = False
        while monotonic() < deadline:
            pids = self._find_process_pids(bundle_id)
            if pids:
                saw_process = True
                for pid in pids:
                    nodes = self._ax.find_nodes(pid)
                    if not nodes:
                        continue
                    self._remember_window_size(nodes)
                    if self._nodes_look_ready(nodes):
                        self._log(
                            f"detected candidate process pids bundle_id={bundle_id} pids={pids} ready_pid={pid}"
                        )
                        return
                if not warned_window_pending:
                    self._log(
                        "电子税务局 process detected but UI window is not ready yet; "
                        "waiting before starting click automation"
                    )
                    warned_window_pending = True
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        if saw_process:
            raise PortalLocalLoginError(f"Timed out waiting for accessible UI in process {bundle_id}.")
        raise PortalLocalLoginError(f"Timed out waiting for process {bundle_id}.")

    def _ensure_etax_session(self, bundle_id: str) -> None:
        self._log("navigating 电子税务局 app login flow")
        self._activate_application(bundle_id)
        self._click_etax_tabbar_item(bundle_id, 4)
        if self._maybe_click_named_element(bundle_id, ("立即登录",), timeout_seconds=4.0):
            self._set_login_account_value(bundle_id, self.config.portal_etax_app_username or "")
            self._set_login_password_value(bundle_id, self.config.portal_etax_app_password or "")
            self._click_named_element(bundle_id, ("登录",), timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
            self._request_sms_code(bundle_id)
            self._focus_sms_code_input(bundle_id)
            if self._try_fill_otp_from_system_prompt(bundle_id):
                self._log("filled SMS verification code from macOS one-time-code prompt")
            else:
                self._log("macOS one-time-code prompt not available; falling back to Messages app")
                code = self._read_latest_sms_code_from_messages()
                self._set_sms_code_value(bundle_id, code)
                self._log("filled SMS verification code from Messages app fallback")
            self._click_named_element(bundle_id, ("登录",), timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
            state = self._wait_for_post_login_state(bundle_id, timeout_seconds=POST_LOGIN_STATE_TIMEOUT_SECONDS)
            if state != "role_dialog":
                raise PortalLocalLoginError(
                    f"电子税务局 app did not reach role selection after SMS login state={state}"
                )
            state = self._confirm_role_selection(bundle_id)
            if state != "fingerprint_prompt":
                raise PortalLocalLoginError(
                    "电子税务局 app did not reach fingerprint quick-login prompt after role selection "
                    f"state={state}"
                )
            self._dismiss_fingerprint_prompt(bundle_id)
            self._log("dismissed fingerprint quick login prompt")
            state = self._wait_for_post_login_state(bundle_id, timeout_seconds=POST_LOGIN_STATE_TIMEOUT_SECONDS)
            if state == "login_page":
                self._log("fingerprint prompt dismissed but home markers not yet visible; continuing to scan flow")
            elif state != "home":
                raise PortalLocalLoginError(
                    f"电子税务局 app did not reach logged-in home after fingerprint prompt state={state}"
                )
        elif self._maybe_click_named_element(bundle_id, (self.role_label,), timeout_seconds=8.0):
            state = self._confirm_role_selection(bundle_id, role_already_selected=True)
            if state != "fingerprint_prompt":
                raise PortalLocalLoginError(
                    "电子税务局 app did not reach fingerprint quick-login prompt after role selection "
                    f"state={state}"
                )
            self._dismiss_fingerprint_prompt(bundle_id)
            self._log("dismissed fingerprint quick login prompt")
            state = self._wait_for_post_login_state(bundle_id, timeout_seconds=POST_LOGIN_STATE_TIMEOUT_SECONDS)
            if state == "login_page":
                self._log("fingerprint prompt dismissed but home markers not yet visible; continuing to scan flow")
            elif state != "home":
                raise PortalLocalLoginError(
                    f"电子税务局 app did not reach logged-in home after fingerprint prompt state={state}"
                )

    def _open_scan_flow(self, bundle_id: str) -> None:
        self._log("opening scan flow in 电子税务局 app")
        self._activate_application(bundle_id)
        self._click_etax_tabbar_item(bundle_id, 0)
        if not self._maybe_click_named_element(bundle_id, ("扫一扫", "扫码"), timeout_seconds=3.0):
            self._click_etax_scan_icon(bundle_id)
        self._wait_for_scan_page_ready(bundle_id)
        self._open_album_from_scan_page(bundle_id)

    def _select_latest_qr_from_album(self, bundle_id: str) -> None:
        self._log("selecting latest imported QR image from album")
        try:
            self._select_latest_qr_in_internal_picker(bundle_id)
            return
        except PortalLocalLoginError:
            pass
        if self._is_photos_picker_visible():
            self._select_latest_qr_in_photos_picker()
            return
        deadline = monotonic() + PHOTO_PICKER_SELECT_TIMEOUT_SECONDS
        while monotonic() < deadline:
            nodes = self._nodes_for_bundle(bundle_id)
            image_nodes = [node for node in nodes if node.role in {"AXImage", "AXButton"} and node.center is not None]
            image_nodes.sort(key=lambda node: (node.position[1] if node.position else 10**9, node.position[0] if node.position else 10**9))
            for node in image_nodes:
                if self._click_node_for_bundle(bundle_id, node):
                    return
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        self._click_etax_latest_photo(bundle_id)

    def _confirm_scan_login(self, bundle_id: str) -> None:
        self._log("confirming scan login in 电子税务局 app")
        self._activate_application(bundle_id)
        self._wait_for_login_confirmation_ready(bundle_id)
        self._click_named_element(bundle_id, ("登录",), timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)

    def _wait_for_scan_page_ready(self, bundle_id: str) -> None:
        deadline = monotonic() + SCAN_PAGE_READY_TIMEOUT_SECONDS
        while monotonic() < deadline:
            if self._is_scan_page_visible(bundle_id):
                return
            sleep(0.3)
        raise PortalLocalLoginError("Timed out waiting for 电子税务局 scan page.")

    def _open_album_from_scan_page(self, bundle_id: str) -> None:
        self._log("opening album from scan page")
        for attempt in range(1, SCAN_ALBUM_OPEN_ATTEMPTS + 1):
            self._activate_application(bundle_id)
            self._click_scan_album_region(bundle_id, attempt)
            sleep(SCAN_ALBUM_OPEN_SETTLE_SECONDS)
            if self._is_internal_photo_picker_visible(bundle_id) or not self._is_scan_page_visible(bundle_id):
                self._log(f"scan-page album entry opened attempt={attempt}")
                return
            self._log(f"scan-page album entry did not open attempt={attempt}")
        raise PortalLocalLoginError("Timed out opening album from 电子税务局 scan page.")

    def _is_scan_page_visible(self, bundle_id: str) -> bool:
        try:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
        except PortalLocalLoginError:
            return False
        return "识别二维码" in texts and "相册" in texts and "扫一扫" in texts

    def _is_internal_photo_picker_visible(self, bundle_id: str) -> bool:
        try:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
        except PortalLocalLoginError:
            return False
        required = ("取消", "照片", "精选集", "搜索你的图库")
        return all(any(required_item in text for text in texts) for required_item in required)

    def _is_photos_picker_visible(self) -> bool:
        try:
            texts = self._collect_visible_texts(PHOTOS_BUNDLE_ID, timeout_seconds=1.0)
        except PortalLocalLoginError:
            return False
        markers = ("所有照片", "图库", "相簿", "照片")
        return any(marker in text for text in texts for marker in markers)

    def _select_latest_qr_in_photos_picker(self) -> None:
        self._log("photo picker detected via Photos app")
        self._activate_application(PHOTOS_BUNDLE_ID)
        deadline = monotonic() + PHOTO_PICKER_SELECT_TIMEOUT_SECONDS
        while monotonic() < deadline:
            nodes = self._nodes_for_bundle(PHOTOS_BUNDLE_ID)
            photo_nodes = [
                node
                for node in nodes
                if node.center is not None
                and any(re.search(r"20\d{2}年\d+月\d+日", text) for text in node.texts)
            ]
            photo_nodes.sort(
                key=lambda node: (
                    node.position[1] if node.position else 10**9,
                    node.position[0] if node.position else 10**9,
                )
            )
            if photo_nodes and self._ax.click_node(photo_nodes[0]):
                return
            self._click_photos_first_item()
            sleep(0.5)
            return
        raise PortalLocalLoginError("Timed out selecting QR image from Photos picker.")

    def _select_latest_qr_in_internal_picker(self, bundle_id: str) -> None:
        self._log("internal photo picker detected in 电子税务局 app")
        for attempt, (x_ratio, y_ratio) in enumerate(IN_APP_PHOTO_PICKER_CLICK_TARGETS, start=1):
            self._activate_application(bundle_id)
            self._click_internal_picker_item(bundle_id, x_ratio=x_ratio, y_ratio=y_ratio)
            sleep(1.0)
            if self._is_login_confirmation_visible(bundle_id):
                self._log(f"internal photo picker selected QR image attempt={attempt}")
                return
        raise PortalLocalLoginError("Timed out selecting QR image from internal photo picker.")

    def _wait_for_login_confirmation_ready(self, bundle_id: str) -> None:
        deadline = monotonic() + LOGIN_CONFIRMATION_TIMEOUT_SECONDS
        while monotonic() < deadline:
            if self._is_login_confirmation_visible(bundle_id):
                return
            sleep(0.3)
        raise PortalLocalLoginError("Timed out waiting for login confirmation dialog after selecting QR image.")

    def _is_login_confirmation_visible(self, bundle_id: str) -> bool:
        try:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
        except PortalLocalLoginError:
            return False
        has_login = any(text == "登录" or text.endswith("登录") for text in texts)
        has_confirmation = any("登录确认" in text for text in texts) or any("确认" in text for text in texts)
        return has_login and has_confirmation and not self._is_scan_page_visible(bundle_id)

    def _request_sms_code(self, bundle_id: str) -> None:
        self._log("requesting SMS verification code")
        for attempt in range(1, SMS_REQUEST_RETRY_ATTEMPTS + 1):
            self._click_named_element(bundle_id, ("获取验证码",), timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
            if self._wait_for_sms_countdown(bundle_id, timeout_seconds=SMS_REQUEST_SETTLE_TIMEOUT_SECONDS):
                self._log(f"SMS verification code request accepted attempt={attempt}")
                return
            self._log(f"SMS verification code request did not enter countdown attempt={attempt}")
        raise PortalLocalLoginError("Timed out waiting for SMS verification countdown after requesting code.")

    def _wait_for_sms_countdown(self, bundle_id: str, *, timeout_seconds: float) -> bool:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
            if any(SMS_RESEND_COUNTDOWN_REGEX.search(text) for text in texts):
                return True
            sleep(0.3)
        return False

    def _confirm_role_selection(self, bundle_id: str, *, role_already_selected: bool = False) -> str:
        state = "role_dialog"
        for attempt in range(1, ROLE_DIALOG_CONFIRM_ATTEMPTS + 1):
            if not role_already_selected or attempt > 1:
                sleep(ROLE_DIALOG_BEFORE_SELECT_SECONDS)
                self._select_role_option(bundle_id)
                sleep(ROLE_DIALOG_SELECTION_SETTLE_SECONDS)
            self._confirm_role_dialog(bundle_id)
            state = self._wait_for_post_login_state(bundle_id, timeout_seconds=5.0)
            if state != "role_dialog":
                self._log(
                    f"selected identity role in 电子税务局 app role={self.role_label} attempt={attempt}"
                )
                return state
            self._log(f"identity role confirmation did not dismiss dialog attempt={attempt}")
        return state

    def _select_role_option(self, bundle_id: str) -> None:
        try:
            self._click_named_element(bundle_id, (self.role_label,), timeout_seconds=3.0)
            return
        except PortalLocalLoginError:
            pass
        self._click_role_dialog_relative(bundle_id, x_ratio=0.32, y_ratio=0.47)

    def _confirm_role_dialog(self, bundle_id: str) -> None:
        try:
            self._click_named_element(bundle_id, ("确认",), timeout_seconds=3.0)
            return
        except PortalLocalLoginError:
            pass
        self._click_role_dialog_relative(bundle_id, x_ratio=0.50, y_ratio=0.64)

    def _click_role_dialog_relative(self, bundle_id: str, *, x_ratio: float, y_ratio: float) -> None:
        self._activate_application(bundle_id)
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine role dialog position.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        self._click_at_for_bundle(bundle_id, left + width * x_ratio, top + height * y_ratio)

    def _dismiss_fingerprint_prompt(self, bundle_id: str) -> None:
        self._activate_application(bundle_id)
        try:
            self._click_named_element(bundle_id, ("暂不设置",), timeout_seconds=3.0)
            return
        except PortalLocalLoginError:
            pass
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine fingerprint quick-login prompt position.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        self._click_at_for_bundle(bundle_id, left + width * 0.33, top + height * 0.69)

    def _wait_for_post_login_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
            if self._texts_show_role_dialog(texts):
                return "role_dialog"
            if "暂不设置" in texts or any("指纹快捷登录" in text for text in texts):
                return "fingerprint_prompt"
            if self._texts_show_logged_in_home(texts):
                return "home"
            if any("立即登录" in text for text in texts):
                return "login_page"
            sleep(0.3)
        return "timeout"

    def _texts_show_role_dialog(self, texts: list[str]) -> bool:
        has_role = any(self.role_label in text for text in texts)
        has_confirm = any(text == "确认" or text.endswith("确认") for text in texts)
        return has_role and has_confirm

    @staticmethod
    def _texts_show_logged_in_home(texts: list[str]) -> bool:
        indicators = ("功能名称", "申报期截止至")
        return any(any(indicator in text for indicator in indicators) for text in texts) and not any(
            "立即登录" in text for text in texts
        )

    def _try_fill_otp_from_system_prompt(self, bundle_id: str) -> bool:
        self._log("waiting for macOS one-time-code prompt")
        deadline = monotonic() + OTP_AUTOFILL_TIMEOUT_SECONDS
        while monotonic() < deadline:
            if self._otp_field_has_code(bundle_id):
                return True
            visible_texts = self._collect_visible_texts(bundle_id, timeout_seconds=3.0)
            code = self._extract_first_sms_code(visible_texts)
            if code and self._maybe_click_named_element(bundle_id, (code,), timeout_seconds=2.0, contains=True):
                if self._otp_field_has_code(bundle_id):
                    return True
            sleep(0.5)
        return False

    def _read_latest_sms_code_from_messages(self) -> str:
        self._log("reading latest 厦门税务 verification code from Messages app")
        self._activate_application(MESSAGES_BUNDLE_ID)
        self._wait_for_process(MESSAGES_BUNDLE_ID, timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
        deadline = monotonic() + OTP_MESSAGES_FALLBACK_TIMEOUT_SECONDS
        while monotonic() < deadline:
            visible_texts = self._collect_visible_texts(MESSAGES_BUNDLE_ID, timeout_seconds=3.0)
            code = self._extract_first_sms_code(visible_texts)
            if code:
                return code
            sleep(0.5)
        raise PortalLocalLoginError("Timed out waiting for 厦门税务 verification code in Messages app.")

    def _otp_field_has_code(self, bundle_id: str) -> bool:
        value = self._read_text_input_value(bundle_id, labels=("短信验证码",), field_index=2)
        return bool(re.fullmatch(r"\d{6}", value))

    def _collect_visible_texts(self, bundle_id: str, *, timeout_seconds: float) -> list[str]:
        self._activate_application(bundle_id)
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            nodes = self._nodes_for_bundle(bundle_id)
            texts: list[str] = []
            for node in nodes:
                for text in node.texts:
                    normalized = self._normalized_text(text)
                    if normalized and normalized not in texts:
                        texts.append(normalized)
            if texts:
                return texts
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        raise PortalLocalLoginError(f"Timed out collecting visible texts from bundle {bundle_id}.")

    @staticmethod
    def _extract_first_sms_code(texts: Iterable[str]) -> str | None:
        for text in texts:
            match = OTP_REGEX.search(text)
            if match:
                return match.group(1)
        for text in texts:
            match = OTP_DIGITS_REGEX.search(text)
            if match:
                return match.group(1)
        return None

    def _click_named_element(
        self,
        bundle_id: str,
        names: tuple[str, ...],
        *,
        timeout_seconds: float,
        contains: bool = False,
    ) -> str:
        self._activate_application(bundle_id)
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            node = self._find_named_node(bundle_id, names, contains=contains)
            if node is not None and self._click_node_for_bundle(bundle_id, node):
                return node.texts[0] if node.texts else "clicked"
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        raise PortalLocalLoginError(f"Timed out clicking element in bundle {bundle_id}.")

    def _maybe_click_named_element(
        self,
        bundle_id: str,
        names: tuple[str, ...],
        *,
        timeout_seconds: float,
        contains: bool = False,
    ) -> bool:
        try:
            self._click_named_element(bundle_id, names, timeout_seconds=timeout_seconds, contains=contains)
        except PortalLocalLoginError:
            return False
        return True

    def _focus_text_input(
        self,
        bundle_id: str,
        labels: tuple[str, ...],
        *,
        field_index: int,
        secure: bool = False,
    ) -> None:
        self._activate_application(bundle_id)
        node = self._find_text_field_node(bundle_id, labels=labels, field_index=field_index, secure=secure)
        if node is None or not self._click_node_for_bundle(bundle_id, node):
            raise PortalLocalLoginError("Unable to find target text field")

    def _set_text_input_value(
        self,
        bundle_id: str,
        value: str,
        *,
        field_index: int,
        labels: tuple[str, ...] = (),
        secure: bool = False,
    ) -> None:
        self._activate_application(bundle_id)
        node = self._find_text_field_node(bundle_id, labels=labels, field_index=field_index, secure=secure)
        if node is None:
            raise PortalLocalLoginError(
                "Unable to set target text field value "
                f"field_index={field_index} secure={secure} labels={labels or ('<none>',)}"
            )
        if self._ax.set_text_value(node, value):
            return
        if not self._click_node_for_bundle(bundle_id, node):
            raise PortalLocalLoginError(
                "Unable to focus target text field for keyboard fallback "
                f"field_index={field_index} secure={secure} labels={labels or ('<none>',)}"
            )
        self._ax.send_modified_key(KEY_A, CG_EVENT_FLAG_MASK_COMMAND)
        self._ax.send_key(KEY_DELETE)
        self._ax.type_text(value)

    def _read_text_input_value(
        self,
        bundle_id: str,
        *,
        labels: tuple[str, ...],
        field_index: int,
        secure: bool = False,
    ) -> str:
        node = self._find_text_field_node(bundle_id, labels=labels, field_index=field_index, secure=secure)
        if node is None:
            return ""
        for text in node.texts:
            if re.fullmatch(r"\d{6}", text):
                return text
        return ""

    @staticmethod
    def _is_etax_bundle(bundle_id: str) -> bool:
        return bundle_id not in {MESSAGES_BUNDLE_ID, PHOTOS_BUNDLE_ID}

    def _wait_before_bundle_click(self, bundle_id: str) -> None:
        if self._is_etax_bundle(bundle_id):
            sleep(ETAX_CLICK_PRE_DELAY_SECONDS)

    def _click_node_for_bundle(self, bundle_id: str, node: AXNode) -> bool:
        self._wait_before_bundle_click(bundle_id)
        return self._ax.click_node(node)

    def _click_at_for_bundle(self, bundle_id: str, x: float, y: float) -> None:
        self._wait_before_bundle_click(bundle_id)
        self._ax.click_at(x, y)

    def _send_key_to_front_process(self, bundle_id: str, *, key_code: int) -> None:
        _ = bundle_id
        self._ax.send_key(key_code)

    def _find_process_pids(self, bundle_id: str) -> list[int]:
        if bundle_id == MESSAGES_BUNDLE_ID:
            pattern = MESSAGES_PROCESS_PATTERN
        elif bundle_id == PHOTOS_BUNDLE_ID:
            pattern = PHOTOS_PROCESS_PATTERN
        else:
            pattern = ETAX_PROCESS_PATTERN
        try:
            output = self._run_command(["pgrep", "-f", pattern], timeout_seconds=3.0)
        except PortalLocalLoginError:
            return []
        pids: list[int] = []
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.isdigit():
                pid = int(stripped)
                if pid not in pids:
                    pids.append(pid)
        pids.sort(reverse=True)
        return pids

    def _nodes_for_bundle(self, bundle_id: str) -> list[AXNode]:
        for pid in self._find_process_pids(bundle_id):
            nodes = self._ax.find_nodes(pid)
            if nodes:
                self._remember_window_size(nodes)
                return nodes
        focused_nodes = self._ax.find_focused_nodes()
        if focused_nodes:
            self._remember_window_size(focused_nodes)
            return focused_nodes
        return []

    def _remember_window_size(self, nodes: list[AXNode]) -> None:
        for node in nodes:
            if node.role == "AXWindow" and node.size is not None:
                self._etax_last_window_size = node.size
                return

    @staticmethod
    def _nodes_look_ready(nodes: list[AXNode]) -> bool:
        for node in nodes:
            if node.role == "AXWindow" and node.position is not None and node.size is not None:
                return True
        return any(node.texts for node in nodes)

    def _find_named_node(self, bundle_id: str, names: tuple[str, ...], *, contains: bool = False) -> AXNode | None:
        normalized_names = [self._normalized_text(name) for name in names if self._normalized_text(name)]
        for node in self._nodes_for_bundle(bundle_id):
            for text in node.texts:
                normalized_text = self._normalized_text(text)
                for normalized_name in normalized_names:
                    if contains:
                        if normalized_name in normalized_text:
                            return node
                    elif normalized_text == normalized_name:
                        return node
        return None

    def _find_text_field_node(
        self,
        bundle_id: str,
        *,
        labels: tuple[str, ...],
        field_index: int,
        secure: bool,
    ) -> AXNode | None:
        normalized_labels = [self._normalized_text(label) for label in labels if self._normalized_text(label)]
        field_nodes = [
            node
            for node in self._nodes_for_bundle(bundle_id)
            if (
                (secure and node.role in {"AXSecureTextField", "AXTextField"})
                or ((not secure) and node.role == "AXTextField")
            )
            and ((node.role == "AXSecureTextField") or ((node.subrole == "AXSecureTextField") if secure else (node.subrole != "AXSecureTextField")))
        ]
        for node in field_nodes:
            for text in node.texts:
                normalized_text = self._normalized_text(text)
                if any(label in normalized_text for label in normalized_labels):
                    return node
        field_nodes.sort(
            key=lambda node: (
                node.position[1] if node.position else 10**9,
                node.position[0] if node.position else 10**9,
            )
        )
        if len(field_nodes) >= field_index:
            return field_nodes[field_index - 1]
        return None

    def _window_node(self, bundle_id: str) -> AXNode | None:
        nodes = self._nodes_for_bundle(bundle_id)
        for node in nodes:
            if node.role == "AXWindow" and node.position is not None and node.size is not None:
                return node
        sized_nodes = [node for node in nodes if node.position is not None and node.size is not None]
        sized_nodes.sort(
            key=lambda node: (node.size[0] * node.size[1]) if node.size is not None else 0.0,
            reverse=True,
        )
        return sized_nodes[0] if sized_nodes else None

    def _window_bounds_for_bundle(self, bundle_id: str) -> tuple[float, float, float, float] | None:
        for pid in self._find_process_pids(bundle_id):
            bounds = self._ax.window_bounds(pid)
            if bounds is not None:
                return bounds
        return None

    def _click_etax_tabbar_item(self, bundle_id: str, index: int) -> None:
        self._activate_application(bundle_id)
        tabbar_nodes = [
            node
            for node in self._nodes_for_bundle(bundle_id)
            if any("tabbarItem" in text for text in node.texts)
        ]
        tabbar_nodes.sort(
            key=lambda node: (
                node.position[0] if node.position else 10**9,
                node.position[1] if node.position else 10**9,
            )
        )
        if len(tabbar_nodes) >= index + 1:
            if self._click_node_for_bundle(bundle_id, tabbar_nodes[index]):
                return
        window = self._window_node(bundle_id)
        if window is None or window.position is None or window.size is None:
            bounds = self._window_bounds_for_bundle(bundle_id)
            if bounds is not None:
                x, y, width, height = bounds
                self._click_at_for_bundle(
                    bundle_id,
                    x + width * ((index + 0.5) / 5.0),
                    y + height * (1.0 - (28.0 / max(height, 1.0))),
                )
                return
            node_count = len(self._nodes_for_bundle(bundle_id))
            raise PortalLocalLoginError(
                f"Unable to determine 电子税务局 window frame for tabbar click. node_count={node_count}"
            )
        x = window.position[0] + window.size[0] * ((index + 0.5) / 5.0)
        y = window.position[1] + window.size[1] - 28.0
        self._click_at_for_bundle(bundle_id, x, y)

    def _click_etax_scan_icon(self, bundle_id: str) -> None:
        self._activate_application(bundle_id)
        window = self._window_node(bundle_id)
        if window is None or window.position is None or window.size is None:
            bounds = self._window_bounds_for_bundle(bundle_id)
            if bounds is not None:
                x, y, width, height = bounds
                self._click_at_for_bundle(bundle_id, x + width * SCAN_ICON_X_RATIO, y + height * SCAN_ICON_Y_RATIO)
                return
            raise PortalLocalLoginError("Unable to determine 电子税务局 window frame for scan icon click.")
        x = window.position[0] + window.size[0] * SCAN_ICON_X_RATIO
        y = window.position[1] + window.size[1] * SCAN_ICON_Y_RATIO
        self._click_at_for_bundle(bundle_id, x, y)

    def _click_etax_album_button(self, bundle_id: str) -> None:
        self._activate_application(bundle_id)
        window = self._window_node(bundle_id)
        if window is None or window.position is None or window.size is None:
            bounds = self._window_bounds_for_bundle(bundle_id)
            if bounds is not None:
                x, y, width, height = bounds
                self._click_at_for_bundle(bundle_id, x + width - 26.0, y + height - 24.0)
                return
            raise PortalLocalLoginError("Unable to determine 电子税务局 window frame for album button click.")
        x = window.position[0] + window.size[0] - 26.0
        y = window.position[1] + window.size[1] - 24.0
        self._click_at_for_bundle(bundle_id, x, y)

    def _click_etax_latest_photo(self, bundle_id: str) -> None:
        self._activate_application(bundle_id)
        window = self._window_node(bundle_id)
        if window is None or window.position is None or window.size is None:
            bounds = self._window_bounds_for_bundle(bundle_id)
            if bounds is not None:
                x, y, width, height = bounds
                self._click_at_for_bundle(bundle_id, x + width * 0.18, y + height * 0.23)
                return
            raise PortalLocalLoginError("Unable to determine 电子税务局 window frame for latest photo click.")
        x = window.position[0] + window.size[0] * 0.18
        y = window.position[1] + window.size[1] * 0.23
        self._click_at_for_bundle(bundle_id, x, y)

    def _click_photos_first_item(self) -> None:
        self._activate_application(PHOTOS_BUNDLE_ID)
        bounds = self._window_bounds_for_bundle(PHOTOS_BUNDLE_ID)
        if bounds is None:
            window = self._window_node(PHOTOS_BUNDLE_ID)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine Photos picker item position.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        self._ax.click_at(left + width * PHOTOS_FIRST_ITEM_CLICK_X_RATIO, top + height * PHOTOS_FIRST_ITEM_CLICK_Y_RATIO)

    def _click_internal_picker_item(self, bundle_id: str, *, x_ratio: float, y_ratio: float) -> None:
        self._activate_application(bundle_id)
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine internal photo picker item position.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        self._click_at_for_bundle(bundle_id, left + width * x_ratio, top + height * y_ratio)

    def _click_scan_album_region(self, bundle_id: str, attempt: int) -> None:
        self._activate_application(bundle_id)
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine scan album region position.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        targets = (
            (0.88, 0.70),
            (0.88, 0.78),
            (0.84, 0.74),
            (0.92, 0.74),
        )
        x_ratio, y_ratio = targets[min(attempt - 1, len(targets) - 1)]
        self._click_at_for_bundle(bundle_id, left + width * x_ratio, top + height * y_ratio)

    def _set_login_account_value(self, bundle_id: str, value: str) -> None:
        if self._try_set_field_value(bundle_id, value, field_index=1, secure=False):
            return
        self._click_login_field_relative(bundle_id, x_ratio=0.52, y_ratio=0.36)
        self._clear_and_type_text(value)

    def _set_login_password_value(self, bundle_id: str, value: str) -> None:
        if self._try_set_field_value(bundle_id, value, field_index=1, secure=True):
            return
        self._click_login_field_relative(bundle_id, x_ratio=0.50, y_ratio=0.49)
        self._clear_and_type_text(value)

    def _focus_sms_code_input(self, bundle_id: str) -> None:
        node = self._find_text_field_node(bundle_id, labels=("短信验证码",), field_index=2, secure=False)
        if node is not None and self._click_node_for_bundle(bundle_id, node):
            return
        self._click_login_field_relative(bundle_id, x_ratio=0.43, y_ratio=0.49)

    def _set_sms_code_value(self, bundle_id: str, value: str) -> None:
        if self._try_set_field_value(bundle_id, value, field_index=2, labels=("短信验证码",), secure=False):
            return
        self._focus_sms_code_input(bundle_id)
        self._clear_and_type_text(value)

    def _try_set_field_value(
        self,
        bundle_id: str,
        value: str,
        *,
        field_index: int,
        labels: tuple[str, ...] = (),
        secure: bool,
    ) -> bool:
        try:
            self._set_text_input_value(bundle_id, value, field_index=field_index, labels=labels, secure=secure)
        except PortalLocalLoginError:
            return False
        return True

    def _click_login_field_relative(self, bundle_id: str, *, x_ratio: float, y_ratio: float) -> None:
        self._activate_application(bundle_id)
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine login field position.")
            x = window.position[0] + window.size[0] * x_ratio
            y = window.position[1] + window.size[1] * y_ratio
        else:
            left, top, width, height = bounds
            x = left + width * x_ratio
            y = top + height * y_ratio
        self._click_at_for_bundle(bundle_id, x, y)

    def _clear_and_type_text(self, value: str) -> None:
        self._ax.send_modified_key(KEY_A, CG_EVENT_FLAG_MASK_COMMAND)
        self._ax.send_key(KEY_DELETE)
        self._ax.type_text(value)

    def _activate_application(self, bundle_id: str) -> None:
        if bundle_id == MESSAGES_BUNDLE_ID:
            command = ["open", "-a", "Messages"]
        elif bundle_id == PHOTOS_BUNDLE_ID:
            command = ["open", "-a", "Photos"]
        else:
            command = ["open", "-a", str(self._etax_app_path)]
        try:
            self._run_command(command, timeout_seconds=5.0)
        except PortalLocalLoginError:
            return

    @staticmethod
    def _normalized_text(value: str) -> str:
        return "".join(value.split()).strip()

    def _run_command(self, command: list[str], *, timeout_seconds: float) -> str:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise PortalLocalLoginError(stderr or f"Command failed: {' '.join(command)}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PortalLocalLoginError(f"Command timed out: {' '.join(command)}") from exc
        return (completed.stdout or "").strip()

    def _run_applescript(self, script: str, *, timeout_seconds: float) -> str:
        try:
            completed = subprocess.run(
                ["osascript", "-"],
                input=script,
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise PortalLocalLoginError(stderr or "AppleScript command failed.") from exc
        except subprocess.TimeoutExpired as exc:
            raise PortalLocalLoginError("AppleScript command timed out.") from exc
        return (completed.stdout or "").strip()

    @staticmethod
    def _apple_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _log(self, message: str) -> None:
        self._logger(self.store_key, message)
