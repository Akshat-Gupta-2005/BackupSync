"""
FolderSync — Two-way folder synchronization engine
Handles: CREATE, UPDATE, DELETE, CONFLICT resolution
"""

import os
import sys
import json
import shutil
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime

# ─── Logging Setup ────────────────────────────────────────────────────────────

def setup_logging(log_file: str = "sync.log", verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)

log = logging.getLogger(__name__)

# ─── State Management ─────────────────────────────────────────────────────────

STATE_FILE = "sync_state.json"

def load_state() -> dict:
    """Load the last sync snapshot from disk."""
    if not os.path.exists(STATE_FILE):
        return {"last_sync": None, "files_a": {}, "files_b": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(folder_a: str, folder_b: str):
    """Snapshot current state of both folders after a successful sync."""
    state = {
        "last_sync": datetime.now().isoformat(),
        "files_a": snapshot_folder(folder_a),
        "files_b": snapshot_folder(folder_b),
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    log.debug("State saved to sync_state.json")

def snapshot_folder(folder: str) -> dict:
    """Return {relative_path: mtime} for every file in folder."""
    snap = {}
    base = Path(folder)
    for p in base.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(base))
            snap[rel] = p.stat().st_mtime
    return snap

# ─── File Utilities ───────────────────────────────────────────────────────────

def get_mtime(path: str) -> float:
    return os.path.getmtime(path)

def file_hash(path: str) -> str:
    """MD5 hash of a file — used to skip identical files even if mtimes differ."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def safe_copy(src: str, dst: str):
    """Copy src → dst, creating intermediate directories as needed."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)  # copy2 preserves timestamps

def safe_delete(path: str):
    """Delete a file if it exists."""
    if os.path.exists(path):
        os.remove(path)
        # Clean up empty parent directories
        parent = os.path.dirname(path)
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.removedirs(parent)
        except OSError:
            pass

# ─── Core Sync Logic ──────────────────────────────────────────────────────────

class SyncResult:
    def __init__(self):
        self.created = []
        self.updated = []
        self.deleted = []
        self.conflicts = []
        self.skipped = []
        self.errors = []

    def summary(self):
        return (
            f"\n{'─'*50}\n"
            f"  ✅  Created   : {len(self.created)}\n"
            f"  🔄  Updated   : {len(self.updated)}\n"
            f"  🗑️   Deleted   : {len(self.deleted)}\n"
            f"  ⚠️   Conflicts : {len(self.conflicts)}\n"
            f"  ⏭️   Skipped   : {len(self.skipped)}\n"
            f"  ❌  Errors    : {len(self.errors)}\n"
            f"{'─'*50}"
        )


def sync_folders(folder_a: str, folder_b: str, dry_run: bool = False) -> SyncResult:
    """
    Main sync engine. Applies all 4 rules:
      CREATE   — file in A not in B (or vice versa) → copy to other
      UPDATE   — file in both, one is newer → overwrite older
      DELETE   — file deleted from one since last sync → delete from other
      CONFLICT — file modified in both since last sync → newer wins
    """
    result = SyncResult()
    state = load_state()
    prev_a = state.get("files_a", {})
    prev_b = state.get("files_b", {})
    first_run = state["last_sync"] is None

    path_a = Path(folder_a)
    path_b = Path(folder_b)

    if not path_a.exists():
        log.error(f"Folder A does not exist: {folder_a}")
        sys.exit(1)
    if not path_b.exists():
        log.error(f"Folder B does not exist: {folder_b}")
        sys.exit(1)

    # Current snapshots
    curr_a = snapshot_folder(folder_a)
    curr_b = snapshot_folder(folder_b)

    all_files = set(curr_a.keys()) | set(curr_b.keys()) | set(prev_a.keys()) | set(prev_b.keys())

    log.info(f"Syncing: '{folder_a}'  ↔  '{folder_b}'")
    log.info(f"Files to evaluate: {len(all_files)}  |  First run: {first_run}")

    for rel in sorted(all_files):
        file_a = str(path_a / rel)
        file_b = str(path_b / rel)

        in_a = rel in curr_a
        in_b = rel in curr_b
        was_in_a = rel in prev_a
        was_in_b = rel in prev_b

        try:
            # ── CASE 1: EXISTS IN BOTH ──────────────────────────────────────
            if in_a and in_b:
                mtime_a = curr_a[rel]
                mtime_b = curr_b[rel]

                # Skip if identical content
                if file_hash(file_a) == file_hash(file_b):
                    log.debug(f"  SKIP (identical)     {rel}")
                    result.skipped.append(rel)
                    continue

                if not first_run:
                    changed_a = was_in_a and (mtime_a != prev_a.get(rel, mtime_a))
                    changed_b = was_in_b and (mtime_b != prev_b.get(rel, mtime_b))
                else:
                    changed_a = changed_b = False

                if changed_a and changed_b:
                    # CONFLICT — both modified since last sync
                    winner = "A" if mtime_a >= mtime_b else "B"
                    if winner == "A":
                        log.warning(f"  CONFLICT → A wins    {rel}")
                        if not dry_run:
                            safe_copy(file_a, file_b)
                    else:
                        log.warning(f"  CONFLICT → B wins    {rel}")
                        if not dry_run:
                            safe_copy(file_b, file_a)
                    result.conflicts.append(f"{rel} (winner: {winner})")
                else:
                    # UPDATE — simple newer-wins
                    if mtime_a >= mtime_b:
                        log.info(f"  UPDATE  A → B        {rel}")
                        if not dry_run:
                            safe_copy(file_a, file_b)
                    else:
                        log.info(f"  UPDATE  B → A        {rel}")
                        if not dry_run:
                            safe_copy(file_b, file_a)
                    result.updated.append(rel)

            # ── CASE 2: ONLY IN A ───────────────────────────────────────────
            elif in_a and not in_b:
                if not first_run and was_in_b and not in_b:
                    # DELETE — file was in B before, now gone → delete from A
                    log.info(f"  DELETE  from A       {rel}")
                    if not dry_run:
                        safe_delete(file_a)
                    result.deleted.append(rel)
                else:
                    # CREATE — new file in A, copy to B
                    log.info(f"  CREATE  A → B        {rel}")
                    if not dry_run:
                        safe_copy(file_a, file_b)
                    result.created.append(rel)

            # ── CASE 3: ONLY IN B ───────────────────────────────────────────
            elif in_b and not in_a:
                if not first_run and was_in_a and not in_a:
                    # DELETE — file was in A before, now gone → delete from B
                    log.info(f"  DELETE  from B       {rel}")
                    if not dry_run:
                        safe_delete(file_b)
                    result.deleted.append(rel)
                else:
                    # CREATE — new file in B, copy to A
                    log.info(f"  CREATE  B → A        {rel}")
                    if not dry_run:
                        safe_copy(file_b, file_a)
                    result.created.append(rel)

            # ── CASE 4: IN NEITHER (was deleted from both) ──────────────────
            else:
                log.debug(f"  SKIP (gone from both) {rel}")

        except Exception as e:
            log.error(f"  ERROR   {rel}: {e}")
            result.errors.append(f"{rel}: {e}")

    if not dry_run:
        save_state(folder_a, folder_b)

    return result


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="FolderSync — Two-way folder sync with CREATE/UPDATE/DELETE/CONFLICT handling",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("folder_a", nargs="?", help="Path to Folder A")
    parser.add_argument("folder_b", nargs="?", help="Path to Folder B")
    parser.add_argument("--dry-run", action="store_true", help="Simulate sync without making changes")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show debug output")
    parser.add_argument("--log", default="sync.log", help="Log file path (default: sync.log)")
    parser.add_argument("--config", default="config.json", help="Config file path (default: config.json)")
    parser.add_argument("--reset-state", action="store_true", help="Delete sync_state.json and treat as first run")
    args = parser.parse_args()

    setup_logging(args.log, args.verbose)

    if args.reset_state and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        log.info("State reset. Next sync will be treated as first run.")

    # Resolve folders from args or config file
    folder_a = args.folder_a
    folder_b = args.folder_b

    if not folder_a or not folder_b:
        if os.path.exists(args.config):
            with open(args.config, "r") as f:
                cfg = json.load(f)
            folder_a = folder_a or cfg.get("folder_a")
            folder_b = folder_b or cfg.get("folder_b")
        
        if not folder_a or not folder_b:
            log.error("Please provide folder_a and folder_b as arguments or in config.json")
            parser.print_help()
            sys.exit(1)

    if args.dry_run:
        log.info("DRY RUN MODE — no files will be changed")

    result = sync_folders(folder_a, folder_b, dry_run=args.dry_run)
    print(result.summary())

    if result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
