from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import AppConfig
from app.portal_local_login import (
    AXNode,
    CG_EVENT_FLAG_MASK_COMMAND,
    KEY_A,
    KEY_DELETE,
    PortalLocalLoginError,
    PortalMacLoginAutomator,
    STARTUP_REMINDER_TITLE,
)


class PortalLocalLoginTests(unittest.TestCase):
    def _build_config(self, tmp_path: Path, **overrides: object) -> AppConfig:
        base_kwargs = dict(
            timezone="Asia/Shanghai",
            survey_cookie="cookie",
            survey_export_url="https://example.com/export",
            survey_export_method="POST",
            survey_export_body_template="",
            survey_export_download_url_path=None,
            survey_extra_headers={},
            default_attachment_question_id=None,
            openai_base_url="https://example.com/v1",
            openai_api_key="key",
            openai_model="model",
            smtp_host="smtp.example.com",
            smtp_port=587,
            smtp_username="user",
            smtp_password="pass",
            smtp_from="from@example.com",
            smtp_to=["to@example.com"],
            template_xlsx_path=tmp_path / "template.xlsx",
            state_db_path=tmp_path / "state.db",
            stores_config_path=tmp_path / "stores.yaml",
            backups_root=tmp_path / "backups",
            portal_browser_backend="chrome_cdp",
            portal_chrome_cdp_url="http://127.0.0.1:9222",
            portal_etax_app_username="demo-user",
            portal_etax_app_password="demo-pass",
            portal_etax_app_path=tmp_path / "电子税务局.app",
        )
        base_kwargs.update(overrides)
        return AppConfig(**base_kwargs)

    def test_is_enabled_requires_credentials_and_chrome_cdp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            enabled_config = self._build_config(tmp_path)
            disabled_backend = self._build_config(tmp_path, portal_browser_backend="playwright")
            missing_password = self._build_config(tmp_path, portal_etax_app_password=None)

            self.assertTrue(PortalMacLoginAutomator.is_enabled(enabled_config))
            self.assertFalse(PortalMacLoginAutomator.is_enabled(disabled_backend))
            self.assertFalse(PortalMacLoginAutomator.is_enabled(missing_password))

    def test_extract_first_sms_code_prefers_first_visible_match(self) -> None:
        texts = [
            "106575327612366 【厦门税务】您的验证码是：839952（有效期为5分钟）",
            "【厦门税务】您的验证码是：108571（有效期为5分钟）",
        ]

        code = PortalMacLoginAutomator._extract_first_sms_code(texts)  # noqa: SLF001

        self.assertEqual("839952", code)

    def test_extract_first_sms_code_prefers_expected_tax_issuer(self) -> None:
        texts = [
            "【厦门税务】您的验证码是：111111（有效期为5分钟）",
            "【泉州税务】您的验证码是：222222（有效期为5分钟）",
        ]

        code = PortalMacLoginAutomator._extract_first_sms_code(  # noqa: SLF001
            texts,
            expected_issuer="泉州税务",
        )

        self.assertEqual("222222", code)

    def test_request_sms_code_retries_until_countdown_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            events: list[str] = []
            countdown_results = iter([False, False, True])

            with patch.object(automator, "_click_named_element", side_effect=lambda *args, **kwargs: events.append("click")):
                with patch.object(
                    automator,
                    "_wait_for_sms_countdown",
                    side_effect=lambda *args, **kwargs: next(countdown_results),
                ):
                    with patch.object(automator, "_log", side_effect=lambda message: events.append(message)):
                        automator._request_sms_code("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(3, events.count("click"))
        self.assertIn("SMS verification code request accepted attempt=3", events)

    def test_request_sms_code_raises_after_retry_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            with patch.object(automator, "_click_named_element", return_value="获取验证码"):
                with patch.object(automator, "_wait_for_sms_countdown", return_value=False):
                    with self.assertRaises(PortalLocalLoginError) as ctx:
                        automator._request_sms_code("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertIn("countdown", str(ctx.exception))

    def test_dismiss_fingerprint_prompt_falls_back_to_relative_click(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            clicks: list[tuple[float, float]] = []

            class FakeAX:
                def click_at(self, x: float, y: float) -> None:
                    clicks.append((x, y))

            automator._ax = FakeAX()  # type: ignore[assignment]

            with patch.object(
                automator,
                "_click_named_element",
                side_effect=PortalLocalLoginError("missing 暂不设置"),
            ):
                with patch.object(automator, "_window_bounds_for_bundle", return_value=(10.0, 20.0, 300.0, 500.0)):
                    automator._dismiss_fingerprint_prompt("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual([(109.0, 365.0)], clicks)

    def test_dismiss_startup_reminder_falls_back_to_relative_click(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            clicks: list[tuple[float, float]] = []
            visible_states = iter([True, False])

            class FakeAX:
                def click_at(self, x: float, y: float) -> None:
                    clicks.append((x, y))

            automator._ax = FakeAX()  # type: ignore[assignment]

            with patch.object(automator, "_startup_reminder_visible", side_effect=lambda *args: next(visible_states)):
                with patch.object(automator, "_window_bounds_for_bundle", return_value=(10.0, 20.0, 300.0, 500.0)):
                    with patch("app.portal_local_login.sleep", return_value=None):
                        automator._dismiss_startup_reminder_if_present("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual([(160.0, 460.0)], clicks)

    def test_dismiss_startup_reminder_stops_after_first_click_when_ocr_reports_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            events: list[str] = []
            ocr_states = iter([True, False])

            with patch.object(automator, "_ocr_startup_reminder_visible", side_effect=lambda *args: next(ocr_states)):
                with patch.object(automator, "_click_startup_reminder_close", side_effect=lambda *args: events.append("click_close")):
                    with patch("app.portal_local_login.sleep", return_value=None):
                        automator._dismiss_startup_reminder_if_present("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(["click_close"], events)

    def test_dismiss_startup_reminder_runs_only_once_per_automation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            events: list[str] = []
            visible_states = iter([True, False])

            with patch.object(automator, "_startup_reminder_visible", side_effect=lambda *args: next(visible_states)):
                with patch.object(
                    automator,
                    "_click_startup_reminder_close",
                    side_effect=lambda *args: events.append("click_close"),
                ):
                    with patch("app.portal_local_login.sleep", return_value=None):
                        automator._dismiss_startup_reminder_if_present("cn.gov.chinatax.gt4.app")  # noqa: SLF001
                        automator._dismiss_startup_reminder_if_present("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(["click_close"], events)

    def test_startup_reminder_visible_falls_back_to_ax_text_when_ocr_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            with patch.object(automator, "_ocr_startup_reminder_visible", return_value=None):
                with patch.object(automator, "_collect_visible_texts", return_value=[STARTUP_REMINDER_TITLE]):
                    visible = automator._startup_reminder_visible("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertTrue(visible)

    def test_ocr_startup_reminder_visible_returns_false_when_model_says_no(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            fake_image = tmp_path / "startup-reminder.png"
            fake_image.write_bytes(b"png")

            with patch.object(automator, "_capture_startup_reminder_screenshot", return_value=fake_image):
                with patch.object(
                    automator,
                    "_recognize_startup_reminder_visibility_from_image",
                    return_value=(False, "NONE"),
                ):
                    visible = automator._ocr_startup_reminder_visible("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertFalse(visible)

    def test_recognize_startup_reminder_visibility_from_image_returns_true_for_exact_modal_title(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            fake_image = tmp_path / "startup-reminder.png"
            fake_image.write_bytes(b"png")

            class FakeVisionClient:
                def _chat_completion(self, image_base64: str, file_name: str, prompt: str) -> str:
                    self.image_base64 = image_base64
                    self.file_name = file_name
                    self.prompt = prompt
                    return f"{STARTUP_REMINDER_TITLE}\n"

            fake_client = FakeVisionClient()

            with patch.object(automator, "_get_vision_client", return_value=fake_client):
                visible, raw_response = automator._recognize_startup_reminder_visibility_from_image(fake_image)  # noqa: SLF001

        self.assertTrue(visible)
        self.assertEqual(STARTUP_REMINDER_TITLE, raw_response)

    def test_recognize_startup_reminder_visibility_from_image_returns_false_for_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            fake_image = tmp_path / "startup-reminder.png"
            fake_image.write_bytes(b"png")

            class FakeVisionClient:
                def _chat_completion(self, image_base64: str, file_name: str, prompt: str) -> str:
                    self.image_base64 = image_base64
                    self.file_name = file_name
                    self.prompt = prompt
                    return "NONE"

            fake_client = FakeVisionClient()

            with patch.object(automator, "_get_vision_client", return_value=fake_client):
                visible, raw_response = automator._recognize_startup_reminder_visibility_from_image(fake_image)  # noqa: SLF001

        self.assertFalse(visible)
        self.assertEqual("NONE", raw_response)

    def test_open_album_from_scan_page_retries_until_scan_page_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            events: list[str] = []
            visibility = iter([True, False])

        with patch.object(automator, "_click_scan_album_region", side_effect=lambda *args: events.append("click_region")):
            with patch.object(automator, "_is_scan_page_visible", side_effect=lambda *args: next(visibility)):
                with patch.object(automator, "_log", side_effect=lambda message: events.append(message)):
                    automator._open_album_from_scan_page("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertIn("opening album from scan page", events)
        self.assertIn("scan-page album entry opened attempt=2", events)

    def test_select_latest_qr_from_album_prefers_photos_picker_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            events: list[str] = []

            with patch.object(automator, "_is_photos_picker_visible", return_value=True):
                with patch.object(
                    automator,
                    "_select_latest_qr_in_photos_picker",
                    side_effect=lambda: events.append("photos_picker"),
                ):
                    automator._select_latest_qr_from_album("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(["photos_picker"], events)

    def test_select_latest_qr_from_album_prefers_internal_picker_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            events: list[str] = []

            with patch.object(automator, "_is_internal_photo_picker_visible", return_value=True):
                with patch.object(
                    automator,
                    "_select_latest_qr_in_internal_picker",
                    side_effect=lambda *args: events.append("internal_picker"),
                ):
                    automator._select_latest_qr_from_album("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(["internal_picker"], events)

    def test_click_node_for_etax_bundle_waits_before_click(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            node = AXNode(
                element=1,
                role="AXButton",
                subrole="",
                texts=("登录",),
                position=(10.0, 10.0),
                size=(100.0, 20.0),
            )
            events: list[str] = []

            class FakeAX:
                def click_node(self, target: AXNode) -> bool:
                    events.append(f"click:{target.texts[0]}")
                    return True

            automator._ax = FakeAX()  # type: ignore[assignment]

            with patch("app.portal_local_login.sleep", side_effect=lambda seconds: events.append(f"sleep:{seconds}")):
                clicked = automator._click_node_for_bundle("cn.gov.chinatax.gt4.app", node)  # noqa: SLF001

        self.assertTrue(clicked)
        self.assertEqual(["sleep:1.0", "click:登录"], events)

    def test_click_at_for_etax_bundle_waits_before_click(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            events: list[str] = []

            class FakeAX:
                def click_at(self, x: float, y: float) -> None:
                    events.append(f"click_at:{x}:{y}")

            automator._ax = FakeAX()  # type: ignore[assignment]

            with patch("app.portal_local_login.sleep", side_effect=lambda seconds: events.append(f"sleep:{seconds}")):
                automator._click_at_for_bundle("cn.gov.chinatax.gt4.app", 12.0, 34.0)  # noqa: SLF001

        self.assertEqual(["sleep:1.0", "click_at:12.0:34.0"], events)

    def test_click_node_for_photos_bundle_skips_etax_pre_click_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            node = AXNode(
                element=1,
                role="AXButton",
                subrole="",
                texts=("照片",),
                position=(10.0, 10.0),
                size=(100.0, 20.0),
            )
            events: list[str] = []

            class FakeAX:
                def click_node(self, target: AXNode) -> bool:
                    events.append(f"click:{target.texts[0]}")
                    return True

            automator._ax = FakeAX()  # type: ignore[assignment]

            with patch("app.portal_local_login.sleep", side_effect=lambda seconds: events.append(f"sleep:{seconds}")):
                clicked = automator._click_node_for_bundle("com.apple.Photos", node)  # noqa: SLF001

        self.assertTrue(clicked)
        self.assertEqual(["click:照片"], events)

    def test_import_qr_into_photos_uses_launch_services_open_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            commands: list[list[str]] = []

            with patch.object(
                automator,
                "_run_command",
                side_effect=lambda command, timeout_seconds: commands.append(command) or "",
            ):
                with patch("app.portal_local_login.sleep", return_value=None):
                    automator._import_qr_into_photos(tmp_path / "login-qr.png")  # noqa: SLF001

        self.assertEqual([["open", "-g", "-a", "Photos", str(tmp_path / "login-qr.png")]], commands)

    def test_capture_qr_code_retries_once_after_three_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            logs: list[str] = []
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda _store, message: logs.append(message))

            class FakeCandidate:
                def __init__(self, width: int, height: int) -> None:
                    self.width = width
                    self.height = height
                    self.screenshot_paths: list[str] = []

                def is_visible(self) -> bool:
                    return True

                def bounding_box(self) -> dict[str, int]:
                    return {"width": self.width, "height": self.height}

                def screenshot(self, *, path: str) -> None:
                    self.screenshot_paths.append(path)

            class FakeLocator:
                def __init__(self, candidates: list[FakeCandidate]) -> None:
                    self.candidates = candidates

                def count(self) -> int:
                    return len(self.candidates)

                def nth(self, index: int) -> FakeCandidate:
                    return self.candidates[index]

            class FakePage:
                def __init__(self, first_selector_attempts: list[list[FakeCandidate]]) -> None:
                    self.first_selector_attempts = first_selector_attempts
                    self.first_selector_calls = 0

                def locator(self, selector: str) -> FakeLocator:
                    if selector == ".qrcode canvas":
                        index = min(self.first_selector_calls, len(self.first_selector_attempts) - 1)
                        self.first_selector_calls += 1
                        return FakeLocator(self.first_selector_attempts[index])
                    return FakeLocator([])

            first_attempt_candidate = FakeCandidate(width=100, height=100)
            second_attempt_candidate = FakeCandidate(width=180, height=180)
            page = FakePage([[first_attempt_candidate], [second_attempt_candidate]])

            with patch("app.portal_local_login.sleep", return_value=None) as mocked_sleep:
                qr_path = automator._capture_qr_code(page, tmp_path)  # noqa: SLF001

        self.assertEqual(tmp_path / "login-qr.png", qr_path)
        self.assertEqual([str(tmp_path / "login-qr.png")], second_attempt_candidate.screenshot_paths)
        mocked_sleep.assert_called_once_with(3.0)
        self.assertIn("tax portal login QR not ready; waiting 3s before retry", logs)

    def test_capture_qr_code_raises_after_single_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            class FakeCandidate:
                def is_visible(self) -> bool:
                    return True

                def bounding_box(self) -> dict[str, int]:
                    return {"width": 80, "height": 80}

            class FakeLocator:
                def __init__(self, candidates: list[FakeCandidate]) -> None:
                    self.candidates = candidates

                def count(self) -> int:
                    return len(self.candidates)

                def nth(self, index: int) -> FakeCandidate:
                    return self.candidates[index]

            class FakePage:
                def __init__(self) -> None:
                    self.calls = 0

                def locator(self, selector: str) -> FakeLocator:
                    if selector == ".qrcode canvas":
                        self.calls += 1
                        return FakeLocator([FakeCandidate()])
                    return FakeLocator([])

            page = FakePage()

            with patch("app.portal_local_login.sleep", return_value=None) as mocked_sleep:
                with self.assertRaises(PortalLocalLoginError) as ctx:
                    automator._capture_qr_code(page, tmp_path)  # noqa: SLF001

        self.assertIn("visible QR element", str(ctx.exception))
        self.assertEqual(2, page.calls)
        mocked_sleep.assert_called_once_with(3.0)

    def test_wait_for_login_confirmation_ready_raises_on_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            with patch.object(automator, "_is_login_confirmation_visible", return_value=False):
                with self.assertRaises(PortalLocalLoginError) as ctx:
                    automator._wait_for_login_confirmation_ready("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertIn("login confirmation", str(ctx.exception))

    def test_automate_orders_steps(self) -> None:
        events: list[str] = []

        class FakeAutomator(PortalMacLoginAutomator):
            def _require_supported_environment(self) -> None:
                events.append("require")

            def _verify_gui_automation_prerequisites(self) -> None:
                events.append("verify_gui")

            def _capture_qr_code(self, page: object, artifacts_dir: Path | None) -> Path:
                events.append("capture_qr")
                return Path("/tmp/login-qr.png")

            def _import_qr_into_photos(self, qr_path: Path) -> None:
                events.append(f"import:{qr_path}")

            def _resolve_app_bundle_identifier(self) -> str:
                events.append("resolve_bundle")
                return "cn.gov.chinatax.gt4.app"

            def _launch_etax_app(self) -> None:
                events.append("launch")

            def _wait_for_process(self, bundle_id: str, *, timeout_seconds: float) -> None:
                events.append(f"wait:{bundle_id}")

            def _home_page_is_logged_in(self, bundle_id: str) -> bool:
                events.append(f"home_logged_in:{bundle_id}")
                return False

            def _ensure_etax_session(self, bundle_id: str) -> None:
                events.append(f"session:{bundle_id}")

            def _open_home_tab(self, bundle_id: str) -> None:
                events.append(f"home_tab:{bundle_id}")

            def _ensure_home_portal_area(self, bundle_id: str) -> None:
                events.append(f"home_area:{bundle_id}")

            def _open_scan_flow(self, bundle_id: str) -> None:
                events.append(f"scan:{bundle_id}")

            def _select_latest_qr_from_album(self, bundle_id: str) -> None:
                events.append(f"album:{bundle_id}")

            def _confirm_scan_login(self, bundle_id: str) -> None:
                events.append(f"confirm:{bundle_id}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = FakeAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            qr_path = automator.automate(object(), tmp_path)

        self.assertEqual(Path("/tmp/login-qr.png"), qr_path)
        self.assertEqual(
            [
                "require",
                "verify_gui",
                "capture_qr",
                "import:/tmp/login-qr.png",
                "resolve_bundle",
                "launch",
                "wait:cn.gov.chinatax.gt4.app",
                "home_logged_in:cn.gov.chinatax.gt4.app",
                "session:cn.gov.chinatax.gt4.app",
                "home_tab:cn.gov.chinatax.gt4.app",
                "home_area:cn.gov.chinatax.gt4.app",
                "scan:cn.gov.chinatax.gt4.app",
                "album:cn.gov.chinatax.gt4.app",
                "confirm:cn.gov.chinatax.gt4.app",
            ],
            events,
        )

    def test_automate_skips_my_page_session_check_when_home_already_logged_in(self) -> None:
        events: list[str] = []

        class FakeAutomator(PortalMacLoginAutomator):
            def _require_supported_environment(self) -> None:
                events.append("require")

            def _verify_gui_automation_prerequisites(self) -> None:
                events.append("verify_gui")

            def _capture_qr_code(self, page: object, artifacts_dir: Path | None) -> Path:
                events.append("capture_qr")
                return Path("/tmp/login-qr.png")

            def _import_qr_into_photos(self, qr_path: Path) -> None:
                events.append(f"import:{qr_path}")

            def _resolve_app_bundle_identifier(self) -> str:
                events.append("resolve_bundle")
                return "cn.gov.chinatax.gt4.app"

            def _launch_etax_app(self) -> None:
                events.append("launch")

            def _wait_for_process(self, bundle_id: str, *, timeout_seconds: float) -> None:
                events.append(f"wait:{bundle_id}")

            def _home_page_is_logged_in(self, bundle_id: str) -> bool:
                events.append(f"home_logged_in:{bundle_id}")
                return True

            def _ensure_etax_session(self, bundle_id: str) -> None:
                events.append(f"session:{bundle_id}")

            def _open_home_tab(self, bundle_id: str) -> None:
                events.append(f"home_tab:{bundle_id}")

            def _ensure_home_portal_area(self, bundle_id: str) -> None:
                events.append(f"home_area:{bundle_id}")

            def _open_scan_flow(self, bundle_id: str) -> None:
                events.append(f"scan:{bundle_id}")

            def _select_latest_qr_from_album(self, bundle_id: str) -> None:
                events.append(f"album:{bundle_id}")

            def _confirm_scan_login(self, bundle_id: str) -> None:
                events.append(f"confirm:{bundle_id}")

            def _log(self, message: str) -> None:
                events.append(f"log:{message}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = FakeAutomator(config, "fuzzy_qz", "法定代表人", lambda *_: None)

            qr_path = automator.automate(object(), tmp_path)

        self.assertEqual(Path("/tmp/login-qr.png"), qr_path)
        self.assertEqual(
            [
                "require",
                "verify_gui",
                "capture_qr",
                "import:/tmp/login-qr.png",
                "resolve_bundle",
                "launch",
                "wait:cn.gov.chinatax.gt4.app",
                "home_logged_in:cn.gov.chinatax.gt4.app",
                "log:电子税务局 首页已显示身份切换; 跳过我的页登录态确认",
                "home_tab:cn.gov.chinatax.gt4.app",
                "home_area:cn.gov.chinatax.gt4.app",
                "scan:cn.gov.chinatax.gt4.app",
                "album:cn.gov.chinatax.gt4.app",
                "confirm:cn.gov.chinatax.gt4.app",
            ],
            events,
        )

    def test_portal_area_text_matches_target_accepts_subset(self) -> None:
        self.assertTrue(
            PortalMacLoginAutomator._portal_area_text_matches_target("福建", "福建省")  # noqa: SLF001
        )
        self.assertTrue(
            PortalMacLoginAutomator._portal_area_text_matches_target("福建省", "福建省")  # noqa: SLF001
        )
        self.assertFalse(
            PortalMacLoginAutomator._portal_area_text_matches_target("厦门", "福建省")  # noqa: SLF001
        )

    def test_texts_show_logged_in_home_accepts_identity_switch_marker(self) -> None:
        self.assertTrue(
            PortalMacLoginAutomator._texts_show_logged_in_home(["身份切换", "首页"])  # noqa: SLF001
        )

    def test_home_page_is_logged_in_requires_identity_switch_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            nodes_without_identity_switch = [
                AXNode(
                    element=1,
                    role="AXStaticText",
                    subrole="",
                    texts=("功能名称",),
                    position=(10.0, 20.0),
                    size=(40.0, 20.0),
                )
            ]
            nodes_with_identity_switch = [
                AXNode(
                    element=2,
                    role="AXButton",
                    subrole="",
                    texts=("身份切换",),
                    position=(20.0, 40.0),
                    size=(60.0, 24.0),
                )
            ]

            with patch.object(automator, "_nodes_for_bundle", return_value=nodes_without_identity_switch):
                self.assertFalse(automator._home_page_is_logged_in("cn.gov.chinatax.gt4.app"))  # noqa: SLF001

            with patch.object(automator, "_nodes_for_bundle", return_value=nodes_with_identity_switch):
                self.assertTrue(automator._home_page_is_logged_in("cn.gov.chinatax.gt4.app"))  # noqa: SLF001

    def test_current_home_portal_area_text_falls_back_to_ocr_when_ax_candidates_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(
                config,
                "fuzzy",
                "法定代表人",
                lambda *_: None,
                portal_area_name="厦门市",
            )

            with patch.object(automator, "_nodes_for_bundle", return_value=[]):
                with patch.object(automator, "_window_bounds_for_bundle", return_value=(0.0, 0.0, 300.0, 500.0)):
                    with patch.object(automator, "_ocr_home_portal_area_text", return_value="厦门") as mocked_ocr:
                        area = automator._current_home_portal_area_text("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual("厦门", area)
        mocked_ocr.assert_called_once_with("cn.gov.chinatax.gt4.app")

    def test_ocr_home_portal_area_text_normalizes_model_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(
                config,
                "fuzzy",
                "法定代表人",
                lambda *_: None,
                portal_area_name="厦门市",
            )
            fake_image = tmp_path / "home-area.png"
            fake_image.write_bytes(b"png")

            with patch.object(automator, "_capture_home_portal_area_screenshot", return_value=fake_image):
                with patch.object(
                    automator,
                    "_recognize_portal_area_text_from_image",
                    return_value="厦门\n",
                ):
                    area = automator._ocr_home_portal_area_text("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual("厦门", area)

    def test_ensure_home_portal_area_skips_switch_when_current_area_matches_target_subset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(
                config,
                "fuzzy_qz",
                "法定代表人",
                lambda *_: None,
                portal_area_name="福建省",
                portal_company_switch_name="泉州市鲤城区浮几餐饮店（个体工商户）（待确认）",
            )

            with patch.object(automator, "_wait_for_home_portal_area", return_value="福建"):
                with patch.object(automator, "_switch_home_portal_area") as mocked_switch:
                    automator._ensure_home_portal_area("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        mocked_switch.assert_not_called()

    def test_ensure_home_portal_area_switches_when_current_area_mismatches_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(
                config,
                "fuzzy_qz",
                "法定代表人",
                lambda *_: None,
                portal_area_name="福建省",
                portal_company_switch_name="泉州市鲤城区浮几餐饮店（个体工商户）（待确认）",
            )

            with patch.object(automator, "_wait_for_home_portal_area", return_value="厦门"):
                with patch.object(automator, "_switch_home_portal_area") as mocked_switch:
                    automator._ensure_home_portal_area("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        mocked_switch.assert_called_once_with("cn.gov.chinatax.gt4.app")

    def test_switch_home_portal_area_clicks_identity_switch_nation_target_area_and_company(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(
                config,
                "fuzzy_qz",
                "法定代表人",
                lambda *_: None,
                portal_area_name="福建省",
                portal_company_switch_name="泉州市鲤城区浮几餐饮店（个体工商户）（待确认）",
            )
            events: list[tuple[str, tuple[str, ...], bool]] = []

            def fake_click_named_element(
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> str:
                _ = bundle_id, timeout_seconds
                events.append(("click", names, contains))
                return names[0]

            def fake_wait_for_named_text(
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> str:
                _ = bundle_id, timeout_seconds
                events.append(("wait", names, contains))
                return names[0]

            with patch.object(automator, "_click_named_element", side_effect=fake_click_named_element):
                with patch.object(automator, "_wait_for_named_text", side_effect=fake_wait_for_named_text):
                    with patch.object(
                        automator,
                        "_click_company_switch_button",
                        side_effect=lambda bundle_id, company_name: events.append(("switch", (company_name,), False)),
                    ):
                        with patch.object(
                            automator,
                            "_complete_area_switch_role_selection",
                            side_effect=lambda bundle_id: events.append(("role_flow", (bundle_id,), False)),
                        ):
                            with patch("app.portal_local_login.sleep", return_value=None):
                                automator._switch_home_portal_area("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(
            [
                ("click", ("身份切换",), False),
                ("wait", ("全国",), False),
                ("click", ("全国",), False),
                ("wait", ("福建省",), False),
                ("click", ("福建省",), False),
                ("wait", ("泉州市鲤城区浮几餐饮店（个体工商户）（待确认）",), True),
                ("switch", ("泉州市鲤城区浮几餐饮店（个体工商户）（待确认）",), False),
                ("role_flow", ("cn.gov.chinatax.gt4.app",), False),
            ],
            events,
        )

    def test_complete_area_switch_role_selection_confirms_role_dialog_and_returns_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy_qz", "法定代表人", lambda *_: None)
            events: list[str] = []
            states = iter(["role_dialog", "home"])

            with patch.object(
                automator,
                "_wait_for_post_login_state",
                side_effect=lambda bundle_id, *, timeout_seconds: next(states),
            ):
                with patch.object(
                    automator,
                    "_confirm_role_selection",
                    side_effect=lambda bundle_id, role_already_selected=False: events.append("confirm_role") or "home",
                ):
                    with patch.object(automator, "_log", side_effect=lambda message: events.append(f"log:{message}")):
                        automator._complete_area_switch_role_selection("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(
            [
                "confirm_role",
                "log:area/company switch role selection entered logged-in home directly",
            ],
            events,
        )

    def test_complete_area_switch_role_selection_confirms_switch_success_dialog_after_role_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy_qz", "法定代表人", lambda *_: None)
            events: list[str] = []

            with patch.object(
                automator,
                "_wait_for_post_login_state",
                return_value="role_dialog",
            ):
                with patch.object(
                    automator,
                    "_confirm_role_selection",
                    side_effect=lambda bundle_id, role_already_selected=False: events.append("confirm_role") or "switch_success_dialog",
                ):
                    with patch.object(
                        automator,
                        "_confirm_switch_success_dialog",
                        side_effect=lambda bundle_id: events.append("confirm_success_dialog"),
                    ):
                        with patch.object(automator, "_log", side_effect=lambda message: events.append(f"log:{message}")):
                            automator._complete_area_switch_role_selection("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(
            [
                "confirm_role",
                "confirm_success_dialog",
                "log:confirmed switch success dialog after area/company switch role selection",
            ],
            events,
        )

    def test_wait_for_post_login_state_prefers_switch_success_dialog_over_background_role_dialog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy_qz", "法定代表人", lambda *_: None)

            with patch.object(
                automator,
                "_collect_visible_texts",
                return_value=["请选择身份类型", "切换成功", "法定代表人"],
            ):
                state = automator._wait_for_post_login_state("cn.gov.chinatax.gt4.app", timeout_seconds=1.0)  # noqa: SLF001

        self.assertEqual("switch_success_dialog", state)

    def test_confirm_switch_success_dialog_uses_relative_click(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy_qz", "法定代表人", lambda *_: None)
            events: list[str] = []
            visible_texts = iter([["切换成功"], []])

            with patch.object(
                automator,
                "_click_switch_success_dialog_confirm_relative",
                side_effect=lambda bundle_id: events.append("relative_click"),
            ):
                with patch.object(
                    automator,
                    "_collect_visible_texts",
                    side_effect=lambda bundle_id, *, timeout_seconds: next(visible_texts),
                ):
                    with patch("app.portal_local_login.sleep", return_value=None):
                        automator._confirm_switch_success_dialog("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(["relative_click"], events)

    def test_wait_for_process_waits_for_accessibility_nodes_after_pid_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            logs: list[str] = []

            class FakeAX:
                def __init__(self) -> None:
                    self.find_nodes_calls = 0

                def find_nodes(self, pid: int) -> list[AXNode]:
                    self.find_nodes_calls += 1
                    if self.find_nodes_calls == 1:
                        return []
                    return [
                        AXNode(
                            element=1,
                            role="AXWindow",
                            subrole="",
                            texts=("首页",),
                            position=(10.0, 20.0),
                            size=(300.0, 500.0),
                        )
                    ]

            fake_ax = FakeAX()
            automator._ax = fake_ax  # type: ignore[assignment]

            with patch.object(automator, "_find_process_pids", return_value=[12345]):
                with patch("app.portal_local_login.sleep", return_value=None):
                    with patch.object(automator, "_log", side_effect=lambda message: logs.append(message)):
                        automator._wait_for_process("cn.gov.chinatax.gt4.app", timeout_seconds=1.0)  # noqa: SLF001

            self.assertEqual(2, fake_ax.find_nodes_calls)
            self.assertIn(
                "电子税务局 process detected but UI window is not ready yet; waiting before starting click automation",
                logs,
            )

    def test_activate_application_uses_etax_path_for_resolved_bundle_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            commands: list[list[str]] = []

            with patch.object(
                automator,
                "_run_command",
                side_effect=lambda command, timeout_seconds: commands.append(command) or "",
            ):
                automator._activate_application("com.vendor.actual-etax-bundle")  # noqa: SLF001

        self.assertEqual([["open", "-a", str(config.portal_etax_app_path)]], commands)

    def test_ensure_etax_session_uses_messages_fallback_when_prompt_fill_unavailable(self) -> None:
        events: list[str] = []

        class FakeAutomator(PortalMacLoginAutomator):
            post_login_states = iter(["role_dialog", "fingerprint_prompt", "home"])

            def _click_etax_tabbar_item(self, bundle_id: str, index: int) -> None:
                events.append(f"tab:{index}")

            def _focus_sms_code_input(self, bundle_id: str) -> None:
                events.append("focus_sms")

            def _request_sms_code(self, bundle_id: str) -> None:
                events.append("request_sms")

            def _wait_for_post_login_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                state = next(self.post_login_states)
                events.append(f"post_login:{state}")
                return state

            def _wait_for_etax_session_entry_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                events.append(f"entry_state:{timeout_seconds}")
                return "login_button"

            def _click_named_element(
                self,
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> str:
                events.append(f"click:{names[0]}")
                return names[0]

            def _maybe_click_named_element(
                self,
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> bool:
                events.append(f"maybe:{names[0]}")
                return names[0] in {"立即登录", "法定代表人", "暂不设置"}

            def _set_text_input_value(
                self,
                bundle_id: str,
                value: str,
                *,
                field_index: int,
                labels: tuple[str, ...] = (),
                secure: bool = False,
            ) -> None:
                events.append(f"set:{field_index}:{secure}:{labels or ('<none>',)}:{value}")

            def _focus_text_input(
                self,
                bundle_id: str,
                labels: tuple[str, ...],
                *,
                field_index: int,
                secure: bool = False,
            ) -> None:
                events.append(f"focus:{field_index}:{labels[0]}")

            def _try_fill_otp_from_system_prompt(self, bundle_id: str) -> bool:
                events.append("prompt:false")
                return False

            def _read_latest_sms_code_from_messages(self) -> str:
                events.append("messages:read")
                return "839952"

            def _log(self, message: str) -> None:
                events.append(f"log:{message}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = FakeAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            automator._ensure_etax_session("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertIn("prompt:false", events)
        self.assertIn("messages:read", events)
        self.assertIn("set:2:False:('短信验证码',):839952", events)

    def test_ensure_etax_session_dismisses_startup_reminder_before_tab_click(self) -> None:
        events: list[str] = []

        class FakeAutomator(PortalMacLoginAutomator):
            def _dismiss_startup_reminder_if_present(self, bundle_id: str) -> None:
                events.append("dismiss_startup")

            def _click_etax_tabbar_item(self, bundle_id: str, index: int) -> None:
                events.append(f"tab:{index}")

            def _maybe_click_named_element(
                self,
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> bool:
                events.append(f"maybe:{names[0]}")
                return False

            def _wait_for_etax_session_entry_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                events.append(f"entry_state:{timeout_seconds}")
                return "home"

            def _log(self, message: str) -> None:
                events.append(f"log:{message}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = FakeAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            automator._ensure_etax_session("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(
            [
                "log:navigating 电子税务局 app login flow",
                "dismiss_startup",
                "tab:4",
                "maybe:立即登录",
                "maybe:法定代表人",
                "entry_state:6.0",
                "log:电子税务局 我的页入口状态 state=home",
            ],
            events,
        )

    def test_ensure_etax_session_returns_immediately_when_my_page_is_already_logged_in_home(self) -> None:
        events: list[str] = []

        class FakeAutomator(PortalMacLoginAutomator):
            def _dismiss_startup_reminder_if_present(self, bundle_id: str) -> None:
                events.append("dismiss_startup")

            def _click_etax_tabbar_item(self, bundle_id: str, index: int) -> None:
                events.append(f"tab:{index}")

            def _maybe_click_named_element(
                self,
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> bool:
                events.append(f"maybe:{names[0]}")
                return False

            def _wait_for_etax_session_entry_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                events.append(f"entry_state:{timeout_seconds}")
                return "home"

            def _log(self, message: str) -> None:
                events.append(f"log:{message}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = FakeAutomator(config, "fuzzy_qz", "法定代表人", lambda *_: None)

            automator._ensure_etax_session("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertEqual(
            [
                "log:navigating 电子税务局 app login flow",
                "dismiss_startup",
                "tab:4",
                "maybe:立即登录",
                "maybe:法定代表人",
                "entry_state:6.0",
                "log:电子税务局 我的页入口状态 state=home",
            ],
            events,
        )

    def test_ensure_etax_session_skips_messages_fallback_when_prompt_fill_succeeds(self) -> None:
        events: list[str] = []

        class FakeAutomator(PortalMacLoginAutomator):
            post_login_states = iter(["role_dialog", "fingerprint_prompt", "home"])

            def _click_etax_tabbar_item(self, bundle_id: str, index: int) -> None:
                events.append(f"tab:{index}")

            def _focus_sms_code_input(self, bundle_id: str) -> None:
                events.append("focus_sms")

            def _request_sms_code(self, bundle_id: str) -> None:
                events.append("request_sms")

            def _wait_for_post_login_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                state = next(self.post_login_states)
                events.append(f"post_login:{state}")
                return state

            def _wait_for_etax_session_entry_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                events.append(f"entry_state:{timeout_seconds}")
                return "login_button"

            def _click_named_element(
                self,
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> str:
                events.append(f"click:{names[0]}")
                return names[0]

            def _set_text_input_value(
                self,
                bundle_id: str,
                value: str,
                *,
                field_index: int,
                labels: tuple[str, ...] = (),
                secure: bool = False,
            ) -> None:
                events.append(f"set:{field_index}:{secure}:{value}")

            def _focus_text_input(
                self,
                bundle_id: str,
                labels: tuple[str, ...],
                *,
                field_index: int,
                secure: bool = False,
            ) -> None:
                events.append("focus")

            def _try_fill_otp_from_system_prompt(self, bundle_id: str) -> bool:
                events.append("prompt:true")
                return True

            def _read_latest_sms_code_from_messages(self) -> str:
                raise AssertionError("Messages fallback should not be used when quick-type succeeds")

            def _log(self, message: str) -> None:
                events.append(f"log:{message}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = FakeAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            automator._ensure_etax_session("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertIn("prompt:true", events)

    def test_ensure_etax_session_continues_when_role_selection_is_skipped_after_sms_login(self) -> None:
        events: list[str] = []

        class FakeAutomator(PortalMacLoginAutomator):
            post_login_states = iter(["fingerprint_prompt", "home"])

            def _click_etax_tabbar_item(self, bundle_id: str, index: int) -> None:
                events.append(f"tab:{index}")

            def _focus_sms_code_input(self, bundle_id: str) -> None:
                events.append("focus_sms")

            def _request_sms_code(self, bundle_id: str) -> None:
                events.append("request_sms")

            def _wait_for_post_login_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                state = next(self.post_login_states)
                events.append(f"post_login:{state}")
                return state

            def _wait_for_etax_session_entry_state(self, bundle_id: str, *, timeout_seconds: float) -> str:
                events.append(f"entry_state:{timeout_seconds}")
                return "login_button"

            def _click_named_element(
                self,
                bundle_id: str,
                names: tuple[str, ...],
                *,
                timeout_seconds: float,
                contains: bool = False,
            ) -> str:
                events.append(f"click:{names[0]}")
                return names[0]

            def _set_text_input_value(
                self,
                bundle_id: str,
                value: str,
                *,
                field_index: int,
                labels: tuple[str, ...] = (),
                secure: bool = False,
            ) -> None:
                events.append(f"set:{field_index}:{secure}:{value}")

            def _try_fill_otp_from_system_prompt(self, bundle_id: str) -> bool:
                events.append("prompt:true")
                return True

            def _dismiss_fingerprint_prompt(self, bundle_id: str) -> None:
                events.append("dismiss_fingerprint")

            def _confirm_role_selection(self, bundle_id: str, *, role_already_selected: bool = False) -> str:
                raise AssertionError("role selection should be skipped when fingerprint prompt appears directly")

            def _log(self, message: str) -> None:
                events.append(f"log:{message}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = FakeAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            automator._ensure_etax_session("cn.gov.chinatax.gt4.app")  # noqa: SLF001

        self.assertIn("dismiss_fingerprint", events)
        self.assertIn(
            "log:role selection not shown after SMS login; continuing with fingerprint quick-login prompt",
            events,
        )

    def test_verify_gui_automation_prerequisites_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            with patch.object(automator._ax, "is_process_trusted", return_value=False):
                with self.assertRaises(PortalLocalLoginError) as ctx:
                    automator._verify_gui_automation_prerequisites()  # noqa: SLF001

        self.assertIn("Python process that runs tax-portal", str(ctx.exception))

    def test_verify_gui_automation_prerequisites_stops_after_accessibility_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            with patch.object(automator._ax, "is_process_trusted", return_value=True):
                with patch.object(automator, "_run_command") as mocked_run_command:
                    automator._verify_gui_automation_prerequisites()  # noqa: SLF001

        mocked_run_command.assert_not_called()

    def test_set_text_input_value_falls_back_to_keyboard_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            node = AXNode(
                element=1,
                role="AXTextField",
                subrole="",
                texts=("请输入",),
                position=(10.0, 10.0),
                size=(100.0, 20.0),
            )
            events: list[str] = []

            class FakeAX:
                def set_text_value(self, target: AXNode, value: str) -> bool:
                    events.append(f"set:{value}")
                    return False

                def click_node(self, target: AXNode) -> bool:
                    events.append("click")
                    return True

                def send_modified_key(self, key_code: int, flags: int) -> None:
                    events.append(f"modified:{key_code}:{flags}")

                def send_key(self, key_code: int) -> None:
                    events.append(f"key:{key_code}")

                def type_text(self, value: str) -> None:
                    events.append(f"type:{value}")

            automator._ax = FakeAX()  # type: ignore[assignment]

            with patch.object(automator, "_find_text_field_node", return_value=node):
                automator._set_text_input_value("cn.gov.chinatax.gt4.app", "demo-pass", field_index=1, secure=True)  # noqa: SLF001

        self.assertEqual(
            [
                "set:demo-pass",
                "click",
                f"modified:{KEY_A}:{CG_EVENT_FLAG_MASK_COMMAND}",
                f"key:{KEY_DELETE}",
                "type:demo-pass",
            ],
            events,
        )
