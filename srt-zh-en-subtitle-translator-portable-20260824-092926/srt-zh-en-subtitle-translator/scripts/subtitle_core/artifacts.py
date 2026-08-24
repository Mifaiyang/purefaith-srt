"""Typed/versioned translation artifact contracts with legacy adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .constants import TRANSLATION_SCHEMA
from .errors import ExitCode, SubtitleError, artifact_error
from .io_utils import read_json
from .srt import SrtDocument


INTEGER_TEXT_RE = re.compile(r"^[+]?\d+$")


def parse_artifact_index(value: Any, *, location: str) -> int:
    if isinstance(value, bool):
        raise artifact_error(
            "Boolean is not a valid cue index",
            code="invalid_cue_index",
            location=location,
            value=value,
        )
    if isinstance(value, int):
        index = value
    elif isinstance(value, str) and INTEGER_TEXT_RE.fullmatch(value.strip()):
        index = int(value.strip())
    else:
        raise artifact_error(
            "Cue index must be a strict integer",
            code="invalid_cue_index",
            location=location,
            value=repr(value),
        )
    if index <= 0:
        raise artifact_error(
            "Cue index must be positive",
            code="invalid_cue_index",
            location=location,
            value=index,
        )
    return index


@dataclass(frozen=True)
class SourceBinding:
    sha256: str
    cue_count: int | None
    cue_fingerprints: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "cue_count": self.cue_count,
            "cue_fingerprints": dict(self.cue_fingerprints),
        }


@dataclass(frozen=True)
class TranslationEntry:
    index: int
    text: str
    source_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"index": self.index, "text": self.text}
        if self.source_fingerprint:
            value["source_fingerprint"] = self.source_fingerprint
        return value


@dataclass(frozen=True)
class TranslationArtifact:
    schema_version: str
    entries: tuple[TranslationEntry, ...]
    source: SourceBinding | None
    legacy_unbound: bool

    @property
    def translations(self) -> dict[int, str]:
        return {entry.index: entry.text for entry in self.entries}


def _parse_source_binding(value: Any) -> SourceBinding | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise artifact_error(
            "Translation source binding must be an object",
            code="invalid_source_binding",
        )
    digest = value.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise artifact_error(
            "Translation source binding requires a SHA-256 digest",
            code="invalid_source_binding",
            sha256=repr(digest),
        )
    cue_count = value.get("cue_count")
    if cue_count is not None:
        cue_count = parse_artifact_index(cue_count, location="source.cue_count")
    fingerprints_raw = value.get("cue_fingerprints", {})
    if not isinstance(fingerprints_raw, dict):
        raise artifact_error(
            "source.cue_fingerprints must be an object",
            code="invalid_source_binding",
        )
    fingerprints: dict[str, str] = {}
    for raw_index, fingerprint in fingerprints_raw.items():
        index = parse_artifact_index(raw_index, location="source.cue_fingerprints")
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", fingerprint
        ):
            raise artifact_error(
                "Cue fingerprint must be a SHA-256 digest",
                code="invalid_source_binding",
                index=index,
            )
        fingerprints[str(index)] = fingerprint.casefold()
    return SourceBinding(
        sha256=digest.casefold(),
        cue_count=cue_count,
        cue_fingerprints=fingerprints,
    )


def load_translation_artifact(path: Path) -> TranslationArtifact:
    data = read_json(path)
    source_value: Any = None
    schema_version = "legacy.translation.v1"
    legacy_unbound = True
    if isinstance(data, list):
        raw_entries = data
    elif isinstance(data, dict):
        raw_entries = data.get("translations", data.get("items"))
        if raw_entries is None:
            raise artifact_error(
                "Translation object must contain translations or items",
                path=str(path),
            )
        schema_version = str(data.get("schema_version") or "legacy.translation.v1")
        source_value = data.get("source")
        legacy_unbound = source_value is None
        if schema_version not in {
            TRANSLATION_SCHEMA,
            "2",
            "2.0",
            "legacy.translation.v1",
        }:
            raise artifact_error(
                f"Unsupported translation schema: {schema_version}",
                code="unsupported_translation_schema",
                path=str(path),
                schema_version=schema_version,
            )
    else:
        raise artifact_error(
            "Translations must be a JSON list or object",
            path=str(path),
        )

    if not isinstance(raw_entries, list):
        raise artifact_error(
            "translations must be a JSON list",
            path=str(path),
        )
    entries: list[TranslationEntry] = []
    seen: set[int] = set()
    for position, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise artifact_error(
                "Each translation entry must be an object",
                position=position,
            )
        if "index" not in item or "text" not in item:
            raise artifact_error(
                'Each translation must contain "index" and "text"',
                position=position,
            )
        index = parse_artifact_index(item["index"], location=f"translations[{position}]")
        if index in seen:
            raise artifact_error(
                f"Duplicate translation for cue {index}",
                code="duplicate_translation_index",
                index=index,
            )
        seen.add(index)
        text = item["text"]
        if not isinstance(text, str):
            raise artifact_error(
                f"Translation text for cue {index} must be a string",
                code="invalid_translation_text",
                index=index,
                actual_type=type(text).__name__,
            )
        if not text.strip():
            raise artifact_error(
                f"Translation text for cue {index} is empty",
                code="empty_translation_text",
                index=index,
            )
        fingerprint = item.get("source_fingerprint")
        if fingerprint is not None and (
            not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint)
        ):
            raise artifact_error(
                f"Invalid source fingerprint for cue {index}",
                code="invalid_source_binding",
                index=index,
            )
        entries.append(
            TranslationEntry(
                index=index,
                text=text,
                source_fingerprint=fingerprint.casefold() if fingerprint else None,
            )
        )

    return TranslationArtifact(
        schema_version=schema_version,
        entries=tuple(entries),
        source=_parse_source_binding(source_value),
        legacy_unbound=legacy_unbound,
    )


def validate_translation_binding(
    artifact: TranslationArtifact,
    source: SrtDocument,
    *,
    require_binding: bool,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if artifact.source is None:
        if require_binding:
            raise SubtitleError(
                "Legacy translation artifact is not bound to the source SRT",
                code="source_binding_required",
                exit_code=ExitCode.SOURCE_MISMATCH,
                details={"source_sha256": source.source_sha256},
            )
        warnings.append(
            {
                "code": "legacy_unbound_translation",
                "message": "Artifact has no source hash; compatibility mode used",
            }
        )
    else:
        if artifact.source.sha256 != source.source_sha256:
            raise SubtitleError(
                "Translation artifact belongs to a different source SRT",
                code="source_hash_mismatch",
                exit_code=ExitCode.SOURCE_MISMATCH,
                details={
                    "expected": source.source_sha256,
                    "actual": artifact.source.sha256,
                },
            )
        if (
            artifact.source.cue_count is not None
            and artifact.source.cue_count != len(source.cues)
        ):
            raise SubtitleError(
                "Translation artifact cue count does not match its source",
                code="source_cue_count_mismatch",
                exit_code=ExitCode.SOURCE_MISMATCH,
                details={
                    "expected": len(source.cues),
                    "actual": artifact.source.cue_count,
                },
            )
        for index_text, actual in artifact.source.cue_fingerprints.items():
            expected = source.cue_fingerprints.get(index_text)
            if expected is None or expected != actual:
                raise SubtitleError(
                    f"Translation source fingerprint mismatch at cue {index_text}",
                    code="cue_fingerprint_mismatch",
                    exit_code=ExitCode.SOURCE_MISMATCH,
                    details={
                        "index": int(index_text),
                        "expected": expected,
                        "actual": actual,
                    },
                )

    source_by_index = {cue.index: cue for cue in source.cues}
    for entry in artifact.entries:
        if entry.source_fingerprint:
            cue = source_by_index.get(entry.index)
            expected = cue.fingerprint if cue else None
            if expected != entry.source_fingerprint:
                raise SubtitleError(
                    f"Translation entry fingerprint mismatch at cue {entry.index}",
                    code="cue_fingerprint_mismatch",
                    exit_code=ExitCode.SOURCE_MISMATCH,
                    details={
                        "index": entry.index,
                        "expected": expected,
                        "actual": entry.source_fingerprint,
                    },
                )
    return warnings


def translation_template(source: SrtDocument) -> dict[str, Any]:
    return {
        "schema_version": TRANSLATION_SCHEMA,
        "source": source.source_manifest(),
        "translations": [
            {
                "index": cue.index,
                "source_fingerprint": cue.fingerprint,
                "text": "",
            }
            for cue in source.cues
        ],
    }
