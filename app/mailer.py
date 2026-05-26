from __future__ import annotations

import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.models import BatchRunSummary


class SummaryMailer:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        sender: str,
        recipients: list[str],
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.recipients = recipients

    def send_summary(self, summary: BatchRunSummary) -> None:
        message = EmailMessage()
        message["Subject"] = "腾讯问卷开票批处理汇总"
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message.set_content(self._build_body(summary))
        for result in summary.succeeded:
            if result.output_path and result.output_path.exists():
                self._attach_file(message, result.output_path)
        with smtplib.SMTP(self.host, self.port) as smtp:
            smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.send_message(message)

    @staticmethod
    def _attach_file(message: EmailMessage, path: Path) -> None:
        mime_type, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime_type or "application/octet-stream").split("/", 1)
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )

    @staticmethod
    def _build_body(summary: BatchRunSummary) -> str:
        lines = ["成功："]
        if summary.succeeded:
            for result in summary.succeeded:
                lines.append(
                    f"- {result.store_name} ({result.store_key}): 新增 {result.processed_count} 条，"
                    f"输出 {result.output_path}"
                )
        else:
            lines.append("- 无")

        lines.append("")
        lines.append("无新增：")
        if summary.no_new_data:
            for result in summary.no_new_data:
                lines.append(f"- {result.store_name} ({result.store_key}): 0 条新增")
        else:
            lines.append("- 无")

        lines.append("")
        lines.append("失败：")
        if summary.failed:
            for result in summary.failed:
                lines.append(
                    f"- {result.store_name} ({result.store_key}): {result.error or '未提供错误信息'}"
                )
        else:
            lines.append("- 无")

        lines.append("")
        lines.append("告警：")
        warning_results = summary.warning_results
        if warning_results:
            for result in warning_results:
                lines.append(f"- {result.store_name} ({result.store_key}):")
                for warning in result.warnings:
                    lines.append(f"  * {warning}")
        else:
            lines.append("- 无")
        return "\n".join(lines)

