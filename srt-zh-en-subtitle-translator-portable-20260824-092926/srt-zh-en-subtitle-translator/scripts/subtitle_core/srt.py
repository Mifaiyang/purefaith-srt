"""Strict SRT parsing with source-identity and layout metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Mapping

from .errors import ExitCode, SubtitleError
from .io_utils import DecodedText, decode_text_bytes, read_decoded_text, sha256_bytes, stable_hash


INDEX_RE = re.compile(r"^[ \t]*(\d+)[ \t]*$")
TIMECODE_TOKEN = r"(?P<{name}>\d{{2,}}:[0-5]\d:[0-5]\d,\d{{3}})"
TIMING_RE = re.compile(
    r"^[ \t]*"
    + TIMECODE_TOKEN.format(name="start")
    + r"[ \t]+-->[ \t]+"
    + TIMECODE_TOKEN.format(name="end")
    + r"(?P<settings>[ \t]+.*?)?[ \t]*$"
)
BLOCK_SEPARATOR_RE = re.compile(r"(\n[ \t]*\n(?:[ \t]*\n)*)")


@dataclass(frozen=True)
class Cue:
    index: int
    raw_index: str
    raw_timing: str
    text: str
    start_ms: int
    end_ms: int
    timing_settings: str
    fingerprint: str

    def as_manifest(self) -> dict[str, object]:
        return {
            "index": self.index,
            "raw_index": self.raw_index,
            "timing": self.raw_timing,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class SrtDocument:
    path: str
    source_sha256: str
    encoding: str
    had_bom: bool
    newline: str
    mixed_newlines: bool
    prefix: str
    separators: tuple[str, ...]
    suffix: str
    cues: tuple[Cue, ...]
    warnings: tuple[dict[str, object], ...] = field(default_factory=tuple)

    @property
    def cue_fingerprints(self) -> dict[str, str]:
        return {str(cue.index): cue.fingerprint for cue in self.cues}

    def source_manifest(self) -> dict[str, object]:
        return {
            "path": self.path,
            "sha256": self.source_sha256,
            "cue_count": len(self.cues),
            "encoding": self.encoding,
            "had_bom": self.had_bom,
            "newline": self.newline,
            "mixed_newlines": self.mixed_newlines,
            "cue_fingerprints": self.cue_fingerprints,
        }


def _srt_error(
    message: str,
    *,
    path: str,
    block: int | None = None,
    cue: int | None = None,
    code: str = "invalid_srt",
) -> SubtitleError:
    details: dict[str, object] = {"path": path}
    if block is not None:
        details["block"] = block
    if cue is not None:
        details["cue"] = cue
    return SubtitleError(
        message,
        code=code,
        exit_code=ExitCode.INVALID_SRT,
        details=details,
    )


def parse_timecode(value: str, *, path: str, cue: int) -> int:
    hours_text, minutes_text, tail = value.split(":")
    seconds_text, milliseconds_text = tail.split(",")
    hours = int(hours_text)
    minutes = int(minutes_text)
    seconds = int(seconds_text)
    milliseconds = int(milliseconds_text)
    if minutes >= 60 or seconds >= 60:
        raise _srt_error(
            f"Timestamp component out of range at cue {cue}: {value}",
            path=path,
            cue=cue,
            code="invalid_timestamp_range",
        )
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_layout(text: str, *, path: str) -> tuple[str, list[str], list[str], str]:
    normalized = _normalize_newlines(text)
    prefix_match = re.match(r"^\n*", normalized)
    prefix = prefix_match.group(0) if prefix_match else ""
    without_prefix = normalized[len(prefix) :]
    suffix_match = re.search(r"\n*$", without_prefix)
    suffix = suffix_match.group(0) if suffix_match else ""
    core = without_prefix[: len(without_prefix) - len(suffix)] if suffix else without_prefix
    if not core.strip():
        raise _srt_error("SRT contains no cues", path=path, code="empty_srt")

    tokens = BLOCK_SEPARATOR_RE.split(core)
    blocks: list[str] = []
    separators: list[str] = []
    for position, token in enumerate(tokens):
        if position % 2:
            separators.append(token)
        else:
            if not token:
                raise _srt_error(
                    "Unexpected empty SRT block",
                    path=path,
                    code="empty_srt_block",
                )
            blocks.append(token)
    if len(separators) != max(0, len(blocks) - 1):
        raise _srt_error(
            "Could not preserve SRT block separators",
            path=path,
            code="invalid_srt_layout",
        )
    return prefix, blocks, separators, suffix


def _cue_fingerprint(
    *, index: int, raw_index: str, raw_timing: str, text: str
) -> str:
    return stable_hash(
        {
            "index": index,
            "raw_index": raw_index,
            "timing": raw_timing,
            "text": text,
        }
    )


def _parse_decoded(decoded: DecodedText, *, path: str) -> SrtDocument:
    prefix, blocks, separators, suffix = _split_layout(decoded.text, path=path)
    cues: list[Cue] = []
    warnings: list[dict[str, object]] = []
    seen: set[int] = set()
    previous_start = -1
    previous_index: int | None = None

    for block_number, raw_block in enumerate(blocks, 1):
        lines = raw_block.split("\n")
        if len(lines) < 3:
            raise _srt_error(
                "Each SRT block must contain an index, timing line, and text",
                path=path,
                block=block_number,
                code="short_srt_block",
            )
        raw_index = lines[0]
        index_match = INDEX_RE.fullmatch(raw_index)
        if not index_match:
            raise _srt_error(
                f"Invalid cue index line: {raw_index!r}",
                path=path,
                block=block_number,
                code="invalid_cue_index",
            )
        index = int(index_match.group(1))
        if index <= 0:
            raise _srt_error(
                f"Cue index must be positive: {index}",
                path=path,
                block=block_number,
                cue=index,
                code="invalid_cue_index",
            )
        if index in seen:
            raise _srt_error(
                f"Duplicate cue index: {index}",
                path=path,
                block=block_number,
                cue=index,
                code="duplicate_cue_index",
            )
        seen.add(index)

        raw_timing = lines[1]
        timing_match = TIMING_RE.fullmatch(raw_timing)
        if not timing_match:
            raise _srt_error(
                f"Invalid timing line at cue {index}: {raw_timing!r}",
                path=path,
                block=block_number,
                cue=index,
                code="invalid_timing_line",
            )
        start_ms = parse_timecode(timing_match.group("start"), path=path, cue=index)
        end_ms = parse_timecode(timing_match.group("end"), path=path, cue=index)
        if end_ms <= start_ms:
            raise _srt_error(
                f"Cue {index} must end after it starts",
                path=path,
                block=block_number,
                cue=index,
                code="invalid_time_order",
            )
        if start_ms < previous_start:
            raise _srt_error(
                f"Cue {index} starts before the preceding cue",
                path=path,
                block=block_number,
                cue=index,
                code="nonchronological_cues",
            )
        if cues and start_ms < cues[-1].end_ms:
            warnings.append(
                {
                    "code": "overlapping_cues",
                    "cue": index,
                    "previous_cue": cues[-1].index,
                }
            )
        if previous_index is not None and index <= previous_index:
            warnings.append(
                {
                    "code": "nonincreasing_cue_numbers",
                    "cue": index,
                    "previous_cue": previous_index,
                }
            )

        text = "\n".join(lines[2:])
        if not text.strip():
            raise _srt_error(
                f"Cue {index} has empty text",
                path=path,
                block=block_number,
                cue=index,
                code="empty_cue_text",
            )
        cue = Cue(
            index=index,
            raw_index=raw_index,
            raw_timing=raw_timing,
            text=text,
            start_ms=start_ms,
            end_ms=end_ms,
            timing_settings=(timing_match.group("settings") or "").strip(),
            fingerprint=_cue_fingerprint(
                index=index,
                raw_index=raw_index,
                raw_timing=raw_timing,
                text=text,
            ),
        )
        cues.append(cue)
        previous_start = start_ms
        previous_index = index

    if decoded.mixed_newlines:
        warnings.append({"code": "mixed_newlines", "selected": decoded.newline})

    return SrtDocument(
        path=path,
        source_sha256=sha256_bytes(decoded.raw_bytes),
        encoding=decoded.encoding,
        had_bom=decoded.had_bom,
        newline=decoded.newline,
        mixed_newlines=decoded.mixed_newlines,
        prefix=prefix,
        separators=tuple(separators),
        suffix=suffix,
        cues=tuple(cues),
        warnings=tuple(warnings),
    )


def parse_srt(path: Path) -> SrtDocument:
    return _parse_decoded(read_decoded_text(path), path=str(path))


def parse_srt_bytes(data: bytes, *, source: str = "<generated>") -> SrtDocument:
    return _parse_decoded(decode_text_bytes(data, source=source), path=source)


def render_srt(document: SrtDocument, translations: Mapping[int, str]) -> str:
    expected = [cue.index for cue in document.cues]
    expected_set = set(expected)
    actual_set = set(translations)
    missing = [index for index in expected if index not in actual_set]
    extra = sorted(actual_set - expected_set)
    if missing or extra:
        raise SubtitleError(
            "Translation indexes do not match the source SRT",
            code="translation_index_mismatch",
            exit_code=ExitCode.INVALID_ARTIFACT,
            details={"missing": missing, "extra": extra},
        )

    blocks: list[str] = []
    for cue in document.cues:
        text = _normalize_newlines(str(translations[cue.index]))
        blocks.append(f"{cue.raw_index}\n{cue.raw_timing}\n{text}")

    pieces: list[str] = [document.prefix]
    for position, block in enumerate(blocks):
        pieces.append(block)
        if position < len(document.separators):
            pieces.append(document.separators[position])
    pieces.append(document.suffix)
    normalized = "".join(pieces)
    if document.newline != "\n":
        normalized = normalized.replace("\n", document.newline)
    return normalized


def encode_rendered_srt(document: SrtDocument, rendered: str) -> bytes:
    encoding = "utf-8" if document.encoding == "utf-8-sig" else document.encoding
    try:
        payload = rendered.encode(encoding)
    except UnicodeEncodeError as exc:
        raise SubtitleError(
            f"Translated SRT cannot be encoded as source encoding {document.encoding}",
            code="output_encoding_failed",
            exit_code=ExitCode.VALIDATION_FAILED,
            details={"encoding": document.encoding, "reason": str(exc)},
        ) from exc
    if document.had_bom and document.encoding in {"utf-8", "utf-8-sig"}:
        payload = b"\xef\xbb\xbf" + payload
    return payload
