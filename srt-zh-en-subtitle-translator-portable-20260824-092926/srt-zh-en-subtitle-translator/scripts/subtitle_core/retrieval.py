"""Deterministic, explainable approved-example and glossary retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable

from .errors import ExitCode, SubtitleError
from .io_utils import read_json, stable_hash
from .text import clean_subtitle_text


ZH_NORMALIZE_RE = re.compile(r"[\s，。！？；：、,.!?;:'\"“”‘’（）()\-—~～]+")


def normalized_zh(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text))
    return ZH_NORMALIZE_RE.sub("", value)


def char_ngrams(text: str, n: int = 2) -> set[str]:
    value = normalized_zh(text)
    if not value:
        return set()
    if len(value) < n:
        return {value}
    return {value[position : position + n] for position in range(len(value) - n + 1)}


def similarity_components(query: str, candidate: str) -> dict[str, float]:
    q = normalized_zh(query)
    c = normalized_zh(candidate)
    if not q or not c:
        return {"sequence": 0.0, "ngram_jaccard": 0.0, "containment": 0.0}
    sequence = SequenceMatcher(None, q, c).ratio()
    qgrams = char_ngrams(q)
    cgrams = char_ngrams(c)
    union = qgrams | cgrams
    jaccard = len(qgrams & cgrams) / len(union) if union else 0.0
    containment = 1.0 if min(len(q), len(c)) >= 3 and (q in c or c in q) else 0.0
    return {
        "sequence": sequence,
        "ngram_jaccard": jaccard,
        "containment": containment,
    }


def combined_similarity(components: dict[str, float]) -> float:
    return (
        0.55 * components["sequence"]
        + 0.35 * components["ngram_jaccard"]
        + 0.10 * components["containment"]
    )


@dataclass(frozen=True)
class MemoryRecord:
    record_id: str
    zh: str
    final_en: str
    draft_en: str
    context_before: str
    context_after: str
    status: str
    provenance: dict[str, Any]


def _stable_record_id(raw: dict[str, Any], position: int) -> str:
    provided = raw.get("id") or raw.get("record_id") or raw.get("memory_id")
    if isinstance(provided, str) and provided.strip():
        return provided.strip()
    identity = {
        "zh": raw.get("zh") or raw.get("source_text") or raw.get("source"),
        "final_en": raw.get("final_en") or raw.get("target_text") or raw.get("target"),
        "draft_en": raw.get("draft_en") or raw.get("draft") or "",
        "context_before": raw.get("context_before") or "",
        "context_after": raw.get("context_after") or "",
    }
    return "legacy-" + stable_hash(identity)[:20]


def _coerce_memory_records(raw: Any) -> list[MemoryRecord]:
    if isinstance(raw, dict):
        raw_items = raw.get("records", raw.get("examples", raw.get("items", [])))
    else:
        raw_items = raw
    if not isinstance(raw_items, list):
        return []
    records: list[MemoryRecord] = []
    for position, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        target = item.get("target")
        zh = item.get("zh") or item.get("source_text") or source
        final = item.get("final_en") or item.get("target_text") or target
        if isinstance(zh, dict):
            zh = zh.get("text") or zh.get("joined")
        if isinstance(final, dict):
            final = (
                final.get("canonical_text")
                or final.get("text")
                or final.get("joined")
            )
        if not isinstance(zh, str) or not zh.strip():
            continue
        if not isinstance(final, str) or not final.strip():
            continue
        status = str(item.get("status") or "legacy_unverified")
        if status.casefold() in {"rejected", "deprecated", "quarantined", "disabled"}:
            continue
        provenance = item.get("provenance")
        context_before = item.get("context_before") or ""
        context_after = item.get("context_after") or ""
        if isinstance(source, dict):
            context_before = source.get("context_before") or context_before
            context_after = source.get("context_after") or context_after
        if isinstance(context_before, list):
            context_before = " ".join(str(value) for value in context_before if value)
        if isinstance(context_after, list):
            context_after = " ".join(str(value) for value in context_after if value)
        records.append(
            MemoryRecord(
                record_id=_stable_record_id(item, position),
                zh=zh.strip(),
                final_en=final.strip(),
                draft_en=str(item.get("draft_en") or item.get("draft") or "").strip(),
                context_before=str(context_before).strip(),
                context_after=str(context_after).strip(),
                status=status,
                provenance=provenance if isinstance(provenance, dict) else {},
            )
        )
    return records


def load_memory_records(path: Path) -> list[MemoryRecord]:
    """Use the memory subsystem when available, with a legacy JSON adapter."""

    raw: Any | None = None
    if path.is_file():
        file_value = read_json(path)
        if isinstance(file_value, list) or (
            isinstance(file_value, dict)
            and any(key in file_value for key in ("examples", "records", "items"))
        ):
            raw = file_value
    try:
        from . import memory as memory_module

        loader = getattr(memory_module, "load_memory_records", None)
        if raw is None and callable(loader):
            try:
                raw = loader(path)
            except TypeError:
                raw = loader(state_root=path)
    except (ImportError, AttributeError):
        raw = None
    if raw is None:
        if not path.exists():
            return []
        raw = read_json(path)
    return _coerce_memory_records(raw)


def retrieve_examples(
    *,
    query: str,
    context: str,
    records: Iterable[MemoryRecord],
    profile: dict[str, Any],
    top_k: int,
    min_score: float,
) -> dict[str, Any]:
    if top_k <= 0:
        raise SubtitleError(
            "--top-k must be greater than zero",
            code="invalid_retrieval_setting",
            exit_code=ExitCode.CONFIG_INVALID,
            details={"top_k": top_k},
        )
    if not 0 <= min_score <= 1:
        raise SubtitleError(
            "--min-score must be between 0 and 1",
            code="invalid_retrieval_setting",
            exit_code=ExitCode.CONFIG_INVALID,
            details={"min_score": min_score},
        )

    candidates: list[dict[str, Any]] = []
    normalized_targets_by_source: dict[str, set[str]] = {}
    record_list = list(records)
    for record in record_list:
        source_key = normalized_zh(record.zh)
        normalized_targets_by_source.setdefault(source_key, set()).add(
            re.sub(r"\s+", " ", record.final_en).casefold().strip()
        )

    normalized_query = normalized_zh(query)
    for record in record_list:
        cue_components = similarity_components(query, record.zh)
        cue_score = combined_similarity(cue_components)
        record_context = " ".join(
            value
            for value in (
                record.context_before,
                record.zh,
                record.context_after,
            )
            if value
        )
        context_components = similarity_components(context, record_context)
        context_score = combined_similarity(context_components)
        exact = bool(normalized_query) and normalized_query == normalized_zh(record.zh)
        score = 1.0 if exact else max(cue_score, context_score * 0.85)
        conflict = len(normalized_targets_by_source.get(normalized_zh(record.zh), set())) > 1
        candidates.append(
            {
                "id": record.record_id,
                "zh": record.zh,
                "approved_en": clean_subtitle_text(record.final_en, profile),
                "draft_en": clean_subtitle_text(record.draft_en, profile)
                if record.draft_en
                else "",
                "context_before": record.context_before,
                "context_after": record.context_after,
                "status": record.status,
                "score": round(score, 6),
                "conflict": conflict,
                "explanation": {
                    "exact_source_match": exact,
                    "cue_similarity": round(cue_score, 6),
                    "context_similarity": round(context_score, 6),
                    "components": {
                        "cue": {
                            key: round(value, 6)
                            for key, value in cue_components.items()
                        },
                        "context": {
                            key: round(value, 6)
                            for key, value in context_components.items()
                        },
                    },
                    "tie_breaker": "stable_record_id",
                },
                "provenance": record.provenance,
            }
        )
    candidates.sort(key=lambda item: (-item["score"], item["id"]))
    visible = candidates[:top_k]
    if not visible:
        return {
            "status": "abstained",
            "reason": "no_approved_examples",
            "min_score": min_score,
            "candidates": [],
        }
    top = visible[0]
    exact_conflicts = [
        item
        for item in visible
        if item["explanation"]["exact_source_match"] and item["conflict"]
    ]
    if exact_conflicts:
        return {
            "status": "abstained",
            "reason": "conflicting_exact_matches",
            "min_score": min_score,
            "candidates": visible,
        }
    if top["score"] < min_score:
        return {
            "status": "abstained",
            "reason": "below_min_score",
            "min_score": min_score,
            "best_score": top["score"],
            "candidates": [],
        }
    return {
        "status": "matched",
        "reason": "score_threshold_met",
        "min_score": min_score,
        "candidates": [item for item in visible if item["score"] >= min_score],
    }


def _coerce_glossary_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("terms"), list):
        items = raw["terms"]
    elif isinstance(raw, dict) and isinstance(raw.get("entries"), list):
        items = raw["entries"]
    elif isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = []
        for source, target in raw.items():
            if isinstance(target, str):
                items.append({"zh": source, "preferred_en": target})
            elif isinstance(target, dict):
                items.append({"zh": source, **target})
    else:
        return []
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_value = item.get("source")
        if isinstance(source_value, dict):
            source = source_value.get("canonical") or source_value.get("text")
            nested_aliases = source_value.get("aliases") or []
        else:
            source = source_value
            nested_aliases = []
        source = (
            item.get("zh")
            or source
            or item.get("term_zh")
            or item.get("source_text")
        )
        target_value = item.get("target")
        if isinstance(target_value, dict):
            target = target_value.get("preferred") or target_value.get("text")
        else:
            target = target_value
        target = (
            item.get("preferred_en")
            or item.get("en")
            or target
            or item.get("approved_en")
        )
        if not isinstance(source, str) or not source.strip():
            continue
        if not isinstance(target, str) or not target.strip():
            continue
        variants = (
            item.get("variants")
            or item.get("zh_contains")
            or nested_aliases
            or []
        )
        if isinstance(variants, str):
            variants = [variants]
        signals = [source.strip()] + [
            str(value).strip() for value in variants if str(value).strip()
        ]
        record_id = item.get("term_id") or item.get("id") or "term-" + stable_hash(
            {"signals": signals, "target": target}
        )[:20]
        result.append(
            {
                "id": str(record_id),
                "term_id": str(record_id),
                "signals": signals,
                "preferred_en": target.strip(),
                "mandatory": bool(item.get("mandatory", False)),
                "note": str(item.get("note") or ""),
            }
        )
    return result


def load_glossary(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    file_value = read_json(path)
    raw: Any | None = file_value
    # The governed termbase loader is authoritative for its 1.0 contract.
    # A small direct adapter remains intentionally available for legacy files
    # and forward-compatible v2 fixtures while those schemas are migrated.
    schema_version = (
        str(file_value.get("schema_version") or "")
        if isinstance(file_value, dict)
        else ""
    )
    if schema_version == "1.0":
        try:
            from . import terminology as terminology_module

            loader = getattr(terminology_module, "load_termbase", None)
            if callable(loader):
                raw = loader(path)
        except (ImportError, AttributeError):
            raw = file_value
    if hasattr(raw, "records"):
        raw = getattr(raw, "records")
    return _coerce_glossary_items(raw)


def match_glossary(context: str, glossary: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in glossary:
        matched = [signal for signal in item["signals"] if signal and signal in context]
        if matched:
            matches.append(
                {
                    "id": item["id"],
                    "term_id": item.get("term_id", item["id"]),
                    "matched_signals": matched,
                    "preferred_en": item["preferred_en"],
                    "mandatory": item["mandatory"],
                    "note": item["note"],
                }
            )
    matches.sort(key=lambda item: item["id"])
    return matches
