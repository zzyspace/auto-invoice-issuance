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

    def test_import_qr_into_photos_avoids_foreground_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)
            scripts: list[str] = []

            with patch.object(
                automator,
                "_run_applescript",
                side_effect=lambda script, timeout_seconds: scripts.append(script) or "",
            ):
                with patch("app.portal_local_login.sleep", return_value=None):
                    automator._import_qr_into_photos(tmp_path / "login-qr.png")  # noqa: SLF001

        self.assertEqual(1, len(scripts))
        self.assertIn('tell application "Photos"', scripts[0])
        self.assertIn("import POSIX file", scripts[0])
        self.assertNotIn("activate", scripts[0])

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

            def _ensure_etax_session(self, bundle_id: str) -> None:
                events.append(f"session:{bundle_id}")

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
                "session:cn.gov.chinatax.gt4.app",
                "scan:cn.gov.chinatax.gt4.app",
                "album:cn.gov.chinatax.gt4.app",
                "confirm:cn.gov.chinatax.gt4.app",
            ],
            events,
        )

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
                return names[0] == "立即登录"

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

    def test_verify_gui_automation_prerequisites_raises_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            config = self._build_config(tmp_path)
            automator = PortalMacLoginAutomator(config, "fuzzy", "法定代表人", lambda *_: None)

            with patch.object(
                automator,
                "_run_applescript",
                side_effect=PortalLocalLoginError("execution error -10827"),
            ):
                with self.assertRaises(PortalLocalLoginError) as ctx:
                    automator._verify_gui_automation_prerequisites()  # noqa: SLF001

        self.assertIn("Python process that runs tax-portal", str(ctx.exception))

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
