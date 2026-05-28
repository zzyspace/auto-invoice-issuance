from __future__ import annotations

import unittest

from app.csv_processing import parse_survey_csv, select_new_records
from app.utils import extract_attachment_download_url, looks_like_natural_person, normalize_tax_id


CSV_TEXT = """编号,开始答题时间,结束答题时间,答题时长,1.发票抬头,2.税号,3.邮箱,4.上传结账单或付款截图,5.手机号,6.备注
309,2026/5/26 12:00,2026/5/26 12:01,30,深圳易思商务咨询有限公司厦门分公司,,a@example.com,a.png,,
308,2026/5/26 11:00,2026/5/26 11:01,31,许心怡,无税号 个人抬头,b@example.com,b.png,,
307,2026/5/26 10:00,2026/5/26 10:01,32,吴翔,91350203MAK37WG54F,c@example.com,c.png,,
"""

CSV_TEXT_REORDERED = """编号,开始答题时间,结束答题时间,答题时长,1.发票抬头,2.税号,3.上传结账单或付款截图,4.邮箱,5.手机号,6.备注
309,2026/5/26 12:00,2026/5/26 12:01,30,深圳易思商务咨询有限公司厦门分公司,,a.png,a@example.com,13800000000,
"""


class CsvProcessingTests(unittest.TestCase):
    def test_select_new_records_sorts_incremental_records(self) -> None:
        records = parse_survey_csv(CSV_TEXT)
        selected = select_new_records(records, 307)
        self.assertEqual([308, 309], [record.submission_id for record in selected])

    def test_tax_id_normalization_ignores_chinese_placeholders(self) -> None:
        self.assertIsNone(normalize_tax_id("无税号 个人抬头"))
        self.assertEqual("AB1234567", normalize_tax_id("AB1234567"))

    def test_title_classification(self) -> None:
        self.assertTrue(looks_like_natural_person("吴翔"))
        self.assertFalse(looks_like_natural_person("深圳易思商务咨询有限公司厦门分公司"))

    def test_extract_attachment_download_url_from_excel_formula(self) -> None:
        formula = '=Hyperlink("https://wj.qq.com/api/files/download?survey_id=22512014&question_id=q-6-VwdW&file_name=abc.png&download=1"，"IMG_9057.png")'
        self.assertEqual(
            "https://wj.qq.com/api/files/download?survey_id=22512014&question_id=q-6-VwdW&file_name=abc.png&download=1",
            extract_attachment_download_url(formula),
        )

    def test_parse_survey_csv_with_reordered_attachment_and_email_columns(self) -> None:
        records = parse_survey_csv(CSV_TEXT_REORDERED)
        self.assertEqual(1, len(records))
        self.assertEqual("a.png", records[0].attachment_name)
        self.assertEqual("a@example.com", records[0].email)
