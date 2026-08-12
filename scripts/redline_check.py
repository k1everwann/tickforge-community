#!/usr/bin/env python3
"""Scan a diff or the working tree for things that must not be published.

This repository is the public, parameter-free half of a private system. The
private half runs against a real account, so a handful of strings would be
actively harmful here: operator identity and host details, execution-venue
identifiers, credential-shaped names, and third-party service endpoints.

It is a plain grep. It is not clever, it does not understand context, and it
will produce false positives - a documentation sentence about ``api_key`` will
trip the credential rule. That is the intended bias: a reviewer glancing at a
short list of hits is cheap, and a leak is not.

Usage::

    python scripts/redline_check.py                 # scan the tracked tree
    python scripts/redline_check.py --diff          # scan `git diff` vs origin/main
    git diff | python scripts/redline_check.py -    # scan a diff on stdin
    python scripts/redline_check.py path/to/file    # scan specific paths

Exit status is 1 when anything matched, so it can be wired into a pre-commit
hook or a CI step.

Note: this file necessarily contains the patterns it searches for, so it
excludes itself from tree scans. Review changes to *this* file by eye.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF_RELATIVE = "scripts/redline_check.py"

#: (category, human explanation, pattern)
RULES: tuple[tuple[str, str, str], ...] = (
    ("host", "private host address", r"100\.94\.236\.57"),
    ("host", "operator account prefix", r"billy@"),
    ("host", "private filesystem path", r"/home/billy"),
    ("host", "private service unit", r"tickforge-trader\.service"),
    ("host", "private service port", r":500[1-4]\b"),
    ("venue", "execution venue SDK", r"shioaji"),
    ("venue", "execution venue name", r"Sinopac"),
    ("venue", "account holder identifier", r"person_id"),
    ("venue", "account identifier", r"account_id"),
    ("venue", "execution venue name", "永豐"),
    ("credential", "credential-shaped name", r"password"),
    ("credential", "credential-shaped name", r"api_key"),
    ("credential", "certificate path", r"ca_path"),
    ("credential", "certificate file", r"\.pfx"),
    ("service", "private model identifier", r"gpt-5\.6"),
    ("service", "provider credential", r"OPENAI_API_KEY"),
    ("service", "provider endpoint", r"api\.openai\.com"),
    ("service", "push provider credential", r"LINE_CHANNEL_ACCESS_TOKEN"),
    ("service", "push provider endpoint", r"ntfy\.sh"),
)

COMPILED = tuple((category, why, re.compile(pattern, re.IGNORECASE)) for category, why, pattern in
                 RULES)

SKIP_DIRECTORIES = {".git", ".venv", "__pycache__", "node_modules", "dist", "build", "htmlcov"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".sqlite3",
                   ".gguf", ".pfx", ".woff", ".woff2"}


class Hit:
    __slots__ = ("category", "why", "location", "line", "text")

    def __init__(self, category: str, why: str, location: str, line: int, text: str) -> None:
        self.category = category
        self.why = why
        self.location = location
        self.line = line
        self.text = text.strip()[:160]

    def render(self) -> str:
        return f"{self.location}:{self.line}: [{self.category}] {self.why}: {self.text}"


def scan_text(
    location: str, text: str, *, added_lines_only: bool = False, diff_mode: bool = False
) -> list[Hit]:
    """Scan text, or the added lines of a unified diff.

    In diff mode the scanner's own hunks are skipped, for the same reason tree
    scans skip this file: it stores the patterns it looks for.
    """
    hits: list[Hit] = []
    current_file = ""
    for number, line in enumerate(text.splitlines(), start=1):
        if diff_mode and line.startswith("+++ "):
            current_file = line[4:].strip().removeprefix("b/")
            continue
        if diff_mode and current_file == SELF_RELATIVE:
            continue
        if added_lines_only and not (line.startswith("+") and not line.startswith("+++")):
            continue
        for category, why, pattern in COMPILED:
            if pattern.search(line):
                hits.append(Hit(category, why, location, number, line))
    return hits


def tracked_files() -> list[Path]:
    try:
        output = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        return [REPO_ROOT / line for line in output.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        return [path for path in REPO_ROOT.rglob("*") if path.is_file()]


def should_scan(path: Path) -> bool:
    if not path.is_file():
        return False
    if any(part in SKIP_DIRECTORIES for part in path.parts):
        return False
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False
    return path.resolve() != Path(__file__).resolve()


def scan_paths(paths: list[Path]) -> list[Hit]:
    hits: list[Hit] = []
    for path in paths:
        if not should_scan(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            location = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        except ValueError:
            location = str(path)
        hits.extend(scan_text(location, text))
    return hits


def git_diff(base: str) -> str:
    """Return the first non-empty diff among branch, working-tree and staged views."""
    for arguments in ([f"{base}...HEAD"], [base], ["--cached", base], []):
        try:
            output = subprocess.run(
                ["git", "diff", *arguments],
                cwd=REPO_ROOT,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        if output and output.strip():
            return output
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "paths", nargs="*", type=Path, help="files to scan; default is the tracked tree"
    )
    parser.add_argument("--diff", action="store_true", help="scan a git diff against --base")
    parser.add_argument("--base", default="origin/main", help="base ref for --diff")
    parser.add_argument(
        "--all-diff-lines",
        action="store_true",
        help="with --diff, also scan removed and context lines",
    )
    arguments = parser.parse_args()

    if arguments.paths == [Path("-")]:
        hits = scan_text(
            "<stdin>",
            sys.stdin.read(),
            added_lines_only=not arguments.all_diff_lines,
            diff_mode=True,
        )
        scanned = "a diff on stdin"
    elif arguments.diff:
        diff = git_diff(arguments.base)
        if not diff.strip():
            print(f"redline_check: no diff against {arguments.base}")
            return 0
        hits = scan_text(
            f"diff({arguments.base})",
            diff,
            added_lines_only=not arguments.all_diff_lines,
            diff_mode=True,
        )
        scanned = f"the diff against {arguments.base}"
    else:
        paths = arguments.paths or tracked_files()
        hits = scan_paths(paths)
        scanned = f"{len(paths)} path(s)"

    if not hits:
        print(f"redline_check: clean ({scanned}, {len(RULES)} rules)")
        return 0

    print(f"redline_check: {len(hits)} hit(s) in {scanned}")
    for hit in hits:
        print("  " + hit.render())
    print("\nEach hit needs a human decision. Do not publish until the list is empty or every")
    print("remaining line is a deliberate, reviewed false positive.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
