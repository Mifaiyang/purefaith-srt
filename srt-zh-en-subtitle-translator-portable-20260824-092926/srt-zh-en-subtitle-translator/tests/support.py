from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Iterable


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_ROOT / "scripts" / "srt_translate_helper.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
STYLE = SKILL_ROOT / "references" / "style-profile.json"
GLOSSARY = SKILL_ROOT / "references" / "glossary.json"
MEMORY = SKILL_ROOT / "references" / "translation-memory.json"


def load_helper():
    """Load the CLI as a module without relying on package installation."""
    spec = importlib.util.spec_from_file_location("srt_translate_helper_under_test", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(
    *args: object,
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
        cwd=str(cwd or SKILL_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(
            f"CLI failed ({result.returncode})\n"
            f"command={args!r}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def write_text(path: Path, text: str, *, newline: str = "\n", bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    encoding = "utf-8-sig" if bom else "utf-8"
    path.write_text(payload, encoding=encoding, newline="")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cue_fingerprint_map(prepared: dict) -> dict[str, str]:
    source = prepared.get("source", {})
    fingerprints = source.get("cue_fingerprints", {})
    if isinstance(fingerprints, dict):
        return {str(key): str(value) for key, value in fingerprints.items()}
    if isinstance(fingerprints, list):
        return {
            str(item["index"]): str(item.get("fingerprint", item.get("sha256", "")))
            for item in fingerprints
        }
    out: dict[str, str] = {}
    for cue in prepared.get("cues", []):
        value = cue.get("source_fingerprint", cue.get("fingerprint", ""))
        if value:
            out[str(cue["index"])] = str(value)
    return out


def prepare_source(
    source: Path,
    output: Path,
    *,
    preferences: Path = MEMORY,
    glossary: Path = GLOSSARY,
    style: Path = STYLE,
    min_score: float | None = None,
) -> dict:
    args: list[object] = [
        "prepare",
        source,
        output,
        "--preferences",
        preferences,
        "--glossary",
        glossary,
        "--style-profile",
        style,
    ]
    if min_score is not None:
        args += ["--min-score", min_score]
    run_cli(*args, check=True)
    return read_json(output)


def write_bound_translations(
    output: Path,
    prepared: dict,
    translations: Iterable[tuple[int, str]],
) -> dict:
    fingerprints = cue_fingerprint_map(prepared)
    source = prepared["source"]
    sha = source.get("sha256", source.get("source_sha256"))
    envelope = {
        "schema_version": "subtitle.translation.v2",
        "source": {
            "sha256": sha,
            "cue_fingerprints": fingerprints,
        },
        "translations": [
            {
                "index": index,
                "text": text,
                **(
                    {"source_fingerprint": fingerprints[str(index)]}
                    if str(index) in fingerprints
                    else {}
                ),
            }
            for index, text in translations
        ],
    }
    write_json(output, envelope)
    return envelope


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def collect_issue_types(report: dict) -> set[str]:
    return {
        str(issue.get("type", issue.get("code", "")))
        for issue in report.get("issues", [])
        if isinstance(issue, dict)
    }


def minimal_srt(texts: Iterable[str]) -> str:
    blocks = []
    for position, text in enumerate(texts, 1):
        start_ms = (position - 1) * 2_000
        end_ms = start_ms + 1_500
        start = f"00:00:{start_ms // 1000:02d},{start_ms % 1000:03d}"
        end = f"00:00:{end_ms // 1000:02d},{end_ms % 1000:03d}"
        blocks.append(f"{position}\n{start} --> {end}\n{text}")
    return "\n\n".join(blocks) + "\n"
