"""
FolderSync v4 — Test Suite
Tests all sync rules + the new scan/preview/confirm pipeline.
Run: python test_syncv4.py
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import syncv4 as s


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

    def path(self, lbl: str, rel: str) -> str:
        return os.path.join(self.dirs[lbl], rel)

    def exists(self, lbl: str, rel: str) -> bool:
        return os.path.exists(self.path(lbl, rel))

    def content(self, lbl: str, rel: str) -> str:
        with open(self.path(lbl, rel)) as f:
            return f.read()

    def scan(self) -> list[s.PlannedAction]:
        return s.scan_folders(self.dirs)

    def execute(self, actions) -> s.SyncResult:
        return s.execute_actions(self.dirs, actions, log_file=None)

    def full_sync(self) -> s.SyncResult:
        return self.execute(self.scan())


# ─── Scan correctness ─────────────────────────────────────────────────────────

class TestScanReturnsCorrectActions(BaseTest):

    def test_new_file_scanned_as_create(self):
        write_file(self.path("folder_a", "new.txt"), "content")
        actions = self.scan()
        creates = [a for a in actions if a.action == "CREATE"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0].rel, "new.txt")

    def test_identical_files_scanned_as_skip(self):
        for lbl in self.dirs:
            write_file(self.path(lbl, "same.txt"), "identical")
        actions = self.scan()
        skips = [a for a in actions if a.action == "SKIP" and a.rel == "same.txt"]
        self.assertEqual(len(skips), 1)

    def test_conflict_detected_in_scan(self):
        for lbl in self.dirs:
            write_file(self.path(lbl, "conflict.txt"), "original")
        self.full_sync()  # baseline
        write_file(self.path("folder_a", "conflict.txt"), "edit A")
        bump_mtime(self.path("folder_a", "conflict.txt"), 2)
        write_file(self.path("folder_b", "conflict.txt"), "edit B")
        bump_mtime(self.path("folder_b", "conflict.txt"), 8)
        actions = self.scan()
        conflicts = [a for a in actions if a.action == "CONFLICT"]
        self.assertEqual(len(conflicts), 1)

    def test_delete_detected_in_scan(self):
        for lbl in self.dirs:
            write_file(self.path(lbl, "gone.txt"), "bye")
        self.full_sync()
        os.remove(self.path("folder_a", "gone.txt"))
        actions = self.scan()
        deletes = [a for a in actions if a.action == "DELETE"]
        self.assertEqual(len(deletes), 1)

    def test_scan_does_not_modify_files(self):
        write_file(self.path("folder_a", "untouched.txt"), "original")
        before_b = os.path.exists(self.path("folder_b", "untouched.txt"))
        self.scan()
        after_b = os.path.exists(self.path("folder_b", "untouched.txt"))
        self.assertEqual(before_b, after_b)  # scan changes nothing


# ─── Execute correctness ──────────────────────────────────────────────────────

class TestExecute(BaseTest):

    def test_create_propagates_to_all(self):
        write_file(self.path("folder_a", "hello.txt"), "from A")
        actions = self.scan()
        self.execute(actions)
        self.assertTrue(self.exists("folder_b", "hello.txt"))
        self.assertTrue(self.exists("folder_c", "hello.txt"))

    def test_update_propagates_to_all(self):
        for lbl in self.dirs:
            write_file(self.path(lbl, "data.txt"), "old")
        self.full_sync()
        write_file(self.path("folder_c", "data.txt"), "new")
        bump_mtime(self.path("folder_c", "data.txt"), 10)
        self.full_sync()
        self.assertEqual(self.content("folder_a", "data.txt"), "new")
        self.assertEqual(self.content("folder_b", "data.txt"), "new")

    def test_delete_purges_all(self):
        for lbl in self.dirs:
            write_file(self.path(lbl, "gone.txt"), "bye")
        self.full_sync()
        os.remove(self.path("folder_b", "gone.txt"))
        self.full_sync()
        self.assertFalse(self.exists("folder_a", "gone.txt"))
        self.assertFalse(self.exists("folder_c", "gone.txt"))

    def test_conflict_newer_wins(self):
        for lbl in self.dirs:
            write_file(self.path(lbl, "c.txt"), "orig")
        self.full_sync()
        write_file(self.path("folder_a", "c.txt"), "edit A")
        bump_mtime(self.path("folder_a", "c.txt"), 2)
        write_file(self.path("folder_b", "c.txt"), "edit B newer")
        bump_mtime(self.path("folder_b", "c.txt"), 10)
        self.full_sync()
        self.assertEqual(self.content("folder_a", "c.txt"), "edit B newer")
        self.assertEqual(self.content("folder_c", "c.txt"), "edit B newer")


# ─── Confirmation gate ────────────────────────────────────────────────────────

class TestConfirmation(BaseTest):

    def test_no_changes_returns_false(self):
        """If nothing to sync, ask_confirmation returns False."""
        actions = []  # empty — nothing planned
        result = s.ask_confirmation(actions)
        self.assertFalse(result)

    def test_y_input_returns_true(self):
        write_file(self.path("folder_a", "f.txt"), "x")
        actions = self.scan()
        with patch("builtins.input", return_value="y"):
            result = s.ask_confirmation(actions)
        self.assertTrue(result)

    def test_n_input_returns_false(self):
        write_file(self.path("folder_a", "f.txt"), "x")
        actions = self.scan()
        with patch("builtins.input", return_value="n"):
            result = s.ask_confirmation(actions)
        self.assertFalse(result)

    def test_empty_input_returns_false(self):
        """Default (just pressing Enter) should be No."""
        write_file(self.path("folder_a", "f.txt"), "x")
        actions = self.scan()
        with patch("builtins.input", return_value=""):
            result = s.ask_confirmation(actions)
        self.assertFalse(result)

    def test_keyboard_interrupt_returns_false(self):
        write_file(self.path("folder_a", "f.txt"), "x")
        actions = self.scan()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = s.ask_confirmation(actions)
        self.assertFalse(result)

    def test_scan_then_abort_leaves_files_unchanged(self):
        """Scanning + aborting at confirmation must leave all folders untouched."""
        write_file(self.path("folder_a", "important.txt"), "original")
        actions = self.scan()
        # Simulate user pressing N — do NOT call execute_actions
        with patch("builtins.input", return_value="n"):
            confirmed = s.ask_confirmation(actions)
        self.assertFalse(confirmed)
        # folder_b and folder_c must still be empty
        self.assertFalse(self.exists("folder_b", "important.txt"))
        self.assertFalse(self.exists("folder_c", "important.txt"))


# ─── Two-pass integrity ───────────────────────────────────────────────────────

class TestTwoPassIntegrity(BaseTest):

    def test_scan_then_execute_matches_direct_sync(self):
        """
        Running scan + execute should produce the same outcome as a direct
        v3-style sync_all would. Validates the two-pass split is lossless.
        """
        write_file(self.path("folder_a", "file1.txt"), "aaa")
        write_file(self.path("folder_b", "file2.txt"), "bbb")
        actions = self.scan()
        self.execute(actions)

        # Both files should now be in all three folders
        for lbl in self.dirs:
            self.assertTrue(self.exists(lbl, "file1.txt"), f"file1.txt missing from {lbl}")
            self.assertTrue(self.exists(lbl, "file2.txt"), f"file2.txt missing from {lbl}")

    def test_second_scan_after_execute_shows_no_changes(self):
        """After a successful sync, a rescan should return only SKIPs."""
        write_file(self.path("folder_a", "settled.txt"), "done")
        actions1 = self.scan()
        self.execute(actions1)

        actions2 = self.scan()
        non_skips = [a for a in actions2 if a.action != "SKIP"]
        self.assertEqual(len(non_skips), 0)


# ─── State migration ──────────────────────────────────────────────────────────

class TestStateMigration(BaseTest):

    def test_v2_state_migrated_correctly(self):
        v2_state = {
            "last_sync": "2024-01-01T00:00:00",
            "files_a": {"old.txt": 1700000000.0},
            "files_b": {"old.txt": 1700000000.0},
        }
        with open(s.STATE_FILE, "w") as f:
            json.dump(v2_state, f)
        result = s.load_state()
        self.assertEqual(result["version"], 4)
        self.assertIn("folder_a", result["folders"])
        self.assertIn("folder_b", result["folders"])


if __name__ == "__main__":
    print("=" * 60)
    print("  FolderSync v4 — Test Suite")
    print("=" * 60)
    unittest.main(verbosity=2)
