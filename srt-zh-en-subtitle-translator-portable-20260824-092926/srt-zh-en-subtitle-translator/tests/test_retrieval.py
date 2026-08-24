from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from tests.support import (
    STYLE,
    minimal_srt,
    prepare_source,
    read_json,
    write_json,
    write_text,
)


def retrieval_for(cue: dict) -> dict:
    value = cue.get("retrieval")
    if isinstance(value, dict):
        return value
    # Fail clearly when a v1 artifact is accidentally emitted.
    raise AssertionError(f"missing v2 retrieval object: {cue}")


def candidate_id(candidate: dict) -> str:
    for key in ("id", "record_id", "memory_id", "example_id"):
        if candidate.get(key):
            return str(candidate[key])
    raise AssertionError(f"retrieval candidate has no stable ID: {candidate}")


class RetrievalContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.empty_glossary = self.work / "empty-glossary.json"
        write_json(
            self.empty_glossary,
            {
                "schema_version": "subtitle.glossary.v2",
                "termbase_id": "test-empty",
                "version": "2.0.0",
                "locale": {"source": "zh-CN", "target": "en"},
                "entries": [],
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_unrelated_query_abstains_instead_of_returning_weak_memory(self) -> None:
        source = self.work / "unrelated.srt"
        memory = self.work / "memory.json"
        prepared_path = self.work / "prepared.json"
        write_text(source, minimal_srt(["这是完全不同的财务话题"]))
        write_json(
            memory,
            {
                "examples": [
                    {
                        "id": "factory-tour",
                        "zh": "带大家参观PU车间",
                        "draft_en": "Let me take you into the PU workshop",
                        "final_en": "Let me show you around our PU workshop",
                    }
                ]
            },
        )
        prepared = prepare_source(
            source,
            prepared_path,
            preferences=memory,
            glossary=self.empty_glossary,
            min_score=0.95,
        )
        retrieval = retrieval_for(prepared["cues"][0])
        self.assertEqual(retrieval["status"], "abstained")
        self.assertEqual(retrieval["candidates"], [])
        self.assertTrue(retrieval.get("reason"))

    def test_approved_glossary_entry_is_matched_with_stable_term_id(self) -> None:
        source = self.work / "glossary.srt"
        memory = self.work / "empty-memory.json"
        glossary = self.work / "glossary.json"
        write_text(source, minimal_srt(["PU工序是家具行业的苦工位"]))
        write_json(memory, {"examples": []})
        write_json(
            glossary,
            {
                "schema_version": "subtitle.glossary.v2",
                "termbase_id": "purefaith-test",
                "version": "2.0.0",
                "locale": {"source": "zh-CN", "target": "en"},
                "entries": [
                    {
                        "term_id": "term-pu-production",
                        "source": {
                            "canonical": "PU工序",
                            "aliases": ["PU生产"],
                        },
                        "target": {
                            "preferred": "PU production",
                            "allowed": [],
                            "forbidden": [],
                        },
                        "domain": ["furniture-manufacturing"],
                        "status": "approved",
                        "evidence": ["human-review:test"],
                        "approval": {"state": "approved", "actor_id": "reviewer-test"},
                    }
                ],
            },
        )
        prepared = prepare_source(
            source,
            self.work / "prepared.json",
            preferences=memory,
            glossary=glossary,
        )
        matches = prepared["cues"][0].get("glossary_matches", [])
        self.assertTrue(matches, prepared["cues"][0])
        self.assertEqual(
            matches[0].get("term_id", matches[0].get("id")),
            "term-pu-production",
        )
        self.assertEqual(matches[0]["preferred_en"], "PU production")

    def test_equal_score_candidate_order_is_stable_when_memory_file_is_reordered(self) -> None:
        source = self.work / "source.srt"
        write_text(source, minimal_srt(["带大家参观工厂"]))
        examples = [
            {
                "id": "memory-b",
                "zh": "带大家参观工厂",
                "draft_en": "Let me take you around the factory",
                "final_en": "Let me show you around the factory",
            },
            {
                "id": "memory-a",
                "zh": "带大家参观工厂",
                "draft_en": "Come visit the factory",
                "final_en": "Let me show you around our factory",
            },
        ]
        candidate_orders: list[list[str]] = []
        for run_number, ordered_examples in enumerate((examples, list(reversed(examples)))):
            memory = self.work / f"memory-{run_number}.json"
            write_json(memory, {"examples": ordered_examples})
            prepared = prepare_source(
                source,
                self.work / f"prepared-{run_number}.json",
                preferences=memory,
                glossary=self.empty_glossary,
                min_score=0.1,
            )
            candidates = retrieval_for(prepared["cues"][0])["candidates"]
            candidate_orders.append([candidate_id(candidate) for candidate in candidates])

        self.assertEqual(candidate_orders[0], candidate_orders[1])
        self.assertEqual(set(candidate_orders[0]), {"memory-a", "memory-b"})

    def test_prepare_semantics_are_stable_ignoring_observation_time(self) -> None:
        source = self.work / "source.srt"
        memory = self.work / "memory.json"
        write_text(source, minimal_srt(["带大家参观工厂"]))
        write_json(
            memory,
            {
                "examples": [
                    {
                        "id": "memory-a",
                        "zh": "带大家参观工厂",
                        "draft_en": "Visit the factory",
                        "final_en": "Let me show you around our factory",
                    }
                ]
            },
        )
        first = self.work / "first.json"
        second = self.work / "second.json"
        prepare_source(
            source,
            first,
            preferences=memory,
            glossary=self.empty_glossary,
        )
        prepare_source(
            source,
            second,
            preferences=memory,
            glossary=self.empty_glossary,
        )
        first_payload = read_json(first)
        second_payload = read_json(second)
        first_payload.pop("created_at", None)
        second_payload.pop("created_at", None)
        self.assertEqual(first_payload, second_payload)

    def test_preferred_phrase_match_modes_and_scope_are_enforced(self) -> None:
        source = self.work / "source.srt"
        memory = self.work / "memory.json"
        style = self.work / "style.json"
        write_text(source, minimal_srt(["甲", "目标", "乙"]))
        write_json(memory, {"examples": []})
        write_json(
            style,
            {
                "remove_terminal_punctuation": True,
                "invalid_revised_values": [],
                "translation_principles": [],
                "preferred_phrases": [
                    {
                        "rule_id": "all-hit",
                        "priority": 10,
                        "match_mode": "all_in_window",
                        "zh_contains": ["甲", "乙"],
                        "preferred_en": "all",
                        "context_scope": {"cue_window": 3, "domains": ["test"]},
                    },
                    {
                        "rule_id": "all-miss",
                        "priority": 20,
                        "match_mode": "all_in_window",
                        "zh_contains": ["甲", "丙"],
                        "preferred_en": "miss",
                        "context_scope": {"cue_window": 3},
                    },
                    {
                        "rule_id": "exact-neighbor-must-not-hit",
                        "priority": 30,
                        "match_mode": "exact_or_contained",
                        "zh_contains": ["甲"],
                        "preferred_en": "wrong",
                        "context_scope": {"cue_window": 3},
                    },
                ],
            },
        )
        prepared = prepare_source(
            source,
            self.work / "prepared.json",
            preferences=memory,
            glossary=self.empty_glossary,
            style=style,
        )
        matches = prepared["cues"][1]["preferred_phrase_matches"]
        self.assertEqual([item["id"] for item in matches], ["all-hit"])
        self.assertEqual(
            matches[0]["applicability"],
            "lexical_match_requires_scope_confirmation",
        )


if __name__ == "__main__":
    unittest.main()
