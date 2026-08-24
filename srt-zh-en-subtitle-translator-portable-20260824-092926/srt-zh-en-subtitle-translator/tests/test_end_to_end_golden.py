from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.support import (
    FIXTURES,
    STYLE,
    prepare_source,
    read_json,
    run_cli,
    write_bound_translations,
)


class PurefaithGoldenFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_bound_translation_review_finalize_validate(self) -> None:
        source = FIXTURES / "golden" / "source.zh.srt"
        expected = FIXTURES / "golden" / "expected.en.srt"
        legacy_translations = json.loads(
            (FIXTURES / "golden" / "translations.legacy.json").read_text(
                encoding="utf-8"
            )
        )

        prepared_path = self.work / "prepared.json"
        prepared = prepare_source(source, prepared_path)
        self.assertEqual(prepared["cue_count"], 5)
        self.assertEqual([cue["index"] for cue in prepared["cues"]], [1, 2, 3, 4, 5])
        self.assertTrue(prepared["source"]["sha256"])
        self.assertTrue(prepared["source"]["cue_fingerprints"])

        bound_translations = self.work / "translations.v2.json"
        write_bound_translations(
            bound_translations,
            prepared,
            [(item["index"], item["text"]) for item in legacy_translations],
        )

        draft = self.work / "draft.en.srt"
        apply_report = self.work / "apply.report.json"
        run_cli(
            "apply",
            source,
            bound_translations,
            draft,
            "--style-profile",
            STYLE,
            "--require-source-binding",
            "--report",
            apply_report,
            check=True,
        )
        self.assertTrue(read_json(apply_report)["passed"])

        review = self.work / "review.csv"
        export_report = self.work / "export.report.json"
        run_cli(
            "export-review",
            source,
            bound_translations,
            review,
            "--style-profile",
            STYLE,
            "--require-source-binding",
            "--report",
            export_report,
            check=True,
        )

        audit_report = self.work / "audit.report.json"
        clean_review = self.work / "review.clean.csv"
        run_cli(
            "audit-review",
            review,
            audit_report,
            "--source",
            source,
            "--clean-output",
            clean_review,
            "--style-profile",
            STYLE,
            "--strict",
            check=True,
        )
        self.assertTrue(read_json(audit_report)["passed"])

        final = self.work / "final.en.srt"
        finalize_report = self.work / "finalize.report.json"
        run_cli(
            "finalize",
            source,
            clean_review,
            final,
            "--style-profile",
            STYLE,
            "--report",
            finalize_report,
            check=True,
        )
        self.assertTrue(read_json(finalize_report)["passed"])

        validation_report = self.work / "validate.report.json"
        run_cli(
            "validate",
            source,
            final,
            "--style-profile",
            STYLE,
            "--report",
            validation_report,
            check=True,
        )
        self.assertTrue(read_json(validation_report)["passed"])
        self.assertEqual(
            final.read_text(encoding="utf-8-sig"),
            expected.read_text(encoding="utf-8-sig"),
        )

        # The approved thought spanning cues 4-5 moves "300 square meters" from
        # source cue 5 into English cue 4. Passing this flow guards against
        # incorrect per-cue number-alignment checks.
        self.assertIn("300-sq-m", final.read_text(encoding="utf-8-sig"))

    def test_deliver_creates_verified_srt_and_bilingual_xlsx(self) -> None:
        source = FIXTURES / "golden" / "source.zh.srt"
        translations = json.loads(
            (FIXTURES / "golden" / "translations.legacy.json").read_text(
                encoding="utf-8"
            )
        )
        prepared = prepare_source(source, self.work / "prepared-deliver.json")
        bound = self.work / "translations-deliver.v2.json"
        write_bound_translations(
            bound,
            prepared,
            [(item["index"], item["text"]) for item in translations],
        )

        delivery_dir = self.work / "delivery"
        result = run_cli("deliver", source, bound, delivery_dir, check=True)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["passed"])
        self.assertTrue(payload["source_bound"])
        self.assertEqual(
            payload["review_columns"],
            ["序号", "中文", "英文", "英文修改版", "时间线"],
        )
        for filename in (
            "final.en.srt",
            "bilingual-review.xlsx",
            "apply-report.json",
            "review-export.json",
            "review-audit.json",
            "final-validation.json",
            "delivery-report.json",
        ):
            self.assertTrue((delivery_dir / filename).is_file(), filename)

        import openpyxl

        workbook = openpyxl.load_workbook(
            delivery_dir / "bilingual-review.xlsx",
            read_only=True,
        )
        try:
            worksheet = workbook["subtitle_review"]
            headers = [cell.value for cell in next(worksheet.iter_rows(max_row=1))]
            self.assertEqual(
                headers[:5],
                ["序号", "中文", "英文", "英文修改版", "时间线"],
            )
            self.assertEqual(worksheet.max_row, 6)
        finally:
            workbook.close()


if __name__ == "__main__":
    unittest.main()
