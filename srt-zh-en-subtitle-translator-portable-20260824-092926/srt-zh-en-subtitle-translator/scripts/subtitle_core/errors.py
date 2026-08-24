"""Structured failures and stable process exit codes."""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    USAGE = 2
    INPUT_IO = 10
    INVALID_SRT = 11
    INVALID_ARTIFACT = 12
    SOURCE_MISMATCH = 13
    REVIEW_BLOCKED = 14
    VALIDATION_FAILED = 15
    DEPENDENCY_MISSING = 16
    CONFIG_INVALID = 17
    UNSUPPORTED = 18
    INTERNAL_ERROR = 70


class SubtitleError(Exception):
    """Expected command failure with a stable machine-readable identity."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        exit_code: ExitCode,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def input_error(message: str, *, code: str = "input_io", **details: Any) -> SubtitleError:
    return SubtitleError(
        message,
        code=code,
        exit_code=ExitCode.INPUT_IO,
        details=details,
    )


def artifact_error(
    message: str, *, code: str = "invalid_artifact", **details: Any
) -> SubtitleError:
    return SubtitleError(
        message,
        code=code,
        exit_code=ExitCode.INVALID_ARTIFACT,
        details=details,
    )
