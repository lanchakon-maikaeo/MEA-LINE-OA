"""release.json for one application image (M12, E.8 step 1/6): everything a deploy needs to
match, and nothing secret. Written into the image at build time; CI keeps a copy per release.

    python deploy/release_manifest.py --commit <sha> --db-image <ref@sha256:…> [--root .]
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

PINNED = re.compile(r"^\s*image:\s*(\S+@sha256:[0-9a-f]{64})\s*$", re.M)
POSTGRES = re.compile(r"^FROM postgres:(\d+)", re.M)
sys.path.insert(0, str(Path(__file__).resolve().parent / "server"))
from release_class import unsafe  # noqa: E402 - beside this file, also shipped in the bundle


def workflows_digest(folder):
    digest = hashlib.sha256()
    for path in sorted(folder.glob("*.json")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def manifest(root, commit, db_image):
    compose = (root / "deploy/server/compose.yaml").read_text()
    images = sorted(set(PINNED.findall(compose)))

    def pick(prefix):
        return next((i for i in images if i.split("@")[0].split(":")[0].endswith(prefix)), "")

    return {
        "schema": 1,
        "commit": commit,
        "built_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "db_image": db_image,
        "n8n_image": pick("n8nio/n8n"),
        "clamav_image": pick("clamav/clamav-debian"),
        "caddy_image": pick("caddy"),
        "backup_image": pick("restic/restic"),
        "workflows_sha256": workflows_digest(root / "workflows/n8n"),
        "migrations": sorted(
            p.stem for p in (root / "operations/migrations").glob("[0-9]*.py") if p.is_file()
        ),
        # ADR 0015 Q6: what makes a release major (a person approves it) for faoj auto-promote.
        "unsafe_migrations": sorted(
            p.stem
            for p in (root / "operations/migrations").glob("[0-9]*.py")
            if p.is_file() and unsafe(p.read_text())
        ),
        "postgres_major": next(
            iter(POSTGRES.findall((root / "deploy/server/db/Dockerfile").read_text())), ""
        ),
        "n8n_major": pick("n8nio/n8n").split("@")[0].rsplit(":", 1)[-1].split(".")[0],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", default="unknown")
    parser.add_argument("--db-image", default="")
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    if args.db_image and not re.search(r"@sha256:[0-9a-f]{64}$", args.db_image):
        sys.exit("--db-image must be pinned by digest")
    print(json.dumps(manifest(Path(args.root), args.commit, args.db_image), indent=2))


if __name__ == "__main__":
    main()
