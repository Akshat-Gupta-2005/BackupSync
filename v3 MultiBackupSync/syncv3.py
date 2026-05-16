"""
FolderSync v3 — Multi-folder synchronization engine
Handles any number of folders defined in config.json.

Rules (scale from 2-folder to N-folder):
  CREATE   — file exists in some folders but not others → copy to all missing
  UPDATE   — file exists in all folders, contents differ → newest mtime wins, propagated to all
  DELETE   — file was in last snapshot for a folder, now gone → deleted from ALL folders
  CONFLICT — file edited in multiple folders since last sync → newest mtime wins across all
"""

import os
import sys
import json
import time
import shutil
import hashlib
import logging
import argparse
from pathlib import Path
from datetime import datetime

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_WIDTH = 90  # wider than v2 to accommodate N-folder detail strings

class FileLogFormatter(logging.Formatter):
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
        return f"  {time_str}  {label}  {record.getMessage()}"


class ConsoleLogFormatter(logging.Formatter):
    COLORS = {
        "DEBUG":    "\033[90m",
        "INFO":     "\033[0m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, "%H:%M:%S")
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{time_str}  {record.getMessage()}{self.RESET}"


def _box_line(content: str = "", width: int = LOG_WIDTH) -> str:
    inner = width - 2
    return f"│  {content:<{inner - 2}}│"


def write_session_header(log_file: str, folders: dict[str, str], dry_run: bool = False):
    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    mode = "  [DRY RUN]" if dry_run else ""
    top    = "┌" + "─" * (LOG_WIDTH - 2) + "┐"
    sep    = "├" + "─" * (LOG_WIDTH - 2) + "┤"
    bottom = "└" + "─" * (LOG_WIDTH - 2) + "┘"

    lines = ["", top, _box_line(f"FOLDERSYNC v3 SESSION  ·  {now}{mode}"), sep]
    for label, path in folders.items():
        lines.append(_box_line(f"{label.upper():<10}→  {path}"))
    lines += [bottom]

    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_session_footer(log_file: str, result: "SyncResult", elapsed: float):
    sep = "  " + "─" * (LOG_WIDTH - 2)
    lines = [
        sep,
        f"  {'RESULT':<12}  Created={len(result.created)}  Updated={len(result.updated)}  "
        f"Deleted={len(result.deleted)}  Conflicts={len(result.conflicts)}  "
        f"Skipped={len(result.skipped)}  Errors={len(result.errors)}",
        f"  {'ELAPSED':<12}  {elapsed:.3f}s",
        sep,
        "",
    ]
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def setup_logging(log_file: str = "sync.log", verbose: bool = False):
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ConsoleLogFormatter())
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console_handler)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(FileLogFormatter())
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)


log = logging.getLogger(__name__)

# ─── State ────────────────────────────────────────────────────────────────────

STATE_FILE = "sync_state.json"


def load_state() -> dict:
    """
    State schema (v3):
    {
      "last_sync": "<iso timestamp>",
      "version": 3,
      "folders": {
        "folder_a": { "rel/path.txt": <mtime_float>, ... },
        "folder_b": { ... },
        ...
      }
    }
    """
    if not os.path.exists(STATE_FILE):
        return {"last_sync": None, "version": 3, "folders": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    # Migrate v2 state (files_a / files_b) to v3 format transparently
    if state.get("version", 2) < 3:
        state = _migrate_v2_state(state)
    return state


def _migrate_v2_state(old: dict) -> dict:
    """Upgrade a v2 two-folder state file to v3 multi-folder format."""
    log.info("Migrating sync_state.json from v2 → v3 format")
    return {
        "last_sync": old.get("last_sync"),
        "version": 3,
        "folders": {
            "folder_a": old.get("files_a", {}),
            "folder_b": old.get("files_b", {}),
        },
    }


def save_state(folders: dict[str, str]):
    """Snapshot all folder states after a successful sync."""
    state = {
        "last_sync": datetime.now().isoformat(),
        "version": 3,
        "folders": {label: snapshot_folder(path) for label, path in folders.items()},
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

def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_copy(src: str, dst: str):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)


def safe_delete(path: str):
    if os.path.exists(path):
        os.remove(path)
        parent = os.path.dirname(path)
        try:
            if os.path.isdir(parent) and not os.listdir(parent):
                os.removedirs(parent)
        except OSError:
            pass

# ─── Logging helpers ──────────────────────────────────────────────────────────

def _log_action(action: str, filename: str, detail: str = ""):
    col_action = f"{action:<10}"
    col_file   = f"{filename:<46}"
    msg = f"{col_action}{col_file}{detail}"
    if action == "CONFLICT":
        log.warning(msg)
    elif action == "ERROR":
        log.error(msg)
    elif action == "SKIP":
        log.debug(msg)
    else:
        log.info(msg)

# ─── Result ───────────────────────────────────────────────────────────────────

class SyncResult:
    def __init__(self):
        self.created   = []   # (rel, copied_to_labels)
        self.updated   = []   # (rel, winner_label)
        self.deleted   = []   # (rel, deleted_from_labels)
        self.conflicts = []   # (rel, winner_label, all_changed_labels)
        self.skipped   = []   # rel
        self.errors    = []   # (rel, msg)

    def summary(self) -> str:
        return (
            f"\n{'─'*54}\n"
            f"  ✅  Created   : {len(self.created)}\n"
            f"  🔄  Updated   : {len(self.updated)}\n"
            f"  🗑️   Deleted   : {len(self.deleted)}\n"
            f"  ⚠️   Conflicts : {len(self.conflicts)}\n"
            f"  ⏭️   Skipped   : {len(self.skipped)}\n"
            f"  ❌  Errors    : {len(self.errors)}\n"
            f"{'─'*54}"
        )

# ─── Core Engine ──────────────────────────────────────────────────────────────

def sync_all(
    folders: dict[str, str],
    dry_run: bool = False,
    log_file: str = "sync.log",
) -> SyncResult:
    """
    Multi-folder sync engine.

    `folders` is an ordered dict of {label: absolute_path}, e.g.:
        {"folder_a": "/data/docs", "folder_b": "/backup", "folder_c": "/nas/docs"}

    Strategy per file:
      1. Collect presence + mtime across all folders.
      2. Determine the "winner" — the folder with the newest mtime among
         those that currently have the file.
      3. Apply CREATE / UPDATE / CONFLICT / DELETE / SKIP accordingly.

    DELETE semantics (N-folder):
      A deletion is inferred when a file appears in the previous snapshot
      for ANY folder but is now absent from that folder AND is NOT newly
      created elsewhere (i.e., it also existed in all other snapshots).
      When a deletion is confirmed, the file is removed from every folder
      that still holds it.

    CONFLICT semantics (N-folder):
      A conflict occurs when two or more folders each have a version of the
      file that changed since the last sync AND those versions differ in
      content. The folder with the newest mtime is the winner; its version
      is propagated to all others.
    """
    start = time.monotonic()
    result = SyncResult()

    # ── Validate all paths ───────────────────────────────────────────────────
    labels = list(folders.keys())
    for label, path in folders.items():
        if not Path(path).exists():
            log.error(f"Folder '{label}' does not exist: {path}")
            sys.exit(1)

    # ── Load state ───────────────────────────────────────────────────────────
    state      = load_state()
    first_run  = state["last_sync"] is None
    prev       = state.get("folders", {})          # {label: {rel: mtime}}

    # ── Session header ───────────────────────────────────────────────────────
    if log_file:
        write_session_header(log_file, folders, dry_run)

    # ── Snapshot current state ───────────────────────────────────────────────
    curr: dict[str, dict[str, float]] = {
        label: snapshot_folder(path) for label, path in folders.items()
    }

    # Union of all relative paths ever seen (current + previous)
    all_files: set[str] = set()
    for snap in curr.values():
        all_files |= snap.keys()
    for snap in prev.values():
        all_files |= snap.keys()

    log.info(f"{'Folders':<46}{len(labels)} ({', '.join(labels)})")
    log.info(f"{'Files to evaluate':<46}{len(all_files)}  |  first_run={first_run}")

    # ── Per-file resolution ──────────────────────────────────────────────────
    for rel in sorted(all_files):
        display = rel if len(rel) <= 45 else "…" + rel[-44:]

        # Which folders currently have this file?
        present: dict[str, float] = {
            lbl: curr[lbl][rel] for lbl in labels if rel in curr[lbl]
        }
        # Which folders had it in the last snapshot?
        was_present: dict[str, float] = {
            lbl: prev.get(lbl, {}).get(rel, None)
            for lbl in labels
            if rel in prev.get(lbl, {})
        }

        try:
            # ────────────────────────────────────────────────────────────────
            # CASE A: No folder has the file now
            # ────────────────────────────────────────────────────────────────
            if not present:
                _log_action("SKIP", display, "gone from all folders")
                continue

            # ────────────────────────────────────────────────────────────────
            # CASE B: DELETE — file was in snapshot(s) but someone deleted it
            # Detection: it existed in ALL snapshots last time (so every node
            # "knew" about it), and now it's missing from at least one.
            # ────────────────────────────────────────────────────────────────
            all_knew = len(was_present) == len(labels)  # every folder had it before
            some_deleted = len(present) < len(labels)   # at least one folder missing it now

            if not first_run and all_knew and some_deleted:
                deleted_from = [lbl for lbl in labels if rel not in curr[lbl]]
                still_have   = [lbl for lbl in labels if rel in curr[lbl]]

                # Confirm it's actually a delete: check nothing changed among
                # those that still have it (unchanged = same mtime as snapshot)
                # If something changed among those keeping it, treat as UPDATE not DELETE.
                keeper_changed = any(
                    curr[lbl][rel] != prev.get(lbl, {}).get(rel)
                    for lbl in still_have
                )

                if not keeper_changed:
                    purge_from = still_have  # delete from everyone who still has it
                    detail = (
                        f"deleted from [{', '.join(deleted_from)}]  "
                        f"→  purging from [{', '.join(purge_from)}]"
                    )
                    _log_action("DELETE", display, detail)
                    if not dry_run:
                        for lbl in purge_from:
                            safe_delete(str(Path(folders[lbl]) / rel))
                    result.deleted.append((rel, purge_from))
                    continue
                # else: fall through — someone edited while others deleted → UPDATE wins

            # ────────────────────────────────────────────────────────────────
            # CASE C: All folders have the file — check for changes
            # ────────────────────────────────────────────────────────────────
            if len(present) == len(labels):
                # Compute hashes only for folders whose mtime changed
                hashes = {
                    lbl: file_hash(str(Path(folders[lbl]) / rel))
                    for lbl in labels
                }
                unique_hashes = set(hashes.values())

                if len(unique_hashes) == 1:
                    # All identical in content
                    _log_action("SKIP", display, "identical across all folders")
                    result.skipped.append(rel)
                    continue

                # Content differs — find winner (newest mtime among present)
                winner_lbl = max(present, key=lambda lbl: present[lbl])
                winner_path = str(Path(folders[winner_lbl]) / rel)

                # Determine if this is a CONFLICT (multiple folders changed)
                if not first_run:
                    changed_labels = [
                        lbl for lbl in labels
                        if curr[lbl][rel] != prev.get(lbl, {}).get(rel, curr[lbl][rel])
                    ]
                else:
                    changed_labels = []

                is_conflict = len(changed_labels) > 1

                action = "CONFLICT" if is_conflict else "UPDATE"
                losers = [lbl for lbl in labels if lbl != winner_lbl]
                detail = (
                    f"[CONFLICT]  {winner_lbl} wins  "
                    f"→  updating [{', '.join(losers)}]"
                    if is_conflict
                    else f"{winner_lbl} wins  →  updating [{', '.join(losers)}]"
                )
                _log_action(action, display, detail)

                if not dry_run:
                    for lbl in losers:
                        safe_copy(winner_path, str(Path(folders[lbl]) / rel))

                if is_conflict:
                    result.conflicts.append((rel, winner_lbl, changed_labels))
                else:
                    result.updated.append((rel, winner_lbl))
                continue

            # ────────────────────────────────────────────────────────────────
            # CASE D: CREATE — file only exists in some folders, not all
            # Could be: brand new file, OR partial delete on first run
            # ────────────────────────────────────────────────────────────────
            missing_labels = [lbl for lbl in labels if rel not in curr[lbl]]

            # Find the best source: newest mtime among folders that have it
            source_lbl  = max(present, key=lambda lbl: present[lbl])
            source_path = str(Path(folders[source_lbl]) / rel)

            detail = f"from {source_lbl}  →  copying to [{', '.join(missing_labels)}]"
            _log_action("CREATE", display, detail)

            if not dry_run:
                for lbl in missing_labels:
                    safe_copy(source_path, str(Path(folders[lbl]) / rel))

            result.created.append((rel, missing_labels))

        except Exception as e:
            _log_action("ERROR", display, str(e))
            result.errors.append((rel, str(e)))

    # ── Persist state ────────────────────────────────────────────────────────
    if not dry_run:
        save_state(folders)

    elapsed = time.monotonic() - start
    if log_file:
        write_session_footer(log_file, result, elapsed)

    return result

# ─── Config Loading ───────────────────────────────────────────────────────────

def load_folders_from_config(config_path: str) -> dict[str, str]:
    """
    Read config.json and extract all folder_* keys in alphabetical order.
    Supports any number: folder_a, folder_b, folder_c, ... folder_z, folder_aa, etc.

    Example config.json:
        {
          "folder_a": "C:/Users/you/Documents",
          "folder_b": "D:/Backup",
          "folder_c": "E:/NAS/Docs"
        }
    """
    if not os.path.exists(config_path):
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    folders = {
        k: v for k, v in sorted(cfg.items())
        if k.startswith("folder_") and isinstance(v, str)
    }

    if len(folders) < 2:
        log.error(
            f"config.json must define at least 2 folder_* keys. "
            f"Found: {list(folders.keys()) or 'none'}"
        )
        sys.exit(1)

    return folders

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "FolderSync v3 — Multi-folder sync\n"
            "Add as many folder_a / folder_b / folder_c / ... entries as you\n"
            "want in config.json. All sync rules apply across every folder."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--dry-run",      action="store_true", help="Simulate without making changes")
    parser.add_argument("--verbose", "-v",action="store_true", help="Show DEBUG output")
    parser.add_argument("--log",          default="sync.log",  help="Log file path (default: sync.log)")
    parser.add_argument("--config",       default="config.json", help="Config file (default: config.json)")
    parser.add_argument("--reset-state",  action="store_true", help="Delete sync_state.json and re-baseline")
    args = parser.parse_args()

    setup_logging(args.log, args.verbose)

    if args.reset_state and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        log.info("State reset — next sync treated as first run.")

    folders = load_folders_from_config(args.config)

    log.info(f"Loaded {len(folders)} folders from {args.config}")
    for label, path in folders.items():
        log.info(f"  {label:<12} →  {path}")

    if args.dry_run:
        log.info("DRY RUN MODE — no files will be changed")

    result = sync_all(folders, dry_run=args.dry_run, log_file=args.log)
    print(result.summary())

    if result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
