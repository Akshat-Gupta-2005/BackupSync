# FolderSync

> Lightweight two-way folder synchronization in pure Python — no dependencies required.

FolderSync keeps two folders in perfect sync across local drives, external disks, or network shares. It handles all four sync scenarios — creating, updating, deleting, and resolving conflicts — using a state-tracking approach that makes delete detection reliable across runs.

---

## Features

- **Two-way sync** — changes in either folder propagate to the other
- **State-aware delete detection** — distinguishes between "file was deleted" and "file never existed"
- **Conflict resolution** — when both sides change, the newer timestamp wins
- **Content-based skip** — MD5 hash comparison avoids unnecessary copies even when timestamps differ
- **Dry-run mode** — preview exactly what would change before touching anything
- **Structured logging** — every sync session writes a clean, aligned log with a header, per-file actions, and a result summary
- **Real-time watcher** — optional `watchdog`-powered watcher for continuous sync on file changes
- **Zero dependencies** — `sync.py` runs on the Python standard library alone

---

## Quick Start

**1. Clone the repo**
```bash
git clone https://github.com/your-username/filesync.git
cd filesync
```

**2. Set your folder paths in `config.json`**
```json
{
  "folder_a": "/path/to/folder_a",
  "folder_b": "/path/to/folder_b"
}
```

**3. Run**
```bash
# Linux / macOS
chmod +x run_sync.sh && ./run_sync.sh

# Windows
run_sync.bat

# Or directly with Python
python sync.py
```

---

## The Four Sync Rules

| Rule | Trigger | Outcome |
|---|---|---|
| **CREATE** | File exists in one folder but not the other | Copied to the missing side |
| **UPDATE** | Same file exists in both, one is newer | Newer version overwrites the older |
| **DELETE** | File present in last sync snapshot, now gone from one side | Deleted from the other side too |
| **CONFLICT** | Same file modified in both folders since last sync | Newer timestamp wins; both folders end up with that version |

> **First-run note:** On the very first run there is no snapshot yet, so deletes are never inferred. Every file found is treated as a CREATE. This is intentional — it's impossible to distinguish "this file was deleted" from "this file was never there" without prior state.

---

## CLI Reference

```bash
# Basic — reads folder paths from config.json
python sync.py

# Explicit paths (overrides config.json)
python sync.py /path/to/folder_a /path/to/folder_b

# Dry run — shows what would happen without making any changes
python sync.py --dry-run

# Verbose output — also prints DEBUG-level lines (skipped files, state saves)
python sync.py --verbose

# Custom config file
python sync.py --config my_config.json

# Custom log file path
python sync.py --log /var/log/mysync.log

# Reset sync state — deletes sync_state.json so the next run is treated as a first run
# Use this if you manually added/removed files and want to re-baseline
python sync.py --reset-state
```

---

## Real-Time Watching

For continuous sync that triggers automatically on any file system change, use the watcher. It debounces rapid events with a 2-second delay to avoid triggering on in-progress saves.

**Install the one optional dependency:**
```bash
pip install watchdog
```

**Run:**
```bash
python watcher.py

# Or with explicit paths
python watcher.py /path/to/folder_a /path/to/folder_b
```

The watcher runs an initial sync on startup, then monitors both folders indefinitely. Press `Ctrl+C` to stop.

---

## Log Format

Every sync run appends a structured session block to `sync.log`:

```
┌──────────────────────────────────────────────────────────────────────┐
│  FOLDERSYNC SESSION  ·  2026-05-14  18:32:01                         │
├──────────────────────────────────────────────────────────────────────┤
│  A  →  /home/user/documents                                          │
│  B  →  /home/user/backup                                             │
└──────────────────────────────────────────────────────────────────────┘
  18:32:01  INFO      Files evaluated                             8 files  |  first_run=False
  18:32:01  INFO      CREATE    notes.txt                                   A → B
  18:32:01  INFO      UPDATE    report.pdf                                  B → A
  18:32:01  WARNING   CONFLICT  budget.xlsx                     [CONFLICT]  B wins
  18:32:01  INFO      DELETE    old_draft.txt                               removed from A  →  purged from B
  18:32:01  DEBUG     SKIP      logo.png                                    identical
  ──────────────────────────────────────────────────────────────────────
  RESULT        Created=1  Updated=1  Deleted=1  Conflicts=1  Skipped=1  Errors=0
  ELAPSED       0.021s
  ──────────────────────────────────────────────────────────────────────
```

The console output mirrors this with ANSI color coding. The log file always captures `DEBUG` level (including skipped files), regardless of the `--verbose` flag.

---

## Automating Runs

**Linux / macOS — cron** (every 5 minutes):
```bash
crontab -e
*/5 * * * * /path/to/filesync/run_sync.sh
```

**Windows — Task Scheduler:**
- Program: `python`
- Arguments: `C:\path\to\filesync\sync.py`
- Trigger: On schedule, on login, on USB connect, etc.

---

## Project Structure

```
filesync/
├── sync.py            # Core sync engine — all four rules, logging, CLI
├── watcher.py         # Real-time file watcher (requires watchdog)
├── test_sync.py       # Test suite — 11 tests covering all rules and edge cases
├── config.json        # Folder path configuration — edit this
├── run_sync.bat       # Windows one-click runner
├── run_sync.sh        # Linux / macOS one-click runner
├── sync_state.json    # Auto-generated after first sync — do not edit
└── sync.log           # Auto-generated log file
```

---

## How Delete Detection Works

After every successful sync, `sync_state.json` is written with a snapshot of every file and its modification time in both folders. On the next run, the engine compares the current state of each folder against that snapshot:

- File in snapshot, still present → normal CREATE / UPDATE / SKIP logic
- File in snapshot, now missing from one side → treated as a deliberate DELETE, propagated to the other side

This is why `sync_state.json` must not be deleted between runs unless you intentionally want to reset state (use `--reset-state` for that).

---

## Running Tests

```bash
python test_sync.py
```

The test suite covers all four sync rules, nested directories, first-run safety (no spurious deletes), dry-run isolation, and conflict resolution with controlled timestamps.

```
Ran 11 tests in 0.13s — OK
```

---

## Requirements

- Python 3.7+
- No external packages for `sync.py` or `test_sync.py`
- `pip install watchdog` only if using `watcher.py`

---

## License

MIT
