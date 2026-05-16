"""
FolderSync v4 — Paired two-way sync with per-pair logs and state
=================================================================

Config format (config.json):
  {
    "pairs": [
      { "source": "D:/FolderA", "destination": "D:/FolderB" },
      { "source": "D:/FolderC", "destination": "D:/FolderD" }
    ]
  }

Each pair runs independently with its own:
  - log file  →  sync_FolderA_FolderB.log
  - state file →  sync_state_FolderA_FolderB.json

Sync rules (two-way, same as before):
  CREATE   — file in one folder but not the other → copy to missing
  UPDATE   — file in both folders, content differs → newest wins, pushed to both
  DELETE   — file gone from one folder since last sync → purged from both
  CONFLICT — file edited in both folders → newest wins, all updated

  --yes / -y  flag skips confirmation for all pairs (for automated / scheduled runs).
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

LOG_WIDTH     = 90
PREVIEW_WIDTH = 92

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


def setup_logging(log_file: str, verbose: bool = False):
    """
    Set up (or reconfigure) logging for a specific pair's log file.
    Each call replaces any existing file handler with a new one pointed
    at the given log_file, while keeping the single console handler.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any existing handlers so we don't accumulate them across pairs
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ConsoleLogFormatter())
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(console_handler)

    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(FileLogFormatter())
        fh.setLevel(logging.DEBUG)
        root.addHandler(fh)


log = logging.getLogger(__name__)

# ─── State ────────────────────────────────────────────────────────────────────

def load_state(state_file: str) -> dict:
    if not os.path.exists(state_file):
        return {"last_sync": None, "version": 4, "folders": {}}
    with open(state_file, "r", encoding="utf-8") as f:
        state = json.load(f)
    if state.get("version", 2) < 3:
        state = _migrate_v2_state(state)
    return state


def _migrate_v2_state(old: dict) -> dict:
    log.info("Migrating state from v2 → v4 format")
    return {
        "last_sync": old.get("last_sync"),
        "version": 4,
        "folders": {
            "source":      old.get("files_a", {}),
            "destination": old.get("files_b", {}),
        },
    }


def save_state(folders: dict[str, str], state_file: str):
    state = {
        "last_sync": datetime.now().isoformat(),
        "version": 4,
        "folders": {label: snapshot_folder(path) for label, path in folders.items()},
    }
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    log.debug(f"State saved to {state_file}")


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
    action:  str
    rel:     str
    detail:  str = ""
    source:  str = ""
    targets: list = field(default_factory=list)


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

def scan_folders(folders: dict[str, str], state_file: str) -> list[PlannedAction]:
    """
    Analyse the source/destination pair and return the full list of planned
    actions. Does NOT touch any files — purely reads mtimes and hashes.
    """
    labels    = list(folders.keys())
    state     = load_state(state_file)
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

ACTION_META = {
    "CREATE":   (C_GREEN,   "  +  ", "CREATE  "),
    "UPDATE":   (C_CYAN,    "  ↑  ", "UPDATE  "),
    "DELETE":   (C_RED,     "  ✕  ", "DELETE  "),
    "CONFLICT": (C_YELLOW,  "  ⚠  ", "CONFLICT"),
    "SKIP":     (C_DIM,     "  ·  ", "SKIP    "),
    "ERROR":    (C_BG_RED,  "  !  ", "ERROR   "),
}


import re as _re
_ANSI_ESC = _re.compile(r"\033\[[0-9;]*m")

def _visible_len(s: str) -> int:
    """Return the printable length of a string, ignoring ANSI escape codes."""
    return len(_ANSI_ESC.sub("", s))


def _pv_line(content: str = "", width: int = PREVIEW_WIDTH,
             left: str = "│", right: str = "│", color: str = "") -> str:
    inner   = width - 4          # visible chars between the two border │ chars (minus 2 spaces each side)
    prefix  = color              # prepended colour, if any (legacy arg kept for compat)
    full    = prefix + content
    pad     = max(0, inner - _visible_len(full))
    return f"{left}  {full}{' ' * pad}{C_RESET}  {right}"


def print_preview(folders: dict[str, str], actions: list[PlannedAction], pair_label: str = ""):
    """
    Print the full pre-flight preview to the terminal for one pair.
    """
    W = PREVIEW_WIDTH
    top    = "┌" + "─" * (W - 2) + "┐"
    sep    = "├" + "─" * (W - 2) + "┤"
    bottom = "└" + "─" * (W - 2) + "┘"
    thin   = "├" + "╌" * (W - 2) + "┤"

    now = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    pair_tag = f"  ·  {pair_label}" if pair_label else ""

    print()
    print(f"{C_BOLD}{C_WHITE}{top}{C_RESET}")
    print(_pv_line(f"  FOLDERSYNC v4  ·  PRE-FLIGHT PREVIEW{pair_tag}  ·  {now}", W,
                   color=C_BOLD + C_WHITE))
    print(f"{C_BOLD}{C_WHITE}{sep}{C_RESET}")

    for label, path in folders.items():
        truncated = path if len(path) <= W - 20 else "…" + path[-(W - 21):]
        print(_pv_line(f"{C_CYAN}{label.upper():<12}{C_RESET}{C_DIM}→{C_RESET}  {truncated}", W))

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
        count_str = f"{label}  ×{len(group)}"
        print(_pv_line(f"{color}{C_BOLD}{count_str}{C_RESET}", W, color=""))
        print(f"{C_BOLD}{C_WHITE}{thin}{C_RESET}")

        for a in group:
            fname_col  = 42
            bullet_vis = len(bullet)   # bullet is plain ASCII spaces + symbol, no escapes
            fname      = a.rel if len(a.rel) <= fname_col else "…" + a.rel[-(fname_col - 1):]
            fname_pad  = f"{fname:<{fname_col}}"
            # inner visible width  minus  border padding (4)  minus  bullet  minus  fname  minus  "  " separator
            detail_col = (W - 4) - bullet_vis - fname_col - 2
            detail     = a.detail if len(a.detail) <= detail_col else a.detail[:detail_col - 1] + "…"
            row = f"{color}{bullet}{C_RESET}{fname_pad}  {C_DIM}{detail}{C_RESET}"
            print(_pv_line(row, W, color=""))

    # ── Summary table ────────────────────────────────────────────────────────
    print(f"{C_BOLD}{C_WHITE}{sep}{C_RESET}")

    counts = {t: len(buckets[t]) for t in buckets}
    total_changes = sum(counts[t] for t in ("CREATE", "UPDATE", "DELETE", "CONFLICT"))

    col_w = (W - 6) // 6

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
        print(_pv_line(
            f"{C_BOLD}  {total_changes} file operation(s) planned  ·  "
            f"{counts['CONFLICT']} conflict(s)  ·  {counts['ERROR']} error(s){C_RESET}", W, color=""))

    print(f"{C_BOLD}{C_WHITE}{bottom}{C_RESET}")
    print()

# ─── Confirmation prompt ──────────────────────────────────────────────────────

def ask_confirmation(actions: list[PlannedAction]) -> bool:
    changes = [a for a in actions if a.action not in ("SKIP",)]
    errors  = [a for a in actions if a.action == "ERROR"]

    if not changes and not errors:
        print(f"  {C_DIM}Nothing to sync. Skipping.{C_RESET}\n")
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
    state_file: str,
) -> SyncResult:
    start  = time.monotonic()
    result = SyncResult()

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

    save_state(folders, state_file)

    elapsed = time.monotonic() - start
    if log_file:
        write_session_footer(log_file, result, elapsed)

    return result

# ─── Pair naming helpers ──────────────────────────────────────────────────────

def _folder_basename(path: str) -> str:
    """Return the last non-empty path component, safe for use in filenames."""
    name = Path(path).name or Path(path).parts[-1]
    # Replace characters that are unsafe in filenames
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name


def pair_file_stem(source: str, destination: str) -> str:
    """Return e.g. 'FolderA_FolderB' for use in log/state filenames."""
    return f"{_folder_basename(source)}_{_folder_basename(destination)}"

# ─── Config loading ───────────────────────────────────────────────────────────

def load_pairs_from_config(config_path: str) -> list[dict[str, str]]:
    """
    Load and validate the pairs list from config.json.
    Returns a list of dicts, each with 'source' and 'destination' keys.
    """
    if not os.path.exists(config_path):
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    pairs = cfg.get("pairs")
    if not pairs or not isinstance(pairs, list):
        log.error("config.json must have a 'pairs' list with at least one entry.")
        sys.exit(1)

    validated = []
    for i, p in enumerate(pairs, start=1):
        if not isinstance(p, dict) or "source" not in p or "destination" not in p:
            log.error(f"Pair {i} is missing 'source' or 'destination' key.")
            sys.exit(1)
        validated.append({"source": p["source"], "destination": p["destination"]})

    return validated

# ─── Single-pair runner ───────────────────────────────────────────────────────

def run_pair(
    pair: dict[str, str],
    pair_index: int,
    total_pairs: int,
    args,
) -> bool:
    """
    Run the full 4-pass sync for one source/destination pair.
    Returns True if there were no errors, False otherwise.
    """
    source      = pair["source"]
    destination = pair["destination"]
    stem        = pair_file_stem(source, destination)
    log_file    = f"sync_{stem}.log"
    state_file  = f"sync_state_{stem}.json"
    pair_label  = f"Pair {pair_index}/{total_pairs}  [{stem}]"

    folders = {"source": source, "destination": destination}

    # Reconfigure logging for this pair's log file
    setup_logging(log_file, args.verbose)

    # Print pair separator when running multiple pairs
    if total_pairs > 1:
        W = PREVIEW_WIDTH
        bar = "═" * (W - 2)
        print(f"\n{C_BOLD}{C_MAGENTA}╔{bar}╗{C_RESET}")
        label_str = f"  PAIR {pair_index} of {total_pairs}  ·  {stem}"
        print(f"{C_BOLD}{C_MAGENTA}║  {label_str:<{W - 4}}║{C_RESET}")
        print(f"{C_BOLD}{C_MAGENTA}╚{bar}╝{C_RESET}")

    if args.reset_state and os.path.exists(state_file):
        os.remove(state_file)
        log.info(f"State reset for {stem} — next sync treated as first run.")

    # Validate folders exist
    for label, path in folders.items():
        if not os.path.isdir(path):
            log.error(f"{label.upper()} folder not found: {path}")
            return False

    # ── PASS 1: Scan ─────────────────────────────────────────────────────────
    print(f"\n  {C_DIM}Scanning folders...{C_RESET}")
    actions = scan_folders(folders, state_file)

    # ── PASS 2: Preview ──────────────────────────────────────────────────────
    print_preview(folders, actions, pair_label=pair_label if total_pairs > 1 else "")

    # ── Dry run exits here ───────────────────────────────────────────────────
    if args.dry_run:
        print(f"  {C_DIM}Dry-run mode — no files changed.{C_RESET}\n")
        return True

    # ── PASS 3: Confirmation ─────────────────────────────────────────────────
    if not args.yes:
        confirmed = ask_confirmation(actions)
        if not confirmed:
            return True   # aborted, but not an error
    else:
        changes = [a for a in actions if a.action not in ("SKIP",)]
        if not changes:
            print(f"  {C_DIM}Nothing to sync. Skipping.{C_RESET}\n")
            return True
        print(f"  {C_DIM}--yes flag set. Proceeding automatically.{C_RESET}\n")

    # ── PASS 4: Execute ──────────────────────────────────────────────────────
    result = execute_actions(folders, actions, log_file=log_file, state_file=state_file)
    print(result.summary())

    return len(result.errors) == 0

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=(
            "FolderSync v4 — Paired two-way sync with per-pair logs and state\n"
            "Each source/destination pair in config.json runs independently,\n"
            "with its own log file and state file named after the folder pair.\n\n"
            "Use --yes to skip confirmation for all pairs (scheduled/automated runs)."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--yes",          "-y",  action="store_true", help="Skip confirmation for all pairs")
    parser.add_argument("--dry-run",             action="store_true", help="Preview only — never execute")
    parser.add_argument("--verbose",      "-v",  action="store_true", help="Show DEBUG output in console")
    parser.add_argument("--config",              default="config.json", help="Config file (default: config.json)")
    parser.add_argument("--reset-state",         action="store_true", help="Delete state files for all pairs and re-baseline")
    args = parser.parse_args()

    pairs = load_pairs_from_config(args.config)
    total = len(pairs)

    print(f"\n  {C_BOLD}FolderSync v4{C_RESET}  ·  {C_CYAN}{total} pair(s) loaded{C_RESET}")

    any_errors = False
    for i, pair in enumerate(pairs, start=1):
        ok = run_pair(pair, i, total, args)
        if not ok:
            any_errors = True

    if total > 1:
        W = PREVIEW_WIDTH
        bar = "─" * (W - 2)
        status = f"{C_RED}Completed with errors{C_RESET}" if any_errors else f"{C_GREEN}All pairs completed successfully{C_RESET}"
        print(f"\n  {C_BOLD}{'─'*54}{C_RESET}")
        print(f"  {C_BOLD}All done.{C_RESET}  {status}\n")

    if any_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
