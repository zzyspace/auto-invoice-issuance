from __future__ import annotations

import base64
import binascii
import ctypes
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp
from time import monotonic, sleep, time_ns
from typing import Callable, Iterable

from app.models import AppConfig
from app.photos_qr_cleanup import ImportedPhotosQr, PhotosQrCleanupError, describe_imported_qr
from app.utils import ensure_parent_dir
from app.vision_client import OpenAICompatibleVisionClient

DEFAULT_ETAX_APP_PATH = Path("/Applications/电子税务局.app")
ETAX_APP_BUNDLE_ID_FALLBACK = "cn.gov.chinatax.gt4.app"
MESSAGES_BUNDLE_ID = "com.apple.MobileSMS"
PHOTOS_BUNDLE_ID = "com.apple.Photos"
VISIBLE_ELEMENT_POLL_SECONDS = 0.2
UI_ACTION_TIMEOUT_SECONDS = 20.0
OTP_AUTOFILL_TIMEOUT_SECONDS = 50.0
OTP_MESSAGES_FALLBACK_TIMEOUT_SECONDS = 60.0
PHOTOS_IMPORT_SETTLE_SECONDS = 1.0
QR_MIN_DIMENSION_PX = 120
QR_CAPTURE_RETRY_DELAY_SECONDS = 3.0
QR_PNG_DATA_URL_PREFIX = "data:image/png;base64,"
OTP_TAX_VERIFICATION_REGEX = re.compile(r"【(?P<issuer>[^】]*税务)】您的验证码是[:：]\s*(?P<code>\d{6})")
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
SCAN_ALBUM_OPEN_ATTEMPTS = 2
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
HOME_PORTAL_AREA_TIMEOUT_SECONDS = 8.0
HOME_PORTAL_AREA_X_MAX_RATIO = 0.45
HOME_PORTAL_AREA_Y_MAX_RATIO = 0.22
HOME_PORTAL_AREA_SCREENSHOT_X_RATIO = 0.02
HOME_PORTAL_AREA_SCREENSHOT_Y_RATIO = 0.06
HOME_PORTAL_AREA_SCREENSHOT_WIDTH_RATIO = 0.20
HOME_PORTAL_AREA_SCREENSHOT_HEIGHT_RATIO = 0.11
PORTAL_AREA_SWITCH_PAGE_TIMEOUT_SECONDS = 10.0
PORTAL_AREA_SWITCH_SETTLE_SECONDS = 1.0
SWITCH_SUCCESS_DIALOG_CONFIRM_X_RATIO = 0.50
SWITCH_SUCCESS_DIALOG_CONFIRM_Y_RATIO = 0.62
STARTUP_REMINDER_TITLE = "关于电子税务局上线申报智能自检的提醒"
STARTUP_REMINDER_CLOSE_X_RATIO = 0.50
STARTUP_REMINDER_CLOSE_Y_RATIO = 0.88
STARTUP_REMINDER_DISMISS_ATTEMPTS = 3
STARTUP_REMINDER_DISMISS_SETTLE_SECONDS = 2.0
ETAX_SESSION_ENTRY_TIMEOUT_SECONDS = 6.0
PORTAL_AREA_TEXT_REGEX = re.compile(r"^[一-龥]{2,6}(?:省|市)?$")
PORTAL_AREA_TEXT_IGNORE = {
    "全国",
    "首页",
    "功能名称",
    "扫一扫",
    "扫码",
    "登录",
    "确认",
    "身份切换",
}
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
PHOTOS_BACKGROUND_IMPORT_SCRIPT = """
on run argv
    set qrFile to POSIX file (item 1 of argv)
    tell application "/System/Applications/Photos.app"
        set importedItems to import {qrFile} skip check duplicates yes
        if (count of importedItems) is not 1 then error "Expected one imported Photos media item"
        return id of item 1 of importedItems
    end tell
end run
""".strip()
PHOTOS_HIDE_SCRIPT = """
tell application "System Events"
    if exists (first application process whose bundle identifier is "com.apple.Photos") then
        set visible of (first application process whose bundle identifier is "com.apple.Photos") to false
    end if
end tell
""".strip()
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
        stack = [element]
        while stack:
            current = stack.pop()
            if not current or current in seen:
                continue
            seen.add(current)
            output.append(
                AXNode(
                    element=current,
                    role=self._attribute_text(current, "AXRole"),
                    subrole=self._attribute_text(current, "AXSubrole"),
                    texts=self._texts_for_element(current),
                    position=self._point_attribute(current, "AXPosition"),
                    size=self._size_attribute(current, "AXSize"),
                )
            )
            children = self._children_from_attribute(current, "AXChildren")
            stack.extend(reversed(children))

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
        portal_area_name: str | None = None,
        portal_company_switch_name: str | None = None,
    ) -> None:
        self.config = config
        self.store_key = store_key
        self.role_label = role_label
        self._logger = logger
        self.portal_area_name = (portal_area_name or "").strip()
        self.portal_company_switch_name = (portal_company_switch_name or "").strip()
        self._etax_app_path = config.portal_etax_app_path or DEFAULT_ETAX_APP_PATH
        self._ax = MacAccessibilityClient()
        self._etax_last_window_size: tuple[float, float] = (288.0, 552.0)
        self._startup_reminder_handled = False
        self._vision_client: OpenAICompatibleVisionClient | None = None
        self.imported_qr: ImportedPhotosQr | None = None

    @staticmethod
    def is_enabled(config: AppConfig) -> bool:
        return (
            sys.platform == "darwin"
            and config.portal_browser_backend == "chrome_cdp"
            and bool((config.portal_etax_app_username or "").strip())
            and bool((config.portal_etax_app_password or "").strip())
        )

    def automate(self, page: object, artifacts_dir: Path | None) -> ImportedPhotosQr:
        self._require_supported_environment()
        self._verify_gui_automation_prerequisites()
        qr_path = self._capture_qr_code(page, artifacts_dir)
        imported_qr = self._import_qr_into_photos(qr_path)
        self.imported_qr = imported_qr
        bundle_id = self._resolve_app_bundle_identifier()
        self._launch_etax_app()
        self._wait_for_process(bundle_id, timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
        if self._home_page_is_logged_in(bundle_id):
            self._log("电子税务局 首页已显示身份切换; 跳过我的页登录态确认")
        else:
            self._ensure_etax_session(bundle_id)
        self._open_home_tab(bundle_id)
        self._ensure_home_portal_area(bundle_id)
        self._open_scan_flow(bundle_id)
        self._select_latest_qr_from_album(bundle_id)
        self._confirm_scan_login(bundle_id)
        return imported_qr

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

    def _capture_qr_code(self, page: object, artifacts_dir: Path | None) -> Path:
        self._log("saving tax portal login QR from page data URL")
        target_dir = artifacts_dir or Path(mkdtemp(prefix="portal-local-login-"))
        ensure_parent_dir(target_dir / "placeholder.txt")
        target = target_dir / f"login-qr-{time_ns()}.png"
        if self._try_capture_qr_code(page, target):
            self._log(f"saved tax portal login QR path={target}")
            return target
        self._log(
            f"tax portal login QR not ready; waiting {QR_CAPTURE_RETRY_DELAY_SECONDS:.0f}s before retry"
        )
        sleep(QR_CAPTURE_RETRY_DELAY_SECONDS)
        if self._try_capture_qr_code(page, target):
            self._log(f"saved tax portal login QR path={target}")
            return target
        raise PortalLocalLoginError(
            "Failed to locate a visible QR image with a readable PNG data URL on the tax portal login page."
        )

    def _try_capture_qr_code(self, page: object, target: Path) -> bool:
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
                    src = candidate.get_attribute("src")
                    if not src or not src.startswith(QR_PNG_DATA_URL_PREFIX):
                        continue
                    try:
                        png = base64.b64decode(src[len(QR_PNG_DATA_URL_PREFIX) :], validate=True)
                    except (binascii.Error, ValueError):
                        continue
                    if (
                        len(png) < 24
                        or png[:8] != b"\x89PNG\r\n\x1a\n"
                        or png[8:12] != b"\x00\x00\x00\r"
                        or png[12:16] != b"IHDR"
                    ):
                        continue
                    width = int.from_bytes(png[16:20], "big")
                    height = int.from_bytes(png[20:24], "big")
                    if width < QR_MIN_DIMENSION_PX or height < QR_MIN_DIMENSION_PX:
                        continue
                    target.write_bytes(png)
                    return True
                except Exception:
                    continue
        return False

    def _import_qr_into_photos(self, qr_path: Path) -> ImportedPhotosQr:
        self._log(f"importing tax portal login QR into Photos path={qr_path}")
        try:
            self._run_command(
                ["open", "-g", "-j", "-a", "Photos"],
                timeout_seconds=UI_ACTION_TIMEOUT_SECONDS,
            )
            self._hide_photos_application()
            asset_id = self._run_command(
                [
                    "osascript",
                    "-e",
                    PHOTOS_BACKGROUND_IMPORT_SCRIPT,
                    str(qr_path),
                ],
                timeout_seconds=UI_ACTION_TIMEOUT_SECONDS,
            )
        except PortalLocalLoginError as exc:
            raise PortalLocalLoginError(
                "Failed to import tax portal QR image into Photos in the background. "
                f"Original error: {exc}"
            ) from exc
        self._hide_photos_application()
        sleep(PHOTOS_IMPORT_SETTLE_SECONDS)
        try:
            imported_qr = describe_imported_qr(qr_path, asset_id)
        except PhotosQrCleanupError as exc:
            raise PortalLocalLoginError(
                f"Photos imported the QR image but did not return a safe cleanup identity: {exc}"
            ) from exc
        self._log(
            "imported tax portal login QR into Photos "
            f"asset_id={imported_qr.asset_id} filename={imported_qr.original_filename}"
        )
        return imported_qr

    def _hide_photos_application(self) -> None:
        try:
            self._run_command(
                ["osascript", "-e", PHOTOS_HIDE_SCRIPT],
                timeout_seconds=5.0,
            )
        except PortalLocalLoginError:
            pass

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
                    window_bounds = getattr(self._ax, "window_bounds", None)
                    bounds = window_bounds(pid) if callable(window_bounds) else None
                    if bounds is not None:
                        self._etax_last_window_size = (bounds[2], bounds[3])
                        self._log(
                            f"detected candidate process pids bundle_id={bundle_id} "
                            f"pids={pids} ready_pid={pid}"
                        )
                        return
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
        self._dismiss_startup_reminder_if_present(bundle_id)
        self._click_etax_tabbar_item(bundle_id, 4)
        if self._maybe_click_named_element(bundle_id, ("立即登录",), timeout_seconds=4.0):
            self._log("电子税务局 我的页入口动作 action=login_button")
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
            self._handle_post_sms_login_state(bundle_id, state)
            return
        if self._maybe_click_named_element(bundle_id, (self.role_label,), timeout_seconds=8.0):
            self._log("电子税务局 我的页入口动作 action=role_entry")
            state = self._confirm_role_selection(bundle_id, role_already_selected=True)
            if state == "home":
                self._log("identity role selection entered logged-in home directly without fingerprint prompt")
                return
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
            return
        entry_state = self._wait_for_etax_session_entry_state(
            bundle_id,
            timeout_seconds=ETAX_SESSION_ENTRY_TIMEOUT_SECONDS,
        )
        self._log(f"电子税务局 我的页入口状态 state={entry_state}")
        if entry_state == "home":
            return
        raise PortalLocalLoginError(
            "电子税务局 app did not show login button, role entry, or logged-in home after opening 我的."
        )

    def _handle_post_sms_login_state(self, bundle_id: str, state: str) -> None:
        if state == "role_dialog":
            state = self._confirm_role_selection(bundle_id)
            if state == "home":
                self._log("identity role selection entered logged-in home directly without fingerprint prompt")
                return
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
                return
            if state != "home":
                raise PortalLocalLoginError(
                    f"电子税务局 app did not reach logged-in home after fingerprint prompt state={state}"
                )
            return
        if state == "fingerprint_prompt":
            self._log("role selection not shown after SMS login; continuing with fingerprint quick-login prompt")
            self._dismiss_fingerprint_prompt(bundle_id)
            self._log("dismissed fingerprint quick login prompt")
            state = self._wait_for_post_login_state(bundle_id, timeout_seconds=POST_LOGIN_STATE_TIMEOUT_SECONDS)
            if state == "login_page":
                self._log("fingerprint prompt dismissed but home markers not yet visible; continuing to scan flow")
                return
            if state != "home":
                raise PortalLocalLoginError(
                    f"电子税务局 app did not reach logged-in home after fingerprint prompt state={state}"
                )
            return
        if state == "home":
            self._log("role selection not shown after SMS login; home markers already visible")
            return
        raise PortalLocalLoginError(
            f"电子税务局 app did not reach role selection after SMS login state={state}"
        )

    def _wait_for_etax_session_entry_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
            if self._texts_show_logged_in_home(texts):
                return "home"
            if any("立即登录" in text for text in texts):
                return "login_button"
            if any(self.role_label in text for text in texts):
                return "role_entry"
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        return "unknown"

    def _home_page_is_logged_in(self, bundle_id: str) -> bool:
        for node in self._nodes_for_bundle(bundle_id):
            if node.center is None:
                continue
            if any("身份切换" in self._normalized_text(text) for text in node.texts):
                return True
        return False

    def _dismiss_startup_reminder_if_present(self, bundle_id: str) -> None:
        if self._startup_reminder_handled:
            return
        self._startup_reminder_handled = True
        for attempt in range(1, STARTUP_REMINDER_DISMISS_ATTEMPTS + 1):
            if not self._startup_reminder_visible(bundle_id):
                return
            self._log(f"startup reminder detected; dismissing attempt={attempt}")
            self._click_startup_reminder_close(bundle_id)
            sleep(STARTUP_REMINDER_DISMISS_SETTLE_SECONDS)
        if self._startup_reminder_visible(bundle_id):
            raise PortalLocalLoginError("Failed to dismiss 电子税务局 startup reminder popup.")

    def _startup_reminder_visible(self, bundle_id: str) -> bool:
        ocr_visible = self._ocr_startup_reminder_visible(bundle_id)
        if ocr_visible is not None:
            return ocr_visible
        return self._startup_reminder_visible_from_ax_text(bundle_id)

    def _startup_reminder_visible_from_ax_text(self, bundle_id: str) -> bool:
        try:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
        except PortalLocalLoginError:
            return False
        return any(STARTUP_REMINDER_TITLE in text for text in texts)

    def _click_startup_reminder_close(self, bundle_id: str) -> None:
        self._activate_application(bundle_id)
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine startup reminder popup position.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        self._click_at_for_bundle(
            bundle_id,
            left + width * STARTUP_REMINDER_CLOSE_X_RATIO,
            top + height * STARTUP_REMINDER_CLOSE_Y_RATIO,
        )

    def _ocr_startup_reminder_visible(self, bundle_id: str) -> bool | None:
        try:
            image_path = self._capture_startup_reminder_screenshot(bundle_id)
        except PortalLocalLoginError as exc:
            self._log(f"could not capture startup reminder screenshot error={exc}")
            return None
        try:
            visible, raw_response = self._recognize_startup_reminder_visibility_from_image(image_path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"could not OCR startup reminder screenshot error={exc}")
            return None
        self._log(
            f"startup reminder OCR path={image_path} visible={visible} raw={raw_response!r}"
        )
        return visible

    def _capture_startup_reminder_screenshot(self, bundle_id: str) -> Path:
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine 电子税务局 window bounds for startup reminder screenshot.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        target_dir = Path(mkdtemp(prefix="portal-startup-reminder-"))
        target = target_dir / "startup-reminder.png"
        self._run_command(
            [
                "screencapture",
                "-x",
                "-R",
                f"{int(left)},{int(top)},{max(int(width), 40)},{max(int(height), 40)}",
                str(target),
            ],
            timeout_seconds=UI_ACTION_TIMEOUT_SECONDS,
        )
        if not target.exists():
            raise PortalLocalLoginError("Startup reminder screenshot was not created.")
        return target

    def _recognize_startup_reminder_visibility_from_image(self, image_path: Path) -> tuple[bool, str]:
        client = self._get_vision_client()
        prompt = (
            "请只判断截图中央是否仍然显示一个居中的模态弹窗。"
            "如果截图中央存在一个大面积白色内容区、上方蓝色圆角标题栏的模态弹窗，只返回 YES。"
            "如果不存在这样的居中模态弹窗，则只返回 NONE。"
            "不要判断标题，也不要受页面顶部横幅、列表卡片、滚动内容文案影响。"
            "不要输出解释或额外文字。"
        )
        raw = image_path.read_bytes()
        response = client._chat_completion(
            base64.b64encode(raw).decode("ascii"),
            image_path.name,
            prompt,
        )
        first_line = response.strip().splitlines()[0].strip()
        normalized = self._normalized_text(first_line)
        normalized_upper = normalized.upper()
        if normalized_upper in {"NONE", "NO", "FALSE"} or normalized in {"无", "没有", "不存在"}:
            return False, first_line
        if normalized_upper in {"YES", "TRUE"} or normalized in {"有", "存在", "可见"}:
            return True, first_line
        raise ValueError(f"Unsupported startup reminder OCR response: {response!r}")

    def _open_scan_flow(self, bundle_id: str) -> None:
        self._log("opening scan flow in 电子税务局 app")
        self._open_home_tab(bundle_id)
        if not self._maybe_click_named_element(bundle_id, ("扫一扫", "扫码"), timeout_seconds=3.0):
            self._click_etax_scan_icon(bundle_id)
        self._wait_for_scan_page_ready(bundle_id)
        self._open_album_from_scan_page(bundle_id)

    def _open_home_tab(self, bundle_id: str) -> None:
        self._log("opening 首页 tab in 电子税务局 app")
        self._activate_application(bundle_id)
        self._click_etax_tabbar_item(bundle_id, 0)

    def _ensure_home_portal_area(self, bundle_id: str) -> None:
        if not self.portal_area_name:
            return
        current_area = self._wait_for_home_portal_area(bundle_id, timeout_seconds=HOME_PORTAL_AREA_TIMEOUT_SECONDS)
        if self._portal_area_text_matches_target(current_area, self.portal_area_name):
            self._log(
                f"tax app home area matches target current={current_area} target={self.portal_area_name}"
            )
            return
        if not self.portal_company_switch_name:
            raise PortalLocalLoginError(
                "Missing portal company switch name for 电子税务局 app area switching."
            )
        self._log(
            "tax app home area mismatch "
            f"current={current_area} target={self.portal_area_name}; opening identity switch"
        )
        self._switch_home_portal_area(bundle_id)

    def _wait_for_home_portal_area(self, bundle_id: str, *, timeout_seconds: float) -> str:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            area_text = self._current_home_portal_area_text(bundle_id)
            if area_text:
                return area_text
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        raise PortalLocalLoginError("Timed out determining current 电子税务局 home area.")

    def _current_home_portal_area_text(self, bundle_id: str) -> str | None:
        nodes = self._nodes_for_bundle(bundle_id)
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                return None
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        candidates: list[tuple[int, float, float, str]] = []
        for node in nodes:
            center = node.center
            if center is None:
                continue
            center_x, center_y = center
            if center_x > left + width * HOME_PORTAL_AREA_X_MAX_RATIO:
                continue
            if center_y > top + height * HOME_PORTAL_AREA_Y_MAX_RATIO:
                continue
            for text in node.texts:
                candidate = self._candidate_portal_area_text(text)
                if candidate is None:
                    continue
                priority = 0 if self._portal_area_text_matches_target(candidate, self.portal_area_name) else 1
                candidates.append((priority, center_y, center_x, candidate))
        if not candidates:
            return self._ocr_home_portal_area_text(bundle_id)
        candidates.sort()
        return candidates[0][3]

    def _ocr_home_portal_area_text(self, bundle_id: str) -> str | None:
        try:
            image_path = self._capture_home_portal_area_screenshot(bundle_id)
        except PortalLocalLoginError as exc:
            self._log(f"could not capture tax app home area screenshot error={exc}")
            return None
        try:
            raw_text = self._recognize_portal_area_text_from_image(image_path)
        except Exception as exc:  # noqa: BLE001
            self._log(f"could not OCR tax app home area screenshot error={exc}")
            return None
        candidate = self._candidate_portal_area_text(raw_text)
        if candidate:
            self._log(f"read tax app home area via OCR text={candidate}")
        return candidate

    def _capture_home_portal_area_screenshot(self, bundle_id: str) -> Path:
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine 电子税务局 window bounds for area screenshot.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        x = int(left + width * HOME_PORTAL_AREA_SCREENSHOT_X_RATIO)
        y = int(top + height * HOME_PORTAL_AREA_SCREENSHOT_Y_RATIO)
        screenshot_width = max(int(width * HOME_PORTAL_AREA_SCREENSHOT_WIDTH_RATIO), 40)
        screenshot_height = max(int(height * HOME_PORTAL_AREA_SCREENSHOT_HEIGHT_RATIO), 30)
        target_dir = Path(mkdtemp(prefix="portal-home-area-"))
        target = target_dir / "home-area.png"
        self._run_command(
            [
                "screencapture",
                "-x",
                "-R",
                f"{x},{y},{screenshot_width},{screenshot_height}",
                str(target),
            ],
            timeout_seconds=UI_ACTION_TIMEOUT_SECONDS,
        )
        if not target.exists():
            raise PortalLocalLoginError("Home area screenshot was not created.")
        return target

    def _recognize_portal_area_text_from_image(self, image_path: Path) -> str:
        client = self._get_vision_client()
        prompt = (
            "读取这张截图中显示的地区名称，只返回纯文本地区名。"
            "例如：厦门、福建、福建省、泉州。不要输出解释。"
        )
        raw = image_path.read_bytes()
        response = client._chat_completion(
            base64.b64encode(raw).decode("ascii"),
            image_path.name,
            prompt,
        )
        return response.strip().splitlines()[0].strip()

    def _get_vision_client(self) -> OpenAICompatibleVisionClient:
        if self._vision_client is None:
            self._vision_client = OpenAICompatibleVisionClient(
                self.config.openai_base_url,
                self.config.openai_api_key,
                self.config.openai_model,
                timeout_seconds=self.config.openai_timeout_seconds,
                ssl_verify=self.config.openai_ssl_verify,
                ca_bundle_path=self.config.openai_ca_bundle_path,
            )
        return self._vision_client

    def _switch_home_portal_area(self, bundle_id: str) -> None:
        self._click_named_element(bundle_id, ("身份切换",), timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
        self._wait_for_named_text(bundle_id, ("全国",), timeout_seconds=PORTAL_AREA_SWITCH_PAGE_TIMEOUT_SECONDS)
        self._click_named_element(bundle_id, ("全国",), timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
        self._wait_for_named_text(
            bundle_id,
            (self.portal_area_name,),
            timeout_seconds=PORTAL_AREA_SWITCH_PAGE_TIMEOUT_SECONDS,
        )
        self._click_named_element(
            bundle_id,
            (self.portal_area_name,),
            timeout_seconds=UI_ACTION_TIMEOUT_SECONDS,
        )
        self._wait_for_named_text(
            bundle_id,
            (self.portal_company_switch_name,),
            timeout_seconds=PORTAL_AREA_SWITCH_PAGE_TIMEOUT_SECONDS,
            contains=True,
        )
        self._click_company_switch_button(bundle_id, self.portal_company_switch_name)
        sleep(PORTAL_AREA_SWITCH_SETTLE_SECONDS)
        self._complete_area_switch_role_selection(bundle_id)

    def _complete_area_switch_role_selection(self, bundle_id: str) -> None:
        state = self._wait_for_post_login_state(bundle_id, timeout_seconds=POST_LOGIN_STATE_TIMEOUT_SECONDS)
        if state == "home":
            return
        if state == "switch_success_dialog":
            self._confirm_switch_success_dialog(bundle_id)
            self._log("confirmed switch success dialog after area/company switch")
            return
        if state != "role_dialog":
            raise PortalLocalLoginError(
                "电子税务局 app did not reach role selection after area/company switch "
                f"state={state}"
            )
        state = self._confirm_role_selection(bundle_id)
        if state == "home":
            self._log("area/company switch role selection entered logged-in home directly")
            return
        if state == "switch_success_dialog":
            self._confirm_switch_success_dialog(bundle_id)
            self._log("confirmed switch success dialog after area/company switch role selection")
            return
        if state != "fingerprint_prompt":
            raise PortalLocalLoginError(
                "电子税务局 app did not reach fingerprint quick-login prompt after area/company switch role selection "
                f"state={state}"
            )
        self._dismiss_fingerprint_prompt(bundle_id)
        self._log("dismissed fingerprint quick login prompt after area/company switch")
        state = self._wait_for_post_login_state(bundle_id, timeout_seconds=POST_LOGIN_STATE_TIMEOUT_SECONDS)
        if state == "switch_success_dialog":
            self._confirm_switch_success_dialog(bundle_id)
            self._log("confirmed switch success dialog after area/company switch fingerprint prompt")
            return
        if state == "login_page":
            self._log(
                "fingerprint prompt dismissed after area/company switch but home markers not yet visible; continuing"
            )
            return
        if state != "home":
            raise PortalLocalLoginError(
                "电子税务局 app did not reach logged-in home after area/company switch fingerprint prompt "
                f"state={state}"
            )

    @staticmethod
    def _portal_area_text_matches_target(current_area: str, target_area_name: str) -> bool:
        normalized_current = PortalMacLoginAutomator._normalized_area_text(current_area)
        normalized_target = PortalMacLoginAutomator._normalized_area_text(target_area_name)
        if not normalized_current or not normalized_target:
            return False
        return normalized_current in normalized_target or normalized_target in normalized_current

    @staticmethod
    def _normalized_area_text(text: str) -> str:
        normalized = PortalMacLoginAutomator._normalized_text(text)
        normalized = normalized.replace("电子税务局", "").replace("税务局", "")
        return normalized.strip()

    def _candidate_portal_area_text(self, text: str) -> str | None:
        candidate = self._normalized_area_text(text)
        if not candidate or candidate in PORTAL_AREA_TEXT_IGNORE:
            return None
        if not PORTAL_AREA_TEXT_REGEX.fullmatch(candidate):
            return None
        return candidate

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

    def _confirm_switch_success_dialog(self, bundle_id: str) -> None:
        self._activate_application(bundle_id)
        self._click_switch_success_dialog_confirm_relative(bundle_id)
        deadline = monotonic() + 5.0
        while monotonic() < deadline:
            try:
                texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
            except PortalLocalLoginError:
                return
            if not self._texts_show_switch_success_dialog(texts):
                return
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        raise PortalLocalLoginError("Timed out dismissing area/company switch success dialog.")

    def _click_switch_success_dialog_confirm_relative(self, bundle_id: str) -> None:
        self._activate_application(bundle_id)
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError("Unable to determine switch success dialog position.")
            left, top, width, height = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, top, width, height = bounds
        self._click_at_for_bundle(
            bundle_id,
            left + width * SWITCH_SUCCESS_DIALOG_CONFIRM_X_RATIO,
            top + height * SWITCH_SUCCESS_DIALOG_CONFIRM_Y_RATIO,
        )

    def _wait_for_post_login_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
            if self._texts_show_switch_success_dialog(texts):
                return "switch_success_dialog"
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
    def _texts_show_switch_success_dialog(texts: list[str]) -> bool:
        return any("切换成功" in text for text in texts)

    @staticmethod
    def _texts_show_logged_in_home(texts: list[str]) -> bool:
        indicators = ("功能名称", "申报期截止至", "身份切换")
        return any(any(indicator in text for indicator in indicators) for text in texts) and not any(
            "立即登录" in text for text in texts
        )

    def _try_fill_otp_from_system_prompt(self, bundle_id: str) -> bool:
        self._log("waiting for macOS one-time-code prompt")
        deadline = monotonic() + OTP_AUTOFILL_TIMEOUT_SECONDS
        expected_issuer = self._expected_tax_verification_issuer()
        while monotonic() < deadline:
            if self._otp_field_has_code(bundle_id):
                return True
            visible_texts = self._collect_visible_texts(bundle_id, timeout_seconds=3.0)
            code = self._extract_first_sms_code(visible_texts, expected_issuer=expected_issuer)
            if code and self._maybe_click_named_element(bundle_id, (code,), timeout_seconds=2.0, contains=True):
                if self._otp_field_has_code(bundle_id):
                    return True
            sleep(0.5)
        return False

    def _read_latest_sms_code_from_messages(self) -> str:
        expected_issuer = self._expected_tax_verification_issuer()
        if expected_issuer:
            self._log(f"reading latest {expected_issuer} verification code from Messages app")
        else:
            self._log("reading latest tax verification code from Messages app")
        self._activate_application(MESSAGES_BUNDLE_ID)
        self._wait_for_process(MESSAGES_BUNDLE_ID, timeout_seconds=UI_ACTION_TIMEOUT_SECONDS)
        deadline = monotonic() + OTP_MESSAGES_FALLBACK_TIMEOUT_SECONDS
        while monotonic() < deadline:
            visible_texts = self._collect_visible_texts(MESSAGES_BUNDLE_ID, timeout_seconds=3.0)
            code = self._extract_first_sms_code(visible_texts, expected_issuer=expected_issuer)
            if code:
                return code
            sleep(0.5)
        if expected_issuer:
            raise PortalLocalLoginError(
                f"Timed out waiting for {expected_issuer} verification code in Messages app."
            )
        raise PortalLocalLoginError("Timed out waiting for tax verification code in Messages app.")

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

    def _expected_tax_verification_issuer(self) -> str | None:
        if not self.portal_area_name:
            return None
        return f"{self.portal_area_name}税务"

    @staticmethod
    def _extract_first_sms_code(
        texts: Iterable[str],
        *,
        expected_issuer: str | None = None,
    ) -> str | None:
        if expected_issuer:
            for text in texts:
                match = OTP_TAX_VERIFICATION_REGEX.search(text)
                if match and match.group("issuer") == expected_issuer:
                    return match.group("code")
        for text in texts:
            match = OTP_TAX_VERIFICATION_REGEX.search(text)
            if match:
                return match.group("code")
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

    def _wait_for_named_text(
        self,
        bundle_id: str,
        names: tuple[str, ...],
        *,
        timeout_seconds: float,
        contains: bool = False,
    ) -> str:
        normalized_names = [self._normalized_text(name) for name in names if self._normalized_text(name)]
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            texts = self._collect_visible_texts(bundle_id, timeout_seconds=1.0)
            for text in texts:
                normalized_text = self._normalized_text(text)
                for normalized_name in normalized_names:
                    if contains:
                        if normalized_name in normalized_text:
                            return text
                    elif normalized_text == normalized_name:
                        return text
            sleep(VISIBLE_ELEMENT_POLL_SECONDS)
        raise PortalLocalLoginError(f"Timed out waiting for text in bundle {bundle_id}: {names!r}")

    def _click_company_switch_button(self, bundle_id: str, company_name: str) -> None:
        company_node = self._find_named_node(bundle_id, (company_name,), contains=True)
        if company_node is None:
            raise PortalLocalLoginError(
                f"Could not find company row for area switch: {company_name}"
            )
        company_center = company_node.center
        if company_center is None:
            raise PortalLocalLoginError(
                f"Could not determine company row position for area switch: {company_name}"
            )
        company_x, company_y = company_center
        company_height = company_node.size[1] if company_node.size is not None else 40.0
        button_candidates: list[tuple[float, AXNode]] = []
        for node in self._nodes_for_bundle(bundle_id):
            if node is company_node or node.center is None:
                continue
            if not any(self._normalized_text(text) == "切换" for text in node.texts):
                continue
            button_x, button_y = node.center
            if button_x <= company_x:
                continue
            if abs(button_y - company_y) > max(company_height, 40.0):
                continue
            button_candidates.append((button_x, node))
        if button_candidates:
            button_candidates.sort(key=lambda item: item[0])
            if self._click_node_for_bundle(bundle_id, button_candidates[0][1]):
                return
        bounds = self._window_bounds_for_bundle(bundle_id)
        if bounds is None:
            window = self._window_node(bundle_id)
            if window is None or window.position is None or window.size is None:
                raise PortalLocalLoginError(
                    f"Unable to determine switch button position for company: {company_name}"
                )
            left, _, width, _ = (
                window.position[0],
                window.position[1],
                window.size[0],
                window.size[1],
            )
        else:
            left, _, width, _ = bounds
        self._click_at_for_bundle(bundle_id, left + width * 0.88, company_y)

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

    def _log(self, message: str) -> None:
        self._logger(self.store_key, message)
