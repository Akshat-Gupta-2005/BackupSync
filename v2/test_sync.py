"""
FolderSync Test Suite
Tests all 4 sync rules: CREATE, UPDATE, DELETE, CONFLICT
Run: python test_sync.py
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path

# Patch STATE_FILE to a temp location during tests
import sync as sync_module

TEMP_STATE = None


def write_file(path: str, content: str = "hello", mtime_offset: float = 0):
    """Write a file and optionally shift its mtime."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mtime_offset:
        t = os.path.getmtime(path) + mtime_offset
        os.utime(path, (t, t))


class TestSync(unittest.TestCase):

    def setUp(self):
        global TEMP_STATE
        self.dir = tempfile.mkdtemp()
        self.a = os.path.join(self.dir, "A")
        self.b = os.path.join(self.dir, "B")
        os.makedirs(self.a)
        os.makedirs(self.b)
        TEMP_STATE = os.path.join(self.dir, "sync_state.json")
        sync_module.STATE_FILE = TEMP_STATE

    def tearDown(self):
        shutil.rmtree(self.dir)

    # ── RULE 1: CREATE ────────────────────────────────────────────────────────

    def test_create_a_to_b(self):
        """File in A but not B → should be copied to B."""
        write_file(os.path.join(self.a, "hello.txt"), "from A")
        result = sync_module.sync_folders(self.a, self.b)
        self.assertIn("hello.txt", result.created)
        self.assertTrue(os.path.exists(os.path.join(self.b, "hello.txt")))
        with open(os.path.join(self.b, "hello.txt")) as f:
            self.assertEqual(f.read(), "from A")

    def test_create_b_to_a(self):
        """File in B but not A → should be copied to A."""
        write_file(os.path.join(self.b, "world.txt"), "from B")
        result = sync_module.sync_folders(self.a, self.b)
        self.assertIn("world.txt", result.created)
        self.assertTrue(os.path.exists(os.path.join(self.a, "world.txt")))

    def test_create_nested(self):
        """Nested file in A should be created with full path in B."""
        write_file(os.path.join(self.a, "sub", "deep.txt"), "nested")
        result = sync_module.sync_folders(self.a, self.b)
        self.assertTrue(os.path.exists(os.path.join(self.b, "sub", "deep.txt")))

    # ── RULE 2: UPDATE ────────────────────────────────────────────────────────

    def test_update_a_newer(self):
        """Same file in both, A is newer → B should be overwritten."""
        write_file(os.path.join(self.a, "data.txt"), "old")
        write_file(os.path.join(self.b, "data.txt"), "old")
        # First sync to establish state
        sync_module.sync_folders(self.a, self.b)

        # Update A
        time.sleep(0.05)
        write_file(os.path.join(self.a, "data.txt"), "new from A")
        t = time.time() + 2
        os.utime(os.path.join(self.a, "data.txt"), (t, t))

        result = sync_module.sync_folders(self.a, self.b)
        self.assertTrue(len(result.updated) > 0 or len(result.skipped) == 0)
        with open(os.path.join(self.b, "data.txt")) as f:
            self.assertEqual(f.read(), "new from A")

    def test_update_b_newer(self):
        """Same file in both, B is newer → A should be overwritten."""
        write_file(os.path.join(self.a, "data.txt"), "old")
        write_file(os.path.join(self.b, "data.txt"), "old")
        sync_module.sync_folders(self.a, self.b)

        time.sleep(0.05)
        write_file(os.path.join(self.b, "data.txt"), "new from B")
        t = time.time() + 2
        os.utime(os.path.join(self.b, "data.txt"), (t, t))

        sync_module.sync_folders(self.a, self.b)
        with open(os.path.join(self.a, "data.txt")) as f:
            self.assertEqual(f.read(), "new from B")

    def test_skip_identical(self):
        """Same content in both → should be skipped (no copy)."""
        write_file(os.path.join(self.a, "same.txt"), "identical content")
        write_file(os.path.join(self.b, "same.txt"), "identical content")
        result = sync_module.sync_folders(self.a, self.b)
        self.assertIn("same.txt", result.skipped)

    # ── RULE 3: DELETE ────────────────────────────────────────────────────────

    def test_delete_from_b_propagates_to_a(self):
        """File deleted from B after sync → should be deleted from A too."""
        write_file(os.path.join(self.a, "gone.txt"), "will be deleted")
        sync_module.sync_folders(self.a, self.b)  # First sync: file in both

        os.remove(os.path.join(self.b, "gone.txt"))  # Delete from B
        result = sync_module.sync_folders(self.a, self.b)

        self.assertIn("gone.txt", result.deleted)
        self.assertFalse(os.path.exists(os.path.join(self.a, "gone.txt")))

    def test_delete_from_a_propagates_to_b(self):
        """File deleted from A after sync → should be deleted from B too."""
        write_file(os.path.join(self.b, "gone.txt"), "will be deleted")
        sync_module.sync_folders(self.a, self.b)

        os.remove(os.path.join(self.a, "gone.txt"))
        result = sync_module.sync_folders(self.a, self.b)

        self.assertIn("gone.txt", result.deleted)
        self.assertFalse(os.path.exists(os.path.join(self.b, "gone.txt")))

    def test_no_delete_on_first_run(self):
        """File only in A on first run should be CREATED to B, not deleted."""
        write_file(os.path.join(self.a, "only_a.txt"), "only in a")
        result = sync_module.sync_folders(self.a, self.b)
        self.assertIn("only_a.txt", result.created)
        self.assertNotIn("only_a.txt", result.deleted)

    # ── RULE 4: CONFLICT ─────────────────────────────────────────────────────

    def test_conflict_newer_wins(self):
        """Both files modified since last sync → newer timestamp wins."""
        write_file(os.path.join(self.a, "conflict.txt"), "original")
        write_file(os.path.join(self.b, "conflict.txt"), "original")
        sync_module.sync_folders(self.a, self.b)

        # Modify both, B gets newer timestamp
        write_file(os.path.join(self.a, "conflict.txt"), "edited in A")
        t_a = time.time() + 1
        os.utime(os.path.join(self.a, "conflict.txt"), (t_a, t_a))

        write_file(os.path.join(self.b, "conflict.txt"), "edited in B - newer")
        t_b = time.time() + 5  # B is newer
        os.utime(os.path.join(self.b, "conflict.txt"), (t_b, t_b))

        result = sync_module.sync_folders(self.a, self.b)
        self.assertTrue(len(result.conflicts) > 0 or len(result.updated) > 0)

        with open(os.path.join(self.a, "conflict.txt")) as f:
            content = f.read()
        # B was newer, so A should now have B's content
        self.assertEqual(content, "edited in B - newer")


class TestDryRun(unittest.TestCase):

    def setUp(self):
        global TEMP_STATE
        self.dir = tempfile.mkdtemp()
        self.a = os.path.join(self.dir, "A")
        self.b = os.path.join(self.dir, "B")
        os.makedirs(self.a)
        os.makedirs(self.b)
        sync_module.STATE_FILE = os.path.join(self.dir, "sync_state.json")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_dry_run_no_changes(self):
        """Dry run should not create any files."""
        write_file(os.path.join(self.a, "test.txt"), "content")
        result = sync_module.sync_folders(self.a, self.b, dry_run=True)
        self.assertIn("test.txt", result.created)
        self.assertFalse(os.path.exists(os.path.join(self.b, "test.txt")))


if __name__ == "__main__":
    print("=" * 50)
    print("  FolderSync Test Suite")
    print("=" * 50)
    unittest.main(verbosity=2)
