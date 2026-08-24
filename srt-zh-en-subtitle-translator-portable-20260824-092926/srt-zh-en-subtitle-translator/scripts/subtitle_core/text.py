"""Text normalization and review/final-output safety policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
from typing import Any

from .errors import ExitCode, SubtitleError
from .io_utils import read_json


TERMINAL_PUNCTUATION_RE = re.compile(r"[.,!?;:，。！？；：]+$")
TERMINAL_PUNCTUATION_BEFORE_QUOTE_RE = re.compile(
    r"""[.,!?;:，。！？；：]+(["'”’])$"""
)
CJK_RE = re.compile(
    "[\u3400-\u9fff\uf900-\ufaff"
    "\U00020000-\U0002a6df\U0002a700-\U0002ebef\U00030000-\U0003134f]"
)
CJK_PUNCTUATION_RE = re.compile(r"[，。！？；：]")
NUMERIC_PLACEHOLDER_RE = re.compile(r"^[\s\d.,+\-/%￥¥$€£]+$", re.UNICODE)
FORMULA_RE = re.compile(r"^[=+@]|^-\s*(?:\d|[A-Za-z_][A-Za-z0-9_.]*\s*\()")
UNSAFE_CONTROL_RE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")


DEFAULT_PROFILE: dict[str, Any] = {
    "profile_name": "default",
    "remove_terminal_punctuation": False,
    "invalid_revised_values": [],
    "reject_numeric_only_revisions": True,
    "translation_principles": [],
    "preferred_phrases": [],
}


@dataclass(frozen=True)
class TextIssue:
    code: str
    message: str
    severity: str = "error"

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }


def load_style_profile(path: Path) -> dict[str, Any]:
    if not path.exists():
        return dict(DEFAULT_PROFILE)
    loaded = read_json(path)
    if not isinstance(loaded, dict):
        raise SubtitleError(
            f"Style profile must be a JSON object: {path}",
            code="invalid_style_profile",
            exit_code=ExitCode.CONFIG_INVALID,
            details={"path": str(path)},
        )
    profile = dict(DEFAULT_PROFILE)
    profile.update(loaded)
    for key in ("translation_principles", "preferred_phrases", "invalid_revised_values"):
        if not isinstance(profile.get(key), list):
            raise SubtitleError(
                f"Style profile field {key!r} must be a list",
                code="invalid_style_profile",
                exit_code=ExitCode.CONFIG_INVALID,
                details={"path": str(path), "field": key},
            )
    if not isinstance(profile.get("remove_terminal_punctuation"), bool):
        raise SubtitleError(
            "Style profile remove_terminal_punctuation must be boolean",
            code="invalid_style_profile",
            exit_code=ExitCode.CONFIG_INVALID,
            details={"path": str(path)},
        )
    return profile


def normalize_space(text: str) -> str:
    lines = []
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    for line in normalized.split("\n"):
        lines.append(re.sub(r"[ \t]+", " ", line).strip())
    return "\n".join(lines).strip()


def clean_subtitle_text(text: str, profile: dict[str, Any]) -> str:
    cleaned = normalize_space(text)
    if profile.get("remove_terminal_punctuation"):
        previous = None
        while cleaned and cleaned != previous:
            previous = cleaned
            cleaned = TERMINAL_PUNCTUATION_RE.sub("", cleaned).rstrip()
            cleaned = TERMINAL_PUNCTUATION_BEFORE_QUOTE_RE.sub(
                r"\1", cleaned
            ).rstrip()
    return cleaned


def normalized_sentinel(text: str) -> str:
    return unicodedata.normalize("NFKC", normalize_space(text)).casefold()


def invalid_revised_reason(text: str, profile: dict[str, Any]) -> str | None:
    value = normalize_space(text)
    if not value:
        return None
    normalized = normalized_sentinel(value)
    invalid_values = {
        normalized_sentinel(str(item))
        for item in profile.get("invalid_revised_values", [])
    }
    if normalized in invalid_values:
        return "known_placeholder"
    if profile.get("reject_numeric_only_revisions", True) and NUMERIC_PLACEHOLDER_RE.fullmatch(
        unicodedata.normalize("NFKC", value)
    ):
        return "numeric_only"
    return None


def is_formula_like(text: str) -> bool:
    return bool(FORMULA_RE.match(str(text).lstrip()))


def spreadsheet_safe_text(text: str) -> str:
    value = str(text)
    return "'" + value if is_formula_like(value) else value


def remove_spreadsheet_guard(text: str) -> str:
    value = str(text)
    if len(value) > 1 and value[0] == "'" and is_formula_like(value[1:]):
        return value[1:]
    return value


def validate_final_text(
    text: str,
    profile: dict[str, Any],
    *,
    english_only: bool = True,
) -> list[TextIssue]:
    issues: list[TextIssue] = []
    value = str(text)
    if not value.strip():
        return [TextIssue("empty_english", "Final English is empty")]
    if is_formula_like(value):
        issues.append(TextIssue("formula_like_text", "Text begins like a spreadsheet formula"))
    if english_only and CJK_RE.search(value):
        issues.append(TextIssue("cjk_residue", "CJK characters remain in final English"))
    if english_only and CJK_PUNCTUATION_RE.search(value):
        issues.append(
            TextIssue(
                "cjk_punctuation",
                "Chinese-style punctuation remains in final English",
            )
        )
    if "\ufffd" in value:
        issues.append(TextIssue("replacement_character", "Unicode replacement character remains"))
    if UNSAFE_CONTROL_RE.search(value):
        issues.append(TextIssue("control_character", "Unsafe control character remains"))
    if ZERO_WIDTH_RE.search(value):
        issues.append(TextIssue("zero_width_character", "Zero-width character remains"))
    if profile.get("remove_terminal_punctuation"):
        stripped = value.strip()
        if TERMINAL_PUNCTUATION_RE.search(stripped) or TERMINAL_PUNCTUATION_BEFORE_QUOTE_RE.search(
            stripped
        ):
            issues.append(
                TextIssue(
                    "terminal_punctuation",
                    "Forbidden terminal punctuation remains",
                )
            )
    return issues
