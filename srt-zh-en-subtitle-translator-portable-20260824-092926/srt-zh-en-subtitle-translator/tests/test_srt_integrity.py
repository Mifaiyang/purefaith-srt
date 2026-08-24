from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.support import (
    FIXTURES,
    STYLE,
    minimal_srt,
    prepare_source,
    read_json,
    run_cli,
    write_bound_translations,
    write_json,
    write_text,
)


class StrictSrtIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_utf8_bom_and_crlf_survive_bound_round_trip(self) -> None:
        source = self.work / "bom-crlf.zh.srt"
        prepared_path = self.work / "prepared.json"
        translations = self.work / "translations.json"
        output = self.work / "output.en.srt"
        write_text(source, minimal_srt(["你好", "欢迎参观"]), newline="\r\n", bom=True)

        prepared = prepare_source(source, prepared_path)
        self.assertEqual(prepared["source"]["encoding"], "utf-8-sig")
        self.assertEqual(prepared["source"]["newline"], "\r\n")
        write_bound_translations(
            translations,
            prepared,
            [(1, "Hello"), (2, "Welcome to our factory")],
        )

        result = run_cli(
            "apply",
            source,
            translations,
            output,
            "--style-profile",
            STYLE,
            "--require-source-binding",
            check=True,
        )
        self.assertEqual(result.returncode, 0)
        raw = output.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_duplicate_source_ids_are_rejected(self) -> None:
        source = self.work / "duplicate.srt"
        write_text(
            source,
            "1\n00:00:00,000 --> 00:00:01,000\n甲\n\n"
            "1\n00:00:01,000 --> 00:00:02,000\n乙\n",
        )
        result = run_cli("prepare", source, self.work / "prepared.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate", (result.stderr + result.stdout).lower())

    def test_fractional_source_id_is_rejected(self) -> None:
        source = self.work / "fractional.srt"
        write_text(source, "1.5\n00:00:00,000 --> 00:00:01,000\n甲\n")
        result = run_cli("prepare", source, self.work / "prepared.json")
        self.assertNotEqual(result.returncode, 0)

    def test_invalid_and_reversed_timestamps_are_rejected(self) -> None:
        cases = {
            "range": "1\n00:00:61,000 --> 00:01:02,000\n甲\n",
            "reversed": "1\n00:00:02,000 --> 00:00:01,000\n甲\n",
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                source = self.work / f"{label}.srt"
                write_text(source, payload)
                result = run_cli("prepare", source, self.work / f"{label}.json")
                self.assertNotEqual(result.returncode, 0)

    def test_changed_source_hash_blocks_bound_apply(self) -> None:
        source = self.work / "source.srt"
        prepared_path = self.work / "prepared.json"
        translations_path = self.work / "translations.json"
        write_text(source, minimal_srt(["第一句", "第二句"]))
        prepared = prepare_source(source, prepared_path)
        envelope = write_bound_translations(
            translations_path,
            prepared,
            [(1, "First line"), (2, "Second line")],
        )
        envelope["source"]["sha256"] = "0" * 64
        write_json(translations_path, envelope)

        result = run_cli(
            "apply",
            source,
            translations_path,
            self.work / "out.srt",
            "--require-source-binding",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source", (result.stderr + result.stdout).lower())

    def test_changed_cue_fingerprint_blocks_bound_apply(self) -> None:
        source = self.work / "source.srt"
        write_text(source, minimal_srt(["第一句"]))
        prepared = prepare_source(source, self.work / "prepared.json")
        translations_path = self.work / "translations.json"
        envelope = write_bound_translations(
            translations_path,
            prepared,
            [(1, "First line")],
        )
        envelope["translations"][0]["source_fingerprint"] = "f" * 64
        write_json(translations_path, envelope)

        result = run_cli(
            "apply",
            source,
            translations_path,
            self.work / "out.srt",
            "--require-source-binding",
        )
        self.assertNotEqual(result.returncode, 0)

    def test_punctuation_only_translation_is_rejected_after_cleanup(self) -> None:
        source = self.work / "source.srt"
        write_text(source, minimal_srt(["第一句"]))
        prepared = prepare_source(source, self.work / "prepared.json")
        translations = self.work / "translations.json"
        write_bound_translations(translations, prepared, [(1, "!?")])

        result = run_cli(
            "apply",
            source,
            translations,
            self.work / "out.srt",
            "--require-source-binding",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.work / "out.srt").exists())

    def test_apply_strips_terminal_but_preserves_internal_punctuation(self) -> None:
        source = self.work / "source.srt"
        write_text(source, minimal_srt(["第一句"]))
        prepared = prepare_source(source, self.work / "prepared.json")
        translations = self.work / "translations.json"
        output = self.work / "out.srt"
        write_bound_translations(
            translations,
            prepared,
            [(1, "First, inspect the frame!")],
        )
        run_cli(
            "apply",
            source,
            translations,
            output,
            "--require-source-binding",
            check=True,
        )
        text = output.read_text(encoding="utf-8-sig")
        self.assertIn("First, inspect the frame", text)
        self.assertNotIn("frame!", text)
        run_cli("validate", source, output, "--style-profile", STYLE, check=True)

    def test_validate_rejects_terminal_punctuation_and_chinese_residue(self) -> None:
        source = self.work / "source.srt"
        write_text(source, minimal_srt(["第一句"]))
        cases = {
            "terminal": "English sentence.",
            "chinese": "English with 中文",
        }
        for label, translated_text in cases.items():
            with self.subTest(label=label):
                translated = self.work / f"{label}.srt"
                write_text(translated, minimal_srt([translated_text]))
                report = self.work / f"{label}.report.json"
                result = run_cli(
                    "validate",
                    source,
                    translated,
                    "--style-profile",
                    STYLE,
                    "--report",
                    report,
                )
                self.assertNotEqual(result.returncode, 0)
                if report.exists():
                    self.assertFalse(read_json(report)["passed"])

    def test_real_steel_timing_drift_is_rejected(self) -> None:
        source = FIXTURES / "negative" / "steel-source-cue-39.srt"
        translated = FIXTURES / "negative" / "steel-timing-drift-cue-39.en.srt"
        result = run_cli("validate", source, translated, "--style-profile", STYLE)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tim", (result.stderr + result.stdout).lower())

    def test_group_level_number_redistribution_remains_valid(self) -> None:
        source = FIXTURES / "golden" / "source.zh.srt"
        translations = FIXTURES / "golden" / "translations.legacy.json"
        output = self.work / "group-number.en.srt"
        # Source cue 5 contains 300 square meters; approved English carries it in
        # cue 4. A per-cue number check would incorrectly reject this valid group.
        run_cli("apply", source, translations, output, "--style-profile", STYLE, check=True)
        run_cli("validate", source, output, "--style-profile", STYLE, check=True)
        self.assertIn("300-sq-m", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
