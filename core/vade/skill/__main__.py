"""`python -m vade.skill --check`: print the catalog, start nothing.

Used by ops to answer "is this skill actually going to be injected?" without a
running core, a registry or a model. The snapshot it prints is the same
`{name, revision, status}` C-11 defines, so it can be diffed between hosts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .loader import SkillLoader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vade.skill")
    parser.add_argument("--check", action="store_true", help="validate and print the catalog")
    parser.add_argument("--dir", default=None, help="skills directory (default: <data_dir>/skills)")
    parser.add_argument("--data-dir", default="./data", help="core data dir (default: ./data)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--strict", action="store_true", help="exit 1 when a skill is invalid")
    parser.add_argument("--tools", default="", help="comma-separated tool names to check availability")
    args = parser.parse_args(argv)

    if not args.check:
        parser.print_help()
        return 0

    tools = [name.strip() for name in args.tools.split(",") if name.strip()]
    skills_dir = Path(args.dir) if args.dir else Path(args.data_dir) / "skills"
    loader = SkillLoader(skills_dir, tool_snapshot=tools)
    loader.reload()

    if args.json:
        print(json.dumps(
            {
                "skills": loader.check(),
                "invalid": loader.invalid,
                "warnings": list(loader.warnings),
                "snapshot": loader.snapshot(),
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(f"skills: {skills_dir}")
        rows = loader.check()
        if not rows:
            print("  (none)")
        for row in rows:
            marker = f" {row['marker']}" if row["marker"] else ""
            extra = f" — {row['unavailable_reason']}" if row["unavailable_reason"] else ""
            print(f"  - {row['name']} rev={row['revision']} [{row['status']}]{marker}{extra}")
        if loader.invalid:
            print("invalid (not injected):")
            for name, reason in sorted(loader.invalid.items()):
                print(f"  - {name}: {reason}")
        for warning in loader.warnings:
            print(f"warning: {warning}")
        print(f"snapshot: {loader.snapshot()['digest'][:16]}")
    if args.strict and loader.invalid:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
