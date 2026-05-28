from __future__ import annotations

import csv
from io import StringIO

from app.models import RawSurveyRecord


def _find_column_name(fieldnames: list[str], *keywords: str) -> str | None:
    for field in fieldnames:
        if field is None:
            continue
        normalized = str(field).strip()
        if normalized and all(keyword in normalized for keyword in keywords):
            return normalized
    return None


def parse_survey_csv(csv_text: str) -> list[RawSurveyRecord]:
    reader = csv.DictReader(StringIO(csv_text))
    fieldnames = [field for field in (reader.fieldnames or []) if field]
    title_key = _find_column_name(fieldnames, "发票抬头")
    tax_key = _find_column_name(fieldnames, "税号")
    email_key = _find_column_name(fieldnames, "邮箱")
    attachment_key = _find_column_name(fieldnames, "上传", "截图")
    phone_key = _find_column_name(fieldnames, "手机号")
    remark_key = _find_column_name(fieldnames, "备注")
    records: list[RawSurveyRecord] = []
    for row in reader:
        submission_id_text = (row.get("编号") or "").strip()
        if not submission_id_text:
            continue
        records.append(
            RawSurveyRecord(
                submission_id=int(submission_id_text),
                start_time=(row.get("开始答题时间") or "").strip(),
                end_time=(row.get("结束答题时间") or "").strip(),
                duration_seconds=(row.get("答题时长") or "").strip(),
                invoice_title=(row.get(title_key or "") or "").strip(),
                tax_id_raw=(row.get(tax_key or "") or "").strip(),
                email=(row.get(email_key or "") or "").strip(),
                attachment_name=(row.get(attachment_key or "") or "").strip(),
                phone=(row.get(phone_key or "") or "").strip(),
                remark=(row.get(remark_key or "") or "").strip(),
                raw={key: (value or "").strip() for key, value in row.items()},
            )
        )
    return records


def select_new_records(records: list[RawSurveyRecord], last_processed_id: int) -> list[RawSurveyRecord]:
    selected = [record for record in records if record.submission_id > last_processed_id]
    return sorted(selected, key=lambda record: record.submission_id)
