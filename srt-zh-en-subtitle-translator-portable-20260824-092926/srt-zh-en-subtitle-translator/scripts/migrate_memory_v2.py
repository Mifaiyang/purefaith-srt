#!/usr/bin/env python3
"""Standalone governed-memory migration and release utility.

All mutating commands default to dry-run.  Pass ``--apply`` explicitly to
append events, create a release, or replace the active pointer.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from subtitle_core.memory import (  # noqa: E402
    DEFAULT_LEGACY_MEMORY_PATH,
    DEFAULT_REFERENCES_DIR,
    DEFAULT_STATE_ROOT,
    MemoryGovernanceError,
    append_review_decision,
    lint_resources,
    list_releases,
    load_active_pointer,
    promote_candidates,
    resource_stats,
    rollback_release,
)
from subtitle_core.migration import migrate_legacy_memory  # noqa: E402


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def command_migrate(args: argparse.Namespace) -> None:
    _print(
        migrate_legacy_memory(
            args.legacy,
            args.state_root,
            corpus_root=args.corpus,
            style_profile_path=args.style_profile,
            termbase_path=args.termbase,
            activate=args.activate,
            actor_id=args.actor,
            dry_run=not args.apply,
        )
    )


def command_lint(args: argparse.Namespace) -> None:
    result = lint_resources(args.references, state_root=args.state_root)
    _print(result)
    if not result["ok"]:
        raise SystemExit(1)


def command_stats(args: argparse.Namespace) -> None:
    _print(resource_stats(args.references, state_root=args.state_root))


def command_approve(args: argparse.Namespace) -> None:
    _print(
        append_review_decision(
            args.state_root,
            args.candidate_id,
            decision=args.decision,
            reviewer_id=args.reviewer,
            rationale=args.rationale,
            dry_run=not args.apply,
        )
    )


def command_promote(args: argparse.Namespace) -> None:
    _print(
        promote_candidates(
            args.state_root,
            args.candidate_id,
            actor_id=args.actor,
            policy_versions={
                "style_profile": args.style_version,
                "termbase": args.termbase_version,
            },
            activate=not args.no_activate,
            dry_run=not args.apply,
        )
    )


def command_releases(args: argparse.Namespace) -> None:
    _print(
        {
            "active_pointer": load_active_pointer(args.state_root),
            "releases": list_releases(args.state_root),
        }
    )


def command_status(args: argparse.Namespace) -> None:
    _print(
        {
            "active_pointer": load_active_pointer(args.state_root),
            "stats": resource_stats(args.references, state_root=args.state_root),
            "lint": lint_resources(args.references, state_root=args.state_root),
        }
    )


def command_rollback(args: argparse.Namespace) -> None:
    _print(
        rollback_release(
            args.state_root,
            args.release_id,
            actor_id=args.actor,
            reason=args.reason,
            dry_run=not args.apply,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Governed PUREFAITH translation-memory migration and releases."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser(
        "migrate",
        help="Plan/apply a non-destructive 108-row legacy migration.",
    )
    migrate.add_argument("--legacy", default=str(DEFAULT_LEGACY_MEMORY_PATH))
    migrate.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    migrate.add_argument(
        "--corpus",
        help="Optional corpus root; migration reads only manifest partitions.dev.",
    )
    migrate.add_argument(
        "--style-profile",
        default=str(DEFAULT_REFERENCES_DIR / "style-profile.json"),
    )
    migrate.add_argument(
        "--termbase",
        default=str(DEFAULT_REFERENCES_DIR / "glossary.json"),
    )
    migrate.add_argument("--actor", default="legacy-migration")
    migrate.add_argument("--activate", action="store_true")
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="Write immutable release/run files; default is dry-run.",
    )
    migrate.set_defaults(func=command_migrate)

    lint = subparsers.add_parser("lint", help="Lint references and governed state.")
    lint.add_argument("--references", default=str(DEFAULT_REFERENCES_DIR))
    lint.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    lint.set_defaults(func=command_lint)

    stats = subparsers.add_parser("stats", help="Show resource and governed-state stats.")
    stats.add_argument("--references", default=str(DEFAULT_REFERENCES_DIR))
    stats.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    stats.set_defaults(func=command_stats)

    approve = subparsers.add_parser(
        "approve",
        help="Append an immutable candidate review decision.",
    )
    approve.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    approve.add_argument("--candidate-id", required=True)
    approve.add_argument("--decision", choices=("approved", "rejected"), required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--rationale", required=True)
    approve.add_argument("--apply", action="store_true")
    approve.set_defaults(func=command_approve)

    promote = subparsers.add_parser(
        "promote",
        help="Promote explicitly approved candidates to an immutable release.",
    )
    promote.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    promote.add_argument("--candidate-id", action="append", required=True)
    promote.add_argument("--actor", required=True)
    promote.add_argument("--style-version", default="2.0.0")
    promote.add_argument("--termbase-version", default="1.0.0")
    promote.add_argument("--no-activate", action="store_true")
    promote.add_argument("--apply", action="store_true")
    promote.set_defaults(func=command_promote)

    releases = subparsers.add_parser("releases", help="List immutable releases.")
    releases.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    releases.set_defaults(func=command_releases)

    status = subparsers.add_parser("status", help="Show active pointer, stats, and lint.")
    status.add_argument("--references", default=str(DEFAULT_REFERENCES_DIR))
    status.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    status.set_defaults(func=command_status)

    rollback = subparsers.add_parser(
        "rollback",
        help="Atomically repoint to an existing immutable release.",
    )
    rollback.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    rollback.add_argument("--release-id", required=True)
    rollback.add_argument("--actor", required=True)
    rollback.add_argument("--reason", default="manual_rollback")
    rollback.add_argument("--apply", action="store_true")
    rollback.set_defaults(func=command_rollback)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except MemoryGovernanceError as exc:
        parser.exit(2, f"Governed-memory error: {exc}\n")


if __name__ == "__main__":
    main()
