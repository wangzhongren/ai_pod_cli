"""Compare a Git work tree to a pre-migration content baseline; never edit source."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

STATE_DIR = ".aipod-migration"


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def repository_root(value):
    root = Path(value).resolve(strict=True)
    top = Path(os.fsdecode(git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    if top != root:
        raise ValueError("--root must be the Git repository top-level directory")
    return root


def relative_file(value):
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (not parts or normalized.endswith("/") or PurePosixPath(normalized).is_absolute()
            or any(part in {"..", ".git", STATE_DIR} for part in parts)
            or any(char in normalized for char in ":*?[]\x00")):
        raise ValueError(f"Expected an exact repository-relative source file: {value}")
    return PurePosixPath(normalized).as_posix()


def safe_path(root, relative):
    path = root / relative
    for part in [path, *path.parents]:
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError(f"Symbolic links/junctions are not supported: {relative}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes repository: {relative}")
    return path


def content_hashes(root):
    paths = set(os.fsdecode(item) for item in git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
    ).split(b"\0") if item)
    hashes = {}
    for relative in sorted(paths):
        if relative == STATE_DIR or relative.startswith(STATE_DIR + "/"):
            continue
        path = safe_path(root, relative)
        if not path.exists():
            continue  # Already-deleted tracked files are part of the baseline state.
        if not path.is_file():
            raise ValueError(f"Submodules/non-regular files require a separate boundary: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        hashes[relative] = digest.hexdigest()
    return hashes


def baseline_path(root, batch):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", batch):
        raise ValueError("Batch ID must use lowercase letters, digits and hyphens (1-64 characters)")
    return safe_path(root, f"{STATE_DIR}/{batch}.baseline.json")


def snapshot(root, batch, allowed):
    allowed = sorted(set(relative_file(item) for item in allowed))
    if not allowed:
        raise ValueError("At least one --allow file is required")
    for relative in allowed:
        if safe_path(root, relative).is_dir():
            raise ValueError(f"--allow must name a file, not a directory: {relative}")
    target = baseline_path(root, batch)
    if target.exists():
        raise ValueError("Baseline already exists; preserve it and choose a new batch ID")
    hashes = content_hashes(root)
    record = {"version": 1, "root": str(root), "batch": batch,
              "allowed": allowed, "hashes": hashes}
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as output:
        json.dump(record, output, indent=2, ensure_ascii=True)
        output.write("\n")
    return {"status": "snapshot_created", "baseline": str(target), "files": len(hashes)}, 0


def check(root, batch):
    record = json.loads(baseline_path(root, batch).read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("version") != 1
            or Path(record.get("root", "")).resolve() != root or record.get("batch") != batch):
        raise ValueError("Baseline version, repository or batch does not match")
    before = record.get("hashes")
    if not isinstance(before, dict) or not isinstance(record.get("allowed"), list):
        raise ValueError("Invalid baseline structure")
    allowed = {relative_file(value) for value in record["allowed"]}
    for name, value in before.items():
        relative_file(name)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("Invalid baseline hash")
    after = content_hashes(root)
    changed = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        kind = "added" if name not in before else "removed" if name not in after else "modified"
        changed.append({"path": name, "kind": kind, "allowed": name in allowed})
    violations = [item["path"] for item in changed if not item["allowed"]]
    return {"status": "out_of_scope" if violations else "scope_passed",
            "changes": changed, "violations": violations,
            "behavior_verified": False}, int(bool(violations))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["snapshot", "check"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--allow", action="append", default=[])
    args = parser.parse_args()
    try:
        root = repository_root(args.root)
        if args.command == "check" and args.allow:
            raise ValueError("check uses the saved allowlist; --allow is only valid for snapshot")
        result, code = (snapshot(root, args.batch, args.allow) if args.command == "snapshot"
                        else check(root, args.batch))
    except (OSError, ValueError, TypeError) as error:
        result, code = {"status": "error", "error": str(error)}, 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
