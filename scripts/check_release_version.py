#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any


SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("root value must be an object")
    return payload


def _check_versions(root: Path, tag: str) -> list[str]:
    errors: list[str] = []
    try:
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [f"VERSION could not be read: {exc}"]

    if not SEMVER_PATTERN.fullmatch(version):
        errors.append("VERSION must use X.Y.Z semantic version format")

    version_sources: list[tuple[str, str]] = []
    try:
        package = _read_json(root / "frontend" / "package.json")
        version_sources.append(("frontend/package.json", str(package.get("version") or "")))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"frontend/package.json could not be read: {exc}")

    try:
        package_lock = _read_json(root / "frontend" / "package-lock.json")
        root_package = package_lock.get("packages", {}).get("", {})
        if not isinstance(root_package, dict):
            root_package = {}
        version_sources.extend(
            [
                ("frontend/package-lock.json", str(package_lock.get("version") or "")),
                (
                    "frontend/package-lock.json packages['']",
                    str(root_package.get("version") or ""),
                ),
            ]
        )
    except (AttributeError, OSError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"frontend/package-lock.json could not be read: {exc}")

    for source, candidate in version_sources:
        if candidate != version:
            errors.append(f"{source} version {candidate or '<missing>'} does not match VERSION {version}")

    clean_tag = str(tag or "").strip()
    expected_tag = f"v{version}"
    if clean_tag and clean_tag != expected_tag:
        errors.append(f"release tag {clean_tag} does not match expected tag {expected_tag}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check MakerHub release version consistency.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root to validate.",
    )
    parser.add_argument("--tag", default="", help="Optional release tag to validate.")
    args = parser.parse_args(argv)

    errors = _check_versions(args.root.resolve(), args.tag)
    if errors:
        for error in errors:
            print(f"release version check failed: {error}", file=sys.stderr)
        return 1

    version = (args.root.resolve() / "VERSION").read_text(encoding="utf-8").strip()
    print(f"release version check passed: v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
