"""
FolderSync v3 — Test Suite
Tests all 4 sync rules across 3 folders (A, B, C).
Run: python test_syncv3.py
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path

import syncv3 as s


def write_file(path: str, content: str = "hello", mtime_offset: float = 0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mtime_offset:
        t = time.time() + mtime_offset
        os.utime(path, (t, t))


def bump_mtime(path: str, offset: float = 5.0):
    t = os.path.getmtime(path) + offset
    os.utime(path, (t, t))


class BaseTest(unittest.TestCase):
    """Creates 3 temp folders and wires up syncv3 state file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dirs = {}
        for lbl in ("folder_a", "folder_b", "folder_c"):
            p = os.path.join(self.tmp, lbl)
            os.makedirs(p)
            self.dirs[lbl] = p
        s.STATE_FILE = os.path.join(self.tmp, "sync_state.json")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def sync(self, **kwargs) -> s.SyncResult:
        return s.sync_all(self.dirs, log_file=None, **kwargs)

    def path(self, folder_label: str, rel: str) -> str:
        return os.path.join(self.dirs[folder_label], rel)

    def exists(self, folder_label: str, rel: str) -> bool:
        return os.path.exists(self.path(folder_label, rel))

    def content(self, folder_label: str, rel: str) -> str:
        with open(self.path(folder_label, rel)) as f:
            return f.read()


# ─── CREATE ───────────────────────────────────────────────────────────────────

class TestCreate(BaseTest):

    def test_new_file_in_a_copies_to_b_and_c(self):
        write_file(self.path("folder_a", "hello.txt"), "from A")
        r = self.sync()
        self.assertIn("hello.txt", [x[0] for x in r.created])
        self.assertTrue(self.exists("folder_b", "hello.txt"))
        self.assertTrue(self.exists("folder_c", "hello.txt"))
        self.assertEqual(self.content("folder_b", "hello.txt"), "from A")
        self.assertEqual(self.content("folder_c", "hello.txt"), "from A")

    def test_new_file_in_b_copies_to_a_and_c(self):
        write_file(self.path("folder_b", "world.txt"), "from B")
        r = self.sync()
        self.assertTrue(self.exists("folder_a", "world.txt"))
        self.assertTrue(self.exists("folder_c", "world.txt"))

    def test_new_file_in_c_copies_to_a_and_b(self):
        write_file(self.path("folder_c", "side.txt"), "from C")
        r = self.sync()
        self.assertTrue(self.exists("folder_a", "side.txt"))
        self.assertTrue(self.exists("folder_b", "side.txt"))

    def test_nested_file_copies_with_full_path(self):
        write_file(self.path("folder_a", "sub/deep/file.txt"), "nested")
        self.sync()
        self.assertTrue(self.exists("folder_b", "sub/deep/file.txt"))
        self.assertTrue(self.exists("folder_c", "sub/deep/file.txt"))

    def test_file_in_two_folders_copies_to_third(self):
        """File already in A and B — should be created in C."""
        write_file(self.path("folder_a", "partial.txt"), "data")
        write_file(self.path("folder_b", "partial.txt"), "data")
        r = self.sync()
        self.assertTrue(self.exists("folder_c", "partial.txt"))

    def test_newest_source_wins_on_create(self):
        """File in A and B with different content — C should get the newest."""
        write_file(self.path("folder_a", "source.txt"), "older version")
        write_file(self.path("folder_b", "source.txt"), "newer version", mtime_offset=10)
        self.sync()
        self.assertEqual(self.content("folder_c", "source.txt"), "newer version")


# ─── UPDATE ───────────────────────────────────────────────────────────────────

class TestUpdate(BaseTest):

    def _baseline(self, filename="data.txt", content="original"):
        for lbl in self.dirs:
            write_file(self.path(lbl, filename), content)
        self.sync()  # establish state

    def test_update_from_a_propagates_to_b_and_c(self):
        self._baseline()
        write_file(self.path("folder_a", "data.txt"), "updated in A")
        bump_mtime(self.path("folder_a", "data.txt"), 10)
        self.sync()
        self.assertEqual(self.content("folder_b", "data.txt"), "updated in A")
        self.assertEqual(self.content("folder_c", "data.txt"), "updated in A")

    def test_update_from_c_propagates_to_a_and_b(self):
        self._baseline()
        write_file(self.path("folder_c", "data.txt"), "updated in C")
        bump_mtime(self.path("folder_c", "data.txt"), 10)
        self.sync()
        self.assertEqual(self.content("folder_a", "data.txt"), "updated in C")
        self.assertEqual(self.content("folder_b", "data.txt"), "updated in C")

    def test_identical_files_are_skipped(self):
        self._baseline("same.txt", "identical content")
        r = self.sync()
        self.assertIn("same.txt", r.skipped)
        self.assertEqual(len(r.updated), 0)


# ─── DELETE ───────────────────────────────────────────────────────────────────

class TestDelete(BaseTest):

    def _baseline_all(self, filename="gone.txt"):
        for lbl in self.dirs:
            write_file(self.path(lbl, filename), "will be deleted")
        self.sync()

    def test_delete_from_a_purges_b_and_c(self):
        self._baseline_all()
        os.remove(self.path("folder_a", "gone.txt"))
        r = self.sync()
        self.assertFalse(self.exists("folder_b", "gone.txt"))
        self.assertFalse(self.exists("folder_c", "gone.txt"))
        self.assertIn("gone.txt", [x[0] for x in r.deleted])

    def test_delete_from_b_purges_a_and_c(self):
        self._baseline_all()
        os.remove(self.path("folder_b", "gone.txt"))
        r = self.sync()
        self.assertFalse(self.exists("folder_a", "gone.txt"))
        self.assertFalse(self.exists("folder_c", "gone.txt"))

    def test_delete_from_c_purges_a_and_b(self):
        self._baseline_all()
        os.remove(self.path("folder_c", "gone.txt"))
        r = self.sync()
        self.assertFalse(self.exists("folder_a", "gone.txt"))
        self.assertFalse(self.exists("folder_b", "gone.txt"))

    def test_no_delete_inferred_on_first_run(self):
        """File only in folder_a on first run → CREATE to others, not DELETE."""
        write_file(self.path("folder_a", "only_a.txt"), "new file")
        r = self.sync()
        self.assertEqual(len(r.deleted), 0)
        self.assertTrue(self.exists("folder_b", "only_a.txt"))
        self.assertTrue(self.exists("folder_c", "only_a.txt"))

    def test_simultaneous_delete_from_two_folders(self):
        """File deleted from A and B — should also be deleted from C."""
        self._baseline_all("multi_del.txt")
        os.remove(self.path("folder_a", "multi_del.txt"))
        os.remove(self.path("folder_b", "multi_del.txt"))
        r = self.sync()
        self.assertFalse(self.exists("folder_c", "multi_del.txt"))


# ─── CONFLICT ─────────────────────────────────────────────────────────────────

class TestConflict(BaseTest):

    def _baseline_all(self, filename="conflict.txt"):
        for lbl in self.dirs:
            write_file(self.path(lbl, filename), "original")
        self.sync()

    def test_conflict_two_folders_newer_wins(self):
        """A and B both edited — B is newer, so B's version wins everywhere."""
        self._baseline_all()
        write_file(self.path("folder_a", "conflict.txt"), "edit in A")
        bump_mtime(self.path("folder_a", "conflict.txt"), 2)
        write_file(self.path("folder_b", "conflict.txt"), "edit in B - newer")
        bump_mtime(self.path("folder_b", "conflict.txt"), 8)
        r = self.sync()
        self.assertTrue(len(r.conflicts) > 0 or len(r.updated) > 0)
        self.assertEqual(self.content("folder_a", "conflict.txt"), "edit in B - newer")
        self.assertEqual(self.content("folder_c", "conflict.txt"), "edit in B - newer")

    def test_conflict_three_way_newest_wins(self):
        """All three folders edited — C is newest, C wins."""
        self._baseline_all("three_way.txt")
        write_file(self.path("folder_a", "three_way.txt"), "edit in A")
        bump_mtime(self.path("folder_a", "three_way.txt"), 1)
        write_file(self.path("folder_b", "three_way.txt"), "edit in B")
        bump_mtime(self.path("folder_b", "three_way.txt"), 3)
        write_file(self.path("folder_c", "three_way.txt"), "edit in C - newest")
        bump_mtime(self.path("folder_c", "three_way.txt"), 10)
        self.sync()
        self.assertEqual(self.content("folder_a", "three_way.txt"), "edit in C - newest")
        self.assertEqual(self.content("folder_b", "three_way.txt"), "edit in C - newest")


# ─── DRY RUN ──────────────────────────────────────────────────────────────────

class TestDryRun(BaseTest):

    def test_dry_run_does_not_create_files(self):
        write_file(self.path("folder_a", "dry.txt"), "content")
        r = s.sync_all(self.dirs, log_file=None, dry_run=True)
        self.assertFalse(self.exists("folder_b", "dry.txt"))
        self.assertFalse(self.exists("folder_c", "dry.txt"))
        self.assertIn("dry.txt", [x[0] for x in r.created])

    def test_dry_run_does_not_delete_files(self):
        for lbl in self.dirs:
            write_file(self.path(lbl, "keep.txt"), "content")
        s.sync_all(self.dirs, log_file=None)  # baseline

        os.remove(self.path("folder_a", "keep.txt"))
        s.sync_all(self.dirs, log_file=None, dry_run=True)
        self.assertTrue(self.exists("folder_b", "keep.txt"))
        self.assertTrue(self.exists("folder_c", "keep.txt"))


# ─── STATE MIGRATION ──────────────────────────────────────────────────────────

class TestStateMigration(BaseTest):

    def test_v2_state_migrated_correctly(self):
        """A v2 state file (files_a / files_b) should be silently upgraded."""
        v2_state = {
            "last_sync": "2024-01-01T00:00:00",
            "files_a": {"old.txt": 1700000000.0},
            "files_b": {"old.txt": 1700000000.0},
        }
        with open(s.STATE_FILE, "w") as f:
            json.dump(v2_state, f)

        result = s.load_state()
        self.assertEqual(result["version"], 3)
        self.assertIn("folder_a", result["folders"])
        self.assertIn("folder_b", result["folders"])


if __name__ == "__main__":
    print("=" * 60)
    print("  FolderSync v3 — Test Suite")
    print("=" * 60)
    unittest.main(verbosity=2)
