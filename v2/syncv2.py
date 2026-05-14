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

LOG_WIDTH = 72  # Total width of the log file columns

class FileLogFormatter(logging.Formatter):
    """
    Structured formatter for the .log file.
    Produces clean, aligned, human-readable output with session blocks.

    Example output:
      ┌──────────────────────────────────────────────────────────────────────┐
      │  FOLDERSYNC SESSION  ·  2024-03-15 14:32:01                         │
      ├──────────────────────────────────────────────────────────────────────┤
      │  A  →  /home/user/docs                                               │
      │  B  →  /home/user/backup                                             │
      └──────────────────────────────────────────────────────────────────────┘
      14:32:01  CREATE    notes.txt                            A → B
      14:32:01  UPDATE    report.pdf                           B → A
      14:32:01  CONFLICT  budget.xlsx            [CONFLICT]    A wins
      14:32:01  DELETE    old_draft.txt                        from B
      14:32:01  INFO      State saved to sync_state.json
      14:32:01  ERROR     broken.zip: Permission denied
    """

    # Labels padded to fixed width so columns stay aligned
    LEVEL_LABELS = {
        "DEBUG":    "DEBUG   ",
        "INFO":     "INFO    ",
        "WARNING":  "WARNING ",
        "ERROR":    "ERROR   ",
        "CRITICAL": "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, "%H:%M:%S")
        label = self.LEVEL_LABELS.get(record.levelname, record.levelname.ljust(8))
        msg = record.getMessage()
        return f"  {time_str}  {label}  {msg}"


class ConsoleLogFormatter(logging.Formatter):
    """Minimal formatter for stdout — timestamp + message only."""

    COLORS = {
        "DEBUG":    "\033[90m",   # dark grey
        "INFO":     "\033[0m",    # default
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[1;31m", # bold red
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, "%H:%M:%S")
        color = self.COLORS.get(record.levelname, "")
        msg = record.getMessage()
        return f"{color}{time_str}  {msg}{self.RESET}"


def _box_line(content: str = "", width: int = LOG_WIDTH, left: str = "│", right: str = "│") -> str:
    inner = width - 2  # subtract left + right border chars
    return f"{left}  {content:<{inner - 2}}{right}"


def write_session_header(log_file: str, folder_a: str, folder_b: str, dry_run: bool = False):
    """Write a formatted session-start block directly to the log file."""
    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    mode = "  [DRY RUN — no files will be changed]" if dry_run else ""
    top    = "┌" + "─" * (LOG_WIDTH - 2) + "┐"
    sep    = "├" + "─" * (LOG_WIDTH - 2) + "┤"
    bottom = "└" + "─" * (LOG_WIDTH - 2) + "┘"

    lines = [
        "",
        top,
        _box_line(f"FOLDERSYNC SESSION  ·  {now}{mode}"),
        sep,
        _box_line(f"A  →  {folder_a}"),
        _box_line(f"B  →  {folder_b}"),
        bottom,
    ]
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_session_footer(log_file: str, result: "SyncResult", elapsed: float):
    """Write a formatted session-end summary block to the log file."""
    sep    = "  " + "─" * (LOG_WIDTH - 2)
    lines = [
        sep,
        f"  {"RESULT":<12}  Created={len(result.created)}  Updated={len(result.updated)}  "
        f"Deleted={len(result.deleted)}  Conflicts={len(result.conflicts)}  "
        f"Skipped={len(result.skipped)}  Errors={len(result.errors)}",
        f"  {"ELAPSED":<12}  {elapsed:.3f}s",
        sep,
        "",
    ]
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def setup_logging(log_file: str = "sync.log", verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ConsoleLogFormatter())
    console_handler.setLevel(level)

    handlers: list[logging.Handler] = [console_handler]

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(FileLogFormatter())
        file_handler.setLevel(logging.DEBUG)  # always capture everything in file
        handlers.append(file_handler)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for h in handlers:
        root.addHandler(h)


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


def _log_action(action: str, filename: str, detail: str = ""):
    """
    Emit a fixed-width action line so all columns stay aligned in the log.

    Output format (inside the formatter's timestamp + level prefix):
      CREATE    notes.txt                                    A → B
      UPDATE    report.pdf                                   B → A
      CONFLICT  budget.xlsx                     [CONFLICT]   A wins
      DELETE    old_draft.txt                               from B
      SKIP      unchanged.png                              identical
    """
    col_action = f"{action:<10}"   # 10 chars: CREATE, UPDATE, DELETE, CONFLICT, SKIP
    col_file   = f"{filename:<44}" # 44 chars for filename (truncated if needed)
    col_detail = detail
    msg = f"{col_action}{col_file}{col_detail}"
    if action == "CONFLICT":
        log.warning(msg)
    elif action in ("ERROR",):
        log.error(msg)
    elif action == "SKIP":
        log.debug(msg)
    else:
        log.info(msg)


def sync_folders(
    folder_a: str,
    folder_b: str,
    dry_run: bool = False,
    log_file: str = "sync.log",
) -> "SyncResult":
    """
    Main sync engine. Applies all 4 rules:
      CREATE   — file in A not in B (or vice versa) → copy to other
      UPDATE   — file in both, one is newer → overwrite older
      DELETE   — file deleted from one since last sync → delete from other
      CONFLICT — file modified in both since last sync → newer wins
    """
    import time
    start_time = time.monotonic()

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

    # Write session header block to log file
    if log_file:
        write_session_header(log_file, folder_a, folder_b, dry_run)

    log.info(f"{'Files evaluated':<44}scanning...")

    # Current snapshots
    curr_a = snapshot_folder(folder_a)
    curr_b = snapshot_folder(folder_b)
    all_files = set(curr_a.keys()) | set(curr_b.keys()) | set(prev_a.keys()) | set(prev_b.keys())

    log.info(f"{'Files evaluated':<44}{len(all_files)} files  |  first_run={first_run}")

    for rel in sorted(all_files):
        file_a = str(path_a / rel)
        file_b = str(path_b / rel)

        in_a = rel in curr_a
        in_b = rel in curr_b
        was_in_a = rel in prev_a
        was_in_b = rel in prev_b

        # Truncate long filenames in log so columns don't break
        display = rel if len(rel) <= 43 else "…" + rel[-(42):]

        try:
            # ── CASE 1: EXISTS IN BOTH ──────────────────────────────────────
            if in_a and in_b:
                mtime_a = curr_a[rel]
                mtime_b = curr_b[rel]

                if file_hash(file_a) == file_hash(file_b):
                    _log_action("SKIP", display, "identical")
                    result.skipped.append(rel)
                    continue

                if not first_run:
                    changed_a = was_in_a and (mtime_a != prev_a.get(rel, mtime_a))
                    changed_b = was_in_b and (mtime_b != prev_b.get(rel, mtime_b))
                else:
                    changed_a = changed_b = False

                if changed_a and changed_b:
                    winner = "A" if mtime_a >= mtime_b else "B"
                    _log_action("CONFLICT", display, f"[CONFLICT]  {winner} wins")
                    if not dry_run:
                        safe_copy(file_a, file_b) if winner == "A" else safe_copy(file_b, file_a)
                    result.conflicts.append(f"{rel} (winner: {winner})")
                else:
                    if mtime_a >= mtime_b:
                        _log_action("UPDATE", display, "A → B")
                        if not dry_run:
                            safe_copy(file_a, file_b)
                    else:
                        _log_action("UPDATE", display, "B → A")
                        if not dry_run:
                            safe_copy(file_b, file_a)
                    result.updated.append(rel)

            # ── CASE 2: ONLY IN A ───────────────────────────────────────────
            elif in_a and not in_b:
                if not first_run and was_in_b and not in_b:
                    _log_action("DELETE", display, "removed from B  →  purged from A")
                    if not dry_run:
                        safe_delete(file_a)
                    result.deleted.append(rel)
                else:
                    _log_action("CREATE", display, "A → B")
                    if not dry_run:
                        safe_copy(file_a, file_b)
                    result.created.append(rel)

            # ── CASE 3: ONLY IN B ───────────────────────────────────────────
            elif in_b and not in_a:
                if not first_run and was_in_a and not in_a:
                    _log_action("DELETE", display, "removed from A  →  purged from B")
                    if not dry_run:
                        safe_delete(file_b)
                    result.deleted.append(rel)
                else:
                    _log_action("CREATE", display, "B → A")
                    if not dry_run:
                        safe_copy(file_b, file_a)
                    result.created.append(rel)

            # ── CASE 4: IN NEITHER (was deleted from both) ──────────────────
            else:
                _log_action("SKIP", display, "gone from both")

        except Exception as e:
            _log_action("ERROR", display, str(e))
            result.errors.append(f"{rel}: {e}")

    if not dry_run:
        save_state(folder_a, folder_b)

    elapsed = time.monotonic() - start_time
    if log_file:
        write_session_footer(log_file, result, elapsed)

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

    result = sync_folders(folder_a, folder_b, dry_run=args.dry_run, log_file=args.log)
    print(result.summary())

    if result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
