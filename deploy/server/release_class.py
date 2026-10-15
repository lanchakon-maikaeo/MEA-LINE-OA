"""Is a release minor (production may take it by itself) or major (a person approves it)?
ADR 0015 Q6: CI records in release.json which migrations cannot be undone, and the PostgreSQL
and n8n major versions; the host compares the new release with the one it runs.

    python3 release_class.py <new release.json> <current release.json>
    → "minor", or "major: <reasons>" (exit 0 either way)
"""

import json
import re
import sys
from pathlib import Path

# Operations whose effect the previous release cannot live with, or that cannot be reversed.
DROPS = re.compile(
    r"migrations\.(RemoveField|DeleteModel|RenameField|RenameModel|AlterModelTable)\b"
)
ONE_WAY_PYTHON = re.compile(r"migrations\.RunPython\(\s*[\w.]+\s*\)")
ONE_WAY_SQL = re.compile(r"migrations\.RunSQL\((?![^)]*reverse_sql)")


def unsafe(text):
    """True when a migration file drops/renames something or runs code without a way back."""
    return bool(DROPS.search(text) or ONE_WAY_PYTHON.search(text) or ONE_WAY_SQL.search(text))


def classify(new, current):
    reasons = []
    added = sorted(set(new.get("migrations", [])) - set(current.get("migrations", [])))
    if added and "unsafe_migrations" not in new:
        reasons.append("the release has no migration check")
    one_way = sorted(set(added) & set(new.get("unsafe_migrations", [])))
    if one_way:
        reasons.append("migrations that cannot be undone: " + ", ".join(one_way))
    if set(current.get("migrations", [])) - set(new.get("migrations", [])):
        reasons.append("the new release lacks migrations production already has")
    for key, label in (("postgres_major", "PostgreSQL"), ("n8n_major", "n8n")):
        if new.get(key) and current.get(key) and new[key] != current[key]:
            reasons.append(f"{label} {current[key]} -> {new[key]}")
    return ("major", reasons) if reasons else ("minor", [])


def main(argv):
    new, current = (json.loads(Path(p).read_text()) for p in argv[1:3])
    kind, reasons = classify(new, current)
    print(kind if kind == "minor" else f"major: {'; '.join(reasons)}")


if __name__ == "__main__":
    main(sys.argv)
