from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import (
    GLOSSARY,
    MEMORY,
    SKILL_ROOT,
    STYLE,
    minimal_srt,
    prepare_source,
    read_csv,
    read_json,
    run_cli,
    write_bound_translations,
    write_csv,
    write_text,
)


SCRIPTS = SKILL_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from subtitle_core import memory, migration  # noqa: E402


class LearningAndReleaseTests(unittest.TestCase):
    FIXED_TIME = "2026-07-28T00:00:00Z"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        base = memory.create_release(
            self.state,
            [],
            source={"origin": "test-baseline"},
            policy_versions={"style": "2.0.0", "glossary": "1.0.0"},
            activate=True,
            actor_id="test-suite",
            created_at=self.FIXED_TIME,
        )
        self.base_release_id = base["release_id"]
        self.base_pointer = (self.state / memory.ACTIVE_POINTER).read_bytes()
        self.base_memory = (
            self.state
            / memory.RELEASES_DIR
            / self.base_release_id
            / "memory.json"
        ).read_bytes()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_candidate(self, *, english: str = "Take care of our employees."):
        return memory.build_candidate_record(
            "善待员工",
            english,
            rendered_cues=[{"cue_index": 1, "text": english}],
            source_span={
                "kind": "complete_thought",
                "document_id": "doc-test",
                "sentence_group_id": "group-1",
                "cue_indexes": [1],
            },
            classification={"brand": "PUREFAITH", "domains": ["corporate-values"]},
            provenance={
                "origin": "review_candidate",
                "source_sha256": "a" * 64,
                "review_artifact": "review.audit.json",
            },
            policy_versions={"style": "2.0.0", "glossary": "1.0.0"},
            created_at=self.FIXED_TIME,
        )

    def test_candidate_append_never_mutates_active_release(self) -> None:
        candidate = self.make_candidate()
        result = memory.append_candidates(self.state, [candidate])
        self.assertEqual(result["candidate_count"], 1)
        self.assertFalse(result["release_mutated"])
        self.assertEqual(
            (self.state / memory.ACTIVE_POINTER).read_bytes(),
            self.base_pointer,
        )
        self.assertEqual(
            (
                self.state
                / memory.RELEASES_DIR
                / self.base_release_id
                / "memory.json"
            ).read_bytes(),
            self.base_memory,
        )
        event = memory.load_candidate_events(self.state)[0]
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(event["record"]["approval"]["status"], "pending")
        self.assertFalse(event["record"]["retrieval"]["eligible"])
        self.assertTrue(event["record"]["target"]["canonical_text"].endswith("."))
        self.assertFalse(
            event["record"]["target"]["rendered_cues"][0]["text"].endswith(".")
        )

    def test_propose_learn_cli_appends_candidate_only(self) -> None:
        source = Path(self.temp.name) / "source.srt"
        prepared_path = Path(self.temp.name) / "prepared.json"
        translations = Path(self.temp.name) / "translations.json"
        review = Path(self.temp.name) / "review.csv"
        reviewed = Path(self.temp.name) / "reviewed.csv"
        audit_report = Path(self.temp.name) / "audit.json"
        clean = Path(self.temp.name) / "clean.csv"
        learn_report = Path(self.temp.name) / "learn.json"
        write_text(source, minimal_srt(["善待员工", "稳住品质"]))
        prepared = prepare_source(source, prepared_path)
        write_bound_translations(
            translations,
            prepared,
            [(1, "Treat employees well"), (2, "Maintain stable quality")],
        )
        run_cli(
            "export-review",
            source,
            translations,
            review,
            "--require-source-binding",
            check=True,
        )
        fields, rows = read_csv(review)
        rows[0]["英文修改版"] = "Take care of our employees"
        write_csv(reviewed, fields, rows)
        run_cli(
            "audit-review",
            reviewed,
            audit_report,
            "--source",
            source,
            "--clean-output",
            clean,
            "--strict",
            check=True,
        )
        pointer_before = (self.state / memory.ACTIVE_POINTER).read_bytes()
        base_before = (
            self.state
            / memory.RELEASES_DIR
            / self.base_release_id
            / "memory.json"
        ).read_bytes()

        result = run_cli(
            "propose-learn",
            clean,
            "--source",
            source,
            "--state-root",
            self.state,
            "--report",
            learn_report,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = read_json(learn_report)
        self.assertEqual(report["candidate_count"], 1)
        self.assertFalse(report["release_mutated"])
        self.assertEqual(
            (self.state / memory.ACTIVE_POINTER).read_bytes(),
            pointer_before,
        )
        self.assertEqual(
            (
                self.state
                / memory.RELEASES_DIR
                / self.base_release_id
                / "memory.json"
            ).read_bytes(),
            base_before,
        )
        events = memory.load_candidate_events(self.state)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["record"]["approval"]["status"], "pending")

    def test_candidate_dry_run_writes_nothing(self) -> None:
        before = sorted(path.relative_to(self.state) for path in self.state.rglob("*"))
        result = memory.append_candidates(
            self.state,
            [self.make_candidate()],
            dry_run=True,
        )
        after = sorted(path.relative_to(self.state) for path in self.state.rglob("*"))
        self.assertTrue(result["dry_run"])
        self.assertEqual(before, after)

    def test_unapproved_promotion_fails_before_any_release_or_pointer_write(self) -> None:
        candidate = self.make_candidate()
        memory.append_candidates(self.state, [candidate])
        release_dirs_before = sorted(
            path.name
            for path in (self.state / memory.RELEASES_DIR).iterdir()
            if path.is_dir()
        )
        pointer_before = (self.state / memory.ACTIVE_POINTER).read_bytes()

        with self.assertRaises(memory.MemoryApprovalError):
            memory.promote_candidates(
                self.state,
                [candidate["candidate_id"]],
                actor_id="test-suite",
                promoted_at="2026-07-28T00:01:00Z",
            )

        release_dirs_after = sorted(
            path.name
            for path in (self.state / memory.RELEASES_DIR).iterdir()
            if path.is_dir()
        )
        self.assertEqual(release_dirs_before, release_dirs_after)
        self.assertEqual(
            pointer_before,
            (self.state / memory.ACTIVE_POINTER).read_bytes(),
        )

    def test_approved_candidate_promotes_to_new_immutable_release(self) -> None:
        candidate = self.make_candidate()
        memory.append_candidates(self.state, [candidate])
        memory.append_review_decision(
            self.state,
            candidate["candidate_id"],
            decision="approved",
            reviewer_id="reviewer-1",
            rationale="Approved from final business review",
            created_at="2026-07-28T00:00:30Z",
        )
        promoted = memory.promote_candidates(
            self.state,
            [candidate["candidate_id"]],
            actor_id="release-manager",
            promoted_at="2026-07-28T00:01:00Z",
        )
        self.assertNotEqual(promoted["release_id"], self.base_release_id)
        self.assertEqual(
            memory.load_active_pointer(self.state)["release_id"],
            promoted["release_id"],
        )
        active_records = memory.load_memory_records(self.state)
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0]["approval"]["status"], "approved")
        self.assertTrue(active_records[0]["retrieval"]["eligible"])
        self.assertEqual(
            (
                self.state
                / memory.RELEASES_DIR
                / self.base_release_id
                / "memory.json"
            ).read_bytes(),
            self.base_memory,
        )

    def test_rollback_repoints_without_deleting_later_release(self) -> None:
        candidate = self.make_candidate()
        memory.append_candidates(self.state, [candidate])
        memory.append_review_decision(
            self.state,
            candidate["candidate_id"],
            decision="approved",
            reviewer_id="reviewer-1",
            rationale="Approved from final business review",
            created_at="2026-07-28T00:00:30Z",
        )
        promoted = memory.promote_candidates(
            self.state,
            [candidate["candidate_id"]],
            actor_id="release-manager",
            promoted_at="2026-07-28T00:01:00Z",
        )
        later_release = (
            self.state / memory.RELEASES_DIR / promoted["release_id"]
        )
        later_bytes = (later_release / "memory.json").read_bytes()

        result = memory.rollback_release(
            self.state,
            self.base_release_id,
            actor_id="release-manager",
            reason="regression-test",
        )
        self.assertFalse(result["no_op"])
        self.assertEqual(
            memory.load_active_pointer(self.state)["release_id"],
            self.base_release_id,
        )
        self.assertTrue(later_release.exists())
        self.assertEqual((later_release / "memory.json").read_bytes(), later_bytes)

    def test_atomic_pointer_failure_preserves_previous_pointer_and_cleans_temp(self) -> None:
        candidate = self.make_candidate()
        memory.append_candidates(self.state, [candidate])
        memory.append_review_decision(
            self.state,
            candidate["candidate_id"],
            decision="approved",
            reviewer_id="reviewer-1",
            rationale="Approved from final business review",
            created_at="2026-07-28T00:00:30Z",
        )
        promoted = memory.promote_candidates(
            self.state,
            [candidate["candidate_id"]],
            actor_id="release-manager",
            activate=False,
            promoted_at="2026-07-28T00:01:00Z",
        )
        pointer_before = (self.state / memory.ACTIVE_POINTER).read_bytes()

        with mock.patch.object(
            memory.os,
            "replace",
            side_effect=OSError("simulated atomic replace failure"),
        ):
            with self.assertRaises(OSError):
                memory.activate_release(
                    self.state,
                    promoted["release_id"],
                    reason="test-failure",
                    actor_id="release-manager",
                )

        self.assertEqual(
            pointer_before,
            (self.state / memory.ACTIVE_POINTER).read_bytes(),
        )
        self.assertEqual(
            list(self.state.glob(f".{memory.ACTIVE_POINTER.name}.tmp-*")),
            [],
        )

    def test_legacy_migration_is_idempotent_and_never_mutates_the_seed(self) -> None:
        legacy_before = MEMORY.read_bytes()
        first = migration.migrate_legacy_memory(
            MEMORY,
            self.state,
            style_profile_path=STYLE,
            termbase_path=GLOSSARY,
            activate=True,
            actor_id="migration-test",
            dry_run=False,
        )
        first_release_id = first["result"]["release_id"]
        first_release = memory.load_release(self.state, first_release_id)
        first_ids = [record["record_id"] for record in first_release["records"]]
        release_dirs_after_first = {
            path.name
            for path in (self.state / memory.RELEASES_DIR).iterdir()
            if path.is_dir()
        }

        second = migration.migrate_legacy_memory(
            MEMORY,
            self.state,
            style_profile_path=STYLE,
            termbase_path=GLOSSARY,
            activate=True,
            actor_id="migration-test",
            dry_run=False,
        )
        second_release_id = second["result"]["release_id"]
        second_release = memory.load_release(self.state, second_release_id)
        second_ids = [record["record_id"] for record in second_release["records"]]
        release_dirs_after_second = {
            path.name
            for path in (self.state / memory.RELEASES_DIR).iterdir()
            if path.is_dir()
        }

        self.assertEqual(first_release_id, second_release_id)
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(
            first_release["records_sha256"],
            second_release["records_sha256"],
        )
        self.assertEqual(release_dirs_after_first, release_dirs_after_second)
        self.assertEqual(MEMORY.read_bytes(), legacy_before)
        self.assertTrue(second["result"]["legacy_unchanged"])
        self.assertEqual(second["result"]["retrieval_eligible_count"], 0)


if __name__ == "__main__":
    unittest.main()
