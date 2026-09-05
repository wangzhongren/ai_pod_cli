"""Offline regression tests using disposable Git repositories."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("guard", Path(__file__).with_name("migration_guard.py"))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aipod-migrate-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "existing.js").write_text("initial\n")
        subprocess.run(["git", "-C", str(self.root), "add", "existing.js"], check=True,
                       capture_output=True)

    def test_dirty_baseline_and_allowed_edit(self):
        (self.root / "existing.js").write_text("user changes\n")
        guard.snapshot(self.root, "pilot", ["existing.js"])
        result, code = guard.check(self.root, "pilot")
        self.assertEqual(code, 0)
        self.assertEqual(result["changes"], [])
        (self.root / "existing.js").write_text("user changes plus migration\n")
        result, code = guard.check(self.root, "pilot")
        self.assertEqual(code, 0)
        self.assertFalse(result["behavior_verified"])
        self.assertEqual(result["changes"][0]["kind"], "modified")

    def test_new_removed_and_out_of_scope_files(self):
        guard.snapshot(self.root, "pilot", ["new.ts"])
        (self.root / "new.ts").write_text("export const value = 1\n")
        (self.root / "existing.js").unlink()
        (self.root / "unrelated.js").write_text("must preserve\n")
        result, code = guard.check(self.root, "pilot")
        self.assertEqual(code, 1)
        self.assertEqual(result["violations"], ["existing.js", "unrelated.js"])
        self.assertEqual({item["kind"] for item in result["changes"]}, {"added", "removed"})

    def test_existing_untracked_files_are_protected(self):
        (self.root / "notes.txt").write_text("user notes\n")
        guard.snapshot(self.root, "pilot", ["existing.js"])
        (self.root / "notes.txt").write_text("changed\n")
        self.assertEqual(guard.check(self.root, "pilot")[0]["violations"], ["notes.txt"])

    def test_baseline_cannot_be_overwritten(self):
        guard.snapshot(self.root, "pilot", ["existing.js"])
        before = guard.baseline_path(self.root, "pilot").read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            guard.snapshot(self.root, "pilot", ["other.js"])
        self.assertEqual(guard.baseline_path(self.root, "pilot").read_bytes(), before)

    def test_scope_cannot_escape_or_include_metadata(self):
        for name in ["../outside", "C:/outside", "/outside", "*.js", ".git/config",
                     ".aipod-migration/pilot.baseline.json"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                guard.snapshot(self.root, "pilot", [name])
        with self.assertRaises(ValueError):
            guard.baseline_path(self.root, "../escape")

    def test_root_mismatch_fails_and_snapshots_do_not_store_contents(self):
        secret = "fixture-value-not-for-snapshot"
        (self.root / "existing.js").write_text(secret)
        guard.snapshot(self.root, "pilot", ["existing.js"])
        path = guard.baseline_path(self.root, "pilot")
        self.assertNotIn(secret, path.read_text())
        record = json.loads(path.read_text())
        record["root"] = str(self.root.parent)
        path.write_text(json.dumps(record))
        with self.assertRaisesRegex(ValueError, "does not match"):
            guard.check(self.root, "pilot")

    def test_ignored_outputs_do_not_affect_source_check(self):
        (self.root / ".gitignore").write_text("build/\n")
        guard.snapshot(self.root, "pilot", ["existing.js"])
        (self.root / "build").mkdir()
        (self.root / "build/output.js").write_text("generated\n")
        self.assertEqual(guard.check(self.root, "pilot")[0]["changes"], [])


if __name__ == "__main__":
    unittest.main()
