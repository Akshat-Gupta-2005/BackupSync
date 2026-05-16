"""
FolderSync v4 — Multi-folder sync with pre-flight validation
============================================================

New in v4:
  Before touching any file, the engine does a full dry-run scan and prints
  a structured preview to the terminal — showing every action it would take,
  grouped by type, with counts and folder paths listed. A confirmation prompt
  then asks the user to proceed or abort. Only after confirmation does the
  actual sync execute.

  --yes / -y  flag skips the confirmation (for automated / scheduled runs).

All v3 rules preserved:
  CREATE   — file in some folders not all → copy to missing
  UPDATE   — file in all folders, content differs → newest wins, pushed to all
  DELETE   — file gone from one folder since last sync → purged from all
  CONFLICT — file edited in multiple folders → newest wins, all updated
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
from dataclasses import dataclass, field

# ─── Constants ────────────────────────────────────────────────────────────────

LOG_WIDTH     = 90    # width for the log file box borders
PREVIEW_WIDTH = 92    # width for the terminal preview box (slightly wider for readability)

# ANSI colour codes — used in terminal preview only, never in the log file
C_RESET    = "\033[0m"
C_BOLD     = "\033[1m"
C_DIM      = "\033[2m"
C_GREEN    = "\033[32m"
C_YELLOW   = "\033[33m"
C_RED      = "\033[31m"
C_CYAN     = "\033[36m"
C_MAGENTA  = "\033[35m"
C_BLUE     = "\033[34m"
C_WHITE    = "\033[97m"
C_BG_RED   = "\033[41m"

# ─── Logging (file + console) ─────────────────────────────────────────────────

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
        "DEBUG":    C_DIM,
        "INFO":     C_RESET,
        "WARNING":  C_YELLOW,
        "ERROR":    C_RED,
        "CRITICAL": "\033[1;31m",
    }

    def format(self, record: logging.LogRecord) -> str:
        time_str = self.formatTime(record, "%H:%M:%S")
        color = self.COLORS.get(record.levelname, "")
        return f"{color}{time_str}  {record.getMessage()}{C_RESET}"


def _log_box_line(content: str = "", width: int = LOG_WIDTH) -> str:
    inner = width - 2
    return f"│  {content:<{inner - 2}}│"


def write_session_header(log_file: str, folders: dict[str, str], dry_run: bool = False):
    now    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    mode   = "  [DRY RUN]" if dry_run else ""
    top    = "┌" + "─" * (LOG_WIDTH - 2) + "┐"
    sep    = "├" + "─" * (LOG_WIDTH - 2) + "┤"
    bottom = "└" + "─" * (LOG_WIDTH - 2) + "┘"
    lines  = ["", top, _log_box_line(f"FOLDERSYNC v4 SESSION  ·  {now}{mode}"), sep]
    for label, path in folders.items():
        lines.append(_log_box_line(f"{label.upper():<10}→  {path}"))
    lines.append(bottom)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_session_footer(log_file: str, result: "SyncResult", elapsed: float):
    sep   = "  " + "─" * (LOG_WIDTH - 2)
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
    if not os.path.exists(STATE_FILE):
        return {"last_sync": None, "version": 4, "folders": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("version", 2) < 3:
        state = _migrate_v2_state(state)
    return state


def _migrate_v2_state(old: dict) -> dict:
    log.info("Migrating sync_state.json from v2 → v4 format")
    return {
        "last_sync": old.get("last_sync"),
        "version": 4,
        "folders": {
            "folder_a": old.get("files_a", {}),
            "folder_b": old.get("files_b", {}),
        },
    }


def save_state(folders: dict[str, str]):
    state = {
        "last_sync": datetime.now().isoformat(),
        "version": 4,
        "folders": {label: snapshot_folder(path) for label, path in folders.items()},
    }
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    log.debug("State saved to sync_state.json")


def snapshot_folder(folder: str) -> dict:
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

# ─── Result & planned actions ─────────────────────────────────────────────────

@dataclass
class PlannedAction:
    """Represents one file operation decided during the scan pass."""
    action:  str          # CREATE | UPDATE | DELETE | CONFLICT | SKIP
    rel:     str          # relative file path
    detail:  str = ""     # human-readable detail string
    source:  str = ""     # source folder label (for copy ops)
    targets: list = field(default_factory=list)  # target folder labels


class SyncResult:
    def __init__(self):
        self.created   = []
        self.updated   = []
        self.deleted   = []
        self.conflicts = []
        self.skipped   = []
        self.errors    = []

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

# ─── Log-file action emitter ──────────────────────────────────────────────────

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

# ─── Scan pass (pure analysis, no file I/O) ───────────────────────────────────

def scan_folders(folders: dict[str, str]) -> list[PlannedAction]:
    """
    Analyse all folders and return the full list of planned actions.
    Does NOT touch any files — purely reads mtimes and hashes.
    This is the same logic as sync_all() but with dry_run=True and
    returning structured PlannedAction objects instead of writing a result.
    """
    labels    = list(folders.keys())
    state     = load_state()
    first_run = state["last_sync"] is None
    prev      = state.get("folders", {})

    curr: dict[str, dict[str, float]] = {
        label: snapshot_folder(path) for label, path in folders.items()
    }

    all_files: set[str] = set()
    for snap in curr.values():
        all_files |= snap.keys()
    for snap in prev.values():
        all_files |= snap.keys()

    actions: list[PlannedAction] = []

    for rel in sorted(all_files):
        display = rel if len(rel) <= 45 else "…" + rel[-44:]

        present: dict[str, float] = {
            lbl: curr[lbl][rel] for lbl in labels if rel in curr[lbl]
        }
        was_present: dict[str, float] = {
            lbl: prev.get(lbl, {}).get(rel, None)
            for lbl in labels
            if rel in prev.get(lbl, {})
        }

        try:
            if not present:
                actions.append(PlannedAction("SKIP", display, "gone from all folders"))
                continue

            all_knew     = len(was_present) == len(labels)
            some_deleted = len(present) < len(labels)

            if not first_run and all_knew and some_deleted:
                deleted_from = [lbl for lbl in labels if rel not in curr[lbl]]
                still_have   = [lbl for lbl in labels if rel in curr[lbl]]
                keeper_changed = any(
                    curr[lbl][rel] != prev.get(lbl, {}).get(rel)
                    for lbl in still_have
                )
                if not keeper_changed:
                    detail = (
                        f"deleted from [{', '.join(deleted_from)}]  "
                        f"→  purging from [{', '.join(still_have)}]"
                    )
                    actions.append(PlannedAction("DELETE", display, detail,
                                                 targets=still_have))
                    continue

            if len(present) == len(labels):
                hashes = {
                    lbl: file_hash(str(Path(folders[lbl]) / rel))
                    for lbl in labels
                }
                if len(set(hashes.values())) == 1:
                    actions.append(PlannedAction("SKIP", display,
                                                 "identical across all folders"))
                    continue

                winner_lbl = max(present, key=lambda lbl: present[lbl])
                losers     = [lbl for lbl in labels if lbl != winner_lbl]

                if not first_run:
                    changed_labels = [
                        lbl for lbl in labels
                        if curr[lbl][rel] != prev.get(lbl, {}).get(rel, curr[lbl][rel])
                    ]
                else:
                    changed_labels = []

                is_conflict = len(changed_labels) > 1
                action      = "CONFLICT" if is_conflict else "UPDATE"
                detail = (
                    f"[CONFLICT]  {winner_lbl} wins  →  updating [{', '.join(losers)}]"
                    if is_conflict
                    else f"{winner_lbl} wins  →  updating [{', '.join(losers)}]"
                )
                actions.append(PlannedAction(action, display, detail,
                                             source=winner_lbl, targets=losers))
                continue

            missing_labels = [lbl for lbl in labels if rel not in curr[lbl]]
            source_lbl     = max(present, key=lambda lbl: present[lbl])
            detail         = f"from {source_lbl}  →  copying to [{', '.join(missing_labels)}]"
            actions.append(PlannedAction("CREATE", display, detail,
                                         source=source_lbl, targets=missing_labels))

        except Exception as e:
            actions.append(PlannedAction("ERROR", display, str(e)))

    return actions

# ─── Terminal preview ─────────────────────────────────────────────────────────

# Action metadata: (colour, bullet, label shown in preview)
ACTION_META = {
    "CREATE":   (C_GREEN,   "  +  ", "CREATE  "),
    "UPDATE":   (C_CYAN,    "  ↑  ", "UPDATE  "),
    "DELETE":   (C_RED,     "  ✕  ", "DELETE  "),
    "CONFLICT": (C_YELLOW,  "  ⚠  ", "CONFLICT"),
    "SKIP":     (C_DIM,     "  ·  ", "SKIP    "),
    "ERROR":    (C_BG_RED,  "  !  ", "ERROR   "),
}


def _pv_line(content: str = "", width: int = PREVIEW_WIDTH,
             left: str = "│", right: str = "│", color: str = "") -> str:
    inner = width - 4
    return f"{left}  {color}{content:<{inner}}{C_RESET}  {right}"


def print_preview(folders: dict[str, str], actions: list[PlannedAction]):
    """
    Print the full pre-flight preview to the terminal.
    Shows a header with folder paths, then every planned action grouped
    by type, then a summary count table.
    """
    W = PREVIEW_WIDTH
    top    = "┌" + "─" * (W - 2) + "┐"
    sep    = "├" + "─" * (W - 2) + "┤"
    bottom = "└" + "─" * (W - 2) + "┘"
    thin   = "├" + "╌" * (W - 2) + "┤"

    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")

    print()
    print(f"{C_BOLD}{C_WHITE}{top}{C_RESET}")
    print(_pv_line(f"  FOLDERSYNC v4  ·  PRE-FLIGHT PREVIEW  ·  {now}", W,
                   color=C_BOLD + C_WHITE))
    print(f"{C_BOLD}{C_WHITE}{sep}{C_RESET}")

    for label, path in folders.items():
        truncated = path if len(path) <= W - 20 else "…" + path[-(W - 21):]
        print(_pv_line(f"{C_CYAN}{label.upper():<12}{C_RESET}{C_DIM}→{C_RESET}  {truncated}", W))

    # ── Bucket actions by type ───────────────────────────────────────────────
    buckets: dict[str, list[PlannedAction]] = {
        "CREATE": [], "UPDATE": [], "DELETE": [],
        "CONFLICT": [], "SKIP": [], "ERROR": [],
    }
    for a in actions:
        buckets.get(a.action, buckets["ERROR"]).append(a)

    shown_types = [t for t in ("CONFLICT", "DELETE", "CREATE", "UPDATE", "ERROR", "SKIP")
                   if buckets[t]]

    for action_type in shown_types:
        group = buckets[action_type]
        color, bullet, label = ACTION_META[action_type]

        print(f"{C_BOLD}{C_WHITE}{sep}{C_RESET}")

        # Section header with count
        count_str = f"{label}  ×{len(group)}"
        print(_pv_line(f"{color}{C_BOLD}{count_str}{C_RESET}", W, color=""))

        print(f"{C_BOLD}{C_WHITE}{thin}{C_RESET}")

        for a in group:
            # File name (left column, fixed width)
            fname_col = 42
            fname     = a.rel if len(a.rel) <= fname_col else "…" + a.rel[-(fname_col - 1):]
            fname_pad = f"{fname:<{fname_col}}"

            # Detail string
            detail_col = W - 4 - fname_col - 6  # remaining space
            detail     = a.detail if len(a.detail) <= detail_col else a.detail[:detail_col - 1] + "…"

            row = f"{color}{bullet}{C_RESET}{fname_pad}  {C_DIM}{detail}{C_RESET}"
            print(_pv_line(row, W, color=""))

    # ── Summary table ────────────────────────────────────────────────────────
    print(f"{C_BOLD}{C_WHITE}{sep}{C_RESET}")

    counts = {t: len(buckets[t]) for t in buckets}
    total_changes = sum(counts[t] for t in ("CREATE", "UPDATE", "DELETE", "CONFLICT"))

    col_w = (W - 6) // 6  # spread 6 columns across the box inner width

    headers = f"{'CREATE':<{col_w}}{'UPDATE':<{col_w}}{'DELETE':<{col_w}}{'CONFLICT':<{col_w}}{'SKIP':<{col_w}}{'ERRORS':<{col_w}}"
    values  = (
        f"{C_GREEN}{counts['CREATE']:<{col_w}}{C_RESET}"
        f"{C_CYAN}{counts['UPDATE']:<{col_w}}{C_RESET}"
        f"{C_RED}{counts['DELETE']:<{col_w}}{C_RESET}"
        f"{C_YELLOW}{counts['CONFLICT']:<{col_w}}{C_RESET}"
        f"{C_DIM}{counts['SKIP']:<{col_w}}{C_RESET}"
        f"{C_RED if counts['ERROR'] else C_DIM}{counts['ERROR']:<{col_w}}{C_RESET}"
    )
    print(_pv_line(f"{C_DIM}{headers}{C_RESET}", W, color=""))
    print(_pv_line(values, W, color=""))

    print(f"{C_BOLD}{C_WHITE}{thin}{C_RESET}")

    if total_changes == 0:
        print(_pv_line(f"{C_GREEN}{C_BOLD}  All folders are already in sync. No changes needed.{C_RESET}", W, color=""))
    else:
        total_files = len([a for a in actions if a.action != "SKIP"])
        print(_pv_line(
            f"{C_BOLD}  {total_changes} file operation(s) planned  ·  "
            f"{counts['CONFLICT']} conflict(s)  ·  {counts['ERROR']} error(s){C_RESET}", W, color=""))

    print(f"{C_BOLD}{C_WHITE}{bottom}{C_RESET}")
    print()


# ─── Confirmation prompt ──────────────────────────────────────────────────────

def ask_confirmation(actions: list[PlannedAction]) -> bool:
    """
    Print the confirmation prompt and wait for user input.
    Returns True if the user confirmed, False if aborted.
    """
    changes = [a for a in actions if a.action not in ("SKIP",)]
    errors  = [a for a in actions if a.action == "ERROR"]

    if not changes and not errors:
        # Nothing to do — no point asking
        print(f"  {C_DIM}Nothing to sync. Exiting.{C_RESET}\n")
        return False

    if errors:
        print(f"  {C_YELLOW}⚠  {len(errors)} error(s) detected during scan. "
              f"They will be skipped during sync.{C_RESET}")

    print(f"  {C_BOLD}Proceed with sync?{C_RESET}  "
          f"{C_GREEN}[Y]{C_RESET} Yes   "
          f"{C_RED}[N]{C_RESET} No / Abort"
          f"  {C_DIM}(default: N){C_RESET}")
    print()

    try:
        answer = input("  → ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(f"\n  {C_RED}Aborted.{C_RESET}\n")
        return False

    if answer in ("y", "yes"):
        print()
        return True

    print(f"\n  {C_RED}Sync aborted. No files were changed.{C_RESET}\n")
    return False

# ─── Execute pass (applies planned actions) ───────────────────────────────────

def execute_actions(
    folders: dict[str, str],
    actions: list[PlannedAction],
    log_file: str,
) -> SyncResult:
    """
    Apply every planned action, writing results to the log file.
    Called only after the user confirms.
    """
    start  = time.monotonic()
    result = SyncResult()
    labels = list(folders.keys())

    if log_file:
        write_session_header(log_file, folders)

    log.info(f"{'Executing':<46}{len([a for a in actions if a.action != 'SKIP'])} planned operations")

    for a in actions:
        display = a.rel

        try:
            if a.action == "SKIP":
                _log_action("SKIP", display, a.detail)
                result.skipped.append(a.rel)

            elif a.action == "CREATE":
                _log_action("CREATE", display, a.detail)
                src_path = str(Path(folders[a.source]) / a.rel)
                for lbl in a.targets:
                    safe_copy(src_path, str(Path(folders[lbl]) / a.rel))
                result.created.append((a.rel, a.targets))

            elif a.action == "UPDATE":
                _log_action("UPDATE", display, a.detail)
                src_path = str(Path(folders[a.source]) / a.rel)
                for lbl in a.targets:
                    safe_copy(src_path, str(Path(folders[lbl]) / a.rel))
                result.updated.append((a.rel, a.source))

            elif a.action == "DELETE":
                _log_action("DELETE", display, a.detail)
                for lbl in a.targets:
                    safe_delete(str(Path(folders[lbl]) / a.rel))
                result.deleted.append((a.rel, a.targets))

            elif a.action == "CONFLICT":
                _log_action("CONFLICT", display, a.detail)
                src_path = str(Path(folders[a.source]) / a.rel)
                for lbl in a.targets:
                    safe_copy(src_path, str(Path(folders[lbl]) / a.rel))
                result.conflicts.append((a.rel, a.source, a.targets))

            elif a.action == "ERROR":
                _log_action("ERROR", display, a.detail)
                result.errors.append((a.rel, a.detail))

        except Exception as e:
            _log_action("ERROR", display, str(e))
            result.errors.append((a.rel, str(e)))

    save_state(folders)

    elapsed = time.monotonic() - start
    if log_file:
        write_session_footer(log_file, result, elapsed)

    return result

# ─── Config loading ───────────────────────────────────────────────────────────

def load_folders_from_config(config_path: str) -> dict[str, str]:
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
            "FolderSync v4 — Multi-folder sync with pre-flight validation\n"
            "Scans all folders, shows a full preview of planned actions,\n"
            "then asks for confirmation before touching any file.\n\n"
            "Use --yes to skip confirmation (for scheduled/automated runs)."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--yes",    "-y",  action="store_true", help="Skip confirmation prompt (auto-confirm)")
    parser.add_argument("--dry-run",       action="store_true", help="Preview only — never execute (implies no confirmation needed)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show DEBUG output in console")
    parser.add_argument("--log",           default="sync.log",  help="Log file path (default: sync.log)")
    parser.add_argument("--config",        default="config.json", help="Config file (default: config.json)")
    parser.add_argument("--reset-state",   action="store_true", help="Delete sync_state.json and re-baseline")
    args = parser.parse_args()

    setup_logging(args.log, args.verbose)

    if args.reset_state and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        log.info("State reset — next sync treated as first run.")

    folders = load_folders_from_config(args.config)

    # ── PASS 1: Scan ─────────────────────────────────────────────────────────
    print(f"\n  {C_DIM}Scanning folders...{C_RESET}")
    actions = scan_folders(folders)

    # ── PASS 2: Preview ──────────────────────────────────────────────────────
    print_preview(folders, actions)

    # ── Dry run exits here ───────────────────────────────────────────────────
    if args.dry_run:
        print(f"  {C_DIM}Dry-run mode — no files changed.{C_RESET}\n")
        return

    # ── PASS 3: Confirmation ─────────────────────────────────────────────────
    if not args.yes:
        confirmed = ask_confirmation(actions)
        if not confirmed:
            sys.exit(0)
    else:
        changes = [a for a in actions if a.action not in ("SKIP",)]
        if not changes:
            print(f"  {C_DIM}Nothing to sync. Exiting.{C_RESET}\n")
            return
        print(f"  {C_DIM}--yes flag set. Proceeding automatically.{C_RESET}\n")

    # ── PASS 4: Execute ──────────────────────────────────────────────────────
    result = execute_actions(folders, actions, log_file=args.log)
    print(result.summary())

    if result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
