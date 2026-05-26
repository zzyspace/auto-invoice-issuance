from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import load_store_configs


class StoreConfigTests(unittest.TestCase):
    def test_load_store_configs_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stores_path = tmp_path / "stores.yaml"
            stores_path.write_text(
                """
stores:
  - store_key: test_store
    store_name: 测试门店
    survey_id: "1001"
    output_xlsx_path: ./output/test.xlsx
    initial_last_processed_id: 307
    attachment_question_id: q-1
    enabled: true
""".strip(),
                encoding="utf-8",
            )
            stores = load_store_configs(stores_path)
            self.assertEqual(1, len(stores))
            self.assertEqual((tmp_path / "output" / "test.xlsx").resolve(), stores[0].output_xlsx_path)
            self.assertEqual("q-1", stores[0].effective_attachment_question_id("fallback"))

    def test_store_uses_default_question_id_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            stores_path = tmp_path / "stores.yaml"
            stores_path.write_text(
                """
stores:
  - store_key: test_store
    store_name: 测试门店
    survey_id: "1001"
    output_xlsx_path: ./output/test.xlsx
    initial_last_processed_id: 307
    enabled: true
""".strip(),
                encoding="utf-8",
            )
            stores = load_store_configs(stores_path)
            self.assertEqual("fallback", stores[0].effective_attachment_question_id("fallback"))

