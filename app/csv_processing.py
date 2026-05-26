from __future__ import annotations

import csv
from io import StringIO

from app.models import RawSurveyRecord


def parse_survey_csv(csv_text: str) -> list[RawSurveyRecord]:
    reader = csv.DictReader(StringIO(csv_text))
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
                invoice_title=(row.get("1.发票抬头") or "").strip(),
                tax_id_raw=(row.get("2.税号") or "").strip(),
                email=(row.get("3.邮箱") or "").strip(),
                attachment_name=(row.get("4.上传结账单或付款截图") or "").strip(),
                phone=(row.get("5.手机号") or "").strip(),
                remark=(row.get("6.备注") or "").strip(),
                raw={key: (value or "").strip() for key, value in row.items()},
            )
        )
    return records


def select_new_records(records: list[RawSurveyRecord], last_processed_id: int) -> list[RawSurveyRecord]:
    selected = [record for record in records if record.submission_id > last_processed_id]
    return sorted(selected, key=lambda record: record.submission_id)

