"""Encoding-aware reads, canonical hashing, and crash-safe publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from .errors import ExitCode, SubtitleError, input_error


@dataclass(frozen=True)
class DecodedText:
    text: str
    encoding: str
    had_bom: bool
    newline: str
    mixed_newlines: bool
    raw_bytes: bytes


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stable_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _detect_newline(text: str) -> tuple[str, bool]:
    crlf = text.count("\r\n")
    without_crlf = text.replace("\r\n", "")
    lf = without_crlf.count("\n")
    cr = without_crlf.count("\r")
    counts = {"\r\n": crlf, "\n": lf, "\r": cr}
    present = [kind for kind, count in counts.items() if count]
    if not present:
        return "\n", False
    newline = max(present, key=lambda kind: counts[kind])
    return newline, len(present) > 1


def decode_text_bytes(data: bytes, *, source: str = "<bytes>") -> DecodedText:
    attempts: list[tuple[str, bool, bytes]] = []
    if data.startswith(b"\xef\xbb\xbf"):
        attempts.append(("utf-8-sig", True, data[3:]))
    else:
        attempts.extend(
            [
                ("utf-8", False, data),
                ("gb18030", False, data),
            ]
        )
    for encoding, had_bom, payload in attempts:
        try:
            text = payload.decode(encoding)
        except UnicodeDecodeError:
            continue
        newline, mixed = _detect_newline(text)
        return DecodedText(
            text=text,
            encoding=encoding,
            had_bom=had_bom,
            newline=newline,
            mixed_newlines=mixed,
            raw_bytes=data,
        )
    raise SubtitleError(
        f"Cannot decode text file: {source}",
        code="text_decode_failed",
        exit_code=ExitCode.INPUT_IO,
        details={"path": source, "attempted_encodings": ["utf-8", "gb18030"]},
    )


def read_decoded_text(path: Path) -> DecodedText:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise input_error(
            f"Cannot read file: {path}",
            code="file_read_failed",
            path=str(path),
            reason=str(exc),
        ) from exc
    return decode_text_bytes(data, source=str(path))


def read_json(path: Path) -> Any:
    decoded = read_decoded_text(path)
    try:
        return json.loads(decoded.text)
    except json.JSONDecodeError as exc:
        raise SubtitleError(
            f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}",
            code="invalid_json",
            exit_code=ExitCode.INVALID_ARTIFACT,
            details={
                "path": str(path),
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise input_error(
            f"Cannot create output directory: {path.parent}",
            code="output_directory_failed",
            path=str(path.parent),
            reason=str(exc),
        ) from exc

    old_mode: int | None = None
    if path.exists():
        try:
            old_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            old_mode = None

    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if old_mode is not None:
            os.chmod(temporary_name, old_mode)
        os.replace(temporary_name, path)
        temporary_name = ""
    except OSError as exc:
        raise input_error(
            f"Cannot publish output atomically: {path}",
            code="atomic_write_failed",
            path=str(path),
            reason=str(exc),
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    _atomic_write(path, data)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    bom: bool = False,
) -> None:
    try:
        payload = text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise input_error(
            f"Cannot encode output as {encoding}: {path}",
            code="text_encode_failed",
            path=str(path),
            encoding=encoding,
            reason=str(exc),
        ) from exc
    if bom and encoding.casefold().replace("_", "-") == "utf-8":
        payload = b"\xef\xbb\xbf" + payload
    _atomic_write(path, payload)


def atomic_write_json(path: Path, data: Any) -> None:
    payload = (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    _atomic_write(path, payload)
