# FolderSync v9

> Lightweight two-way folder synchronization in pure Python — no dependencies required.

FolderSync keeps two folders in perfect sync across local drives, external disks, or network shares. It handles all four sync scenarios — creating, updating, deleting, and resolving conflicts — using a state-tracking approach that makes delete detection reliable across runs. v9 adds multi-pair syncing, cloud-drive-aware mtime tolerance, and a pre-flight preview with confirmation before anything is touched.

---

## Features

- **Two-way sync** — changes in either folder propagate to the other
- **Multi-pair support** — sync any number of folder pairs from a single config file, each with its own name and independent state
- **State-aware delete detection** — distinguishes between "file was deleted" and "file never existed"
- **Conflict resolution** — when both sides change, the newer timestamp wins
- **Content-based skip** — MD5 hash comparison avoids unnecessary copies even when timestamps differ
- **Mtime tolerance** — ignores small timestamp drift from cloud-mapped drives (OneDrive, WebDAV, SMB) so re-uploads don't trigger spurious updates
- **Pre-flight preview** — scans and shows every planned CREATE/UPDATE/DELETE/CONFLICT before touching a single file, then asks for confirmation (or skip confirmation with `--yes` for scheduled runs)
- **Dry-run mode** — preview exactly what would change without even asking for confirmation
- **Structured logging** — every sync session writes a clean, aligned log with a header, per-file actions, and a result summary
- **Real-time watcher** — optional `watchdog`-powered watcher for continuous sync on file changes
- **Zero dependencies** — `syncv9.py` runs on the Python standard library alone

---

## Quick Start

**1. Clone the repo**
```bash
git clone https://github.com/your-username/filesync.git
cd filesync
```

**2. Set your folder pairs in `config.json`**
```json
{
  "mtime_tolerance": 60,
  "pairs": [
    {
      "name": "Projects",
      "source": "D:/CODING/New Projects/9_backupSync/v8/A",
      "destination": "D:/CODING/New Projects/9_backupSync/v8/B"
    }
  ]
}
```

**3. Run**
```bash
# Linux / macOS
chmod +x run_sync.sh && ./run_sync.sh

# Windows
run_sync.bat

# Or directly with Python
python syncv9.py
```

Every run first scans both folders and prints a preview of what it plans to do. Nothing is copied or deleted until you confirm (or pass `--yes` to skip the prompt).

---

## The Four Sync Rules

| Rule | Trigger | Outcome |
|---|---|---|
| **CREATE** | File exists in one folder but not the other | Copied to the missing side |
| **UPDATE** | Same file exists in both, one is newer (beyond mtime tolerance) | Newer version overwrites the older |
| **DELETE** | File present in last sync snapshot, now gone from one side | Deleted from the other side too |
| **CONFLICT** | Same file modified in both folders since last sync | Newer timestamp wins; both folders end up with that version |

> **First-run note:** On the very first run there is no snapshot yet, so deletes are never inferred. Every file found is treated as a CREATE. This is intentional — it's impossible to distinguish "this file was deleted" from "this file was never there" without prior state.

---

## Multi-Pair Syncing

`config.json` supports syncing multiple independent folder pairs in a single run. Each pair gets its own name, its own state file, and can optionally override the global mtime tolerance:

```json
{
  "mtime_tolerance": 60,
  "pairs": [
    { "name": "Projects", "source": "D:/A", "destination": "D:/B" },
    { "name": "Docs",     "source": "C:/Work", "destination": "E:/Backup", "mtime_tolerance": 30 }
  ]
}
```

- `source` / `destination` are labels only — sync is always bidirectional.
- `name` is optional but recommended; it's used for the state filename (`sync_state_<name>.json`) and appears in log headers and the multi-pair summary.
- A per-pair `mtime_tolerance` overrides the global value for that pair only.
- The legacy single-pair format (`{"folder_a": ..., "folder_b": ...}`) is still supported for backward compatibility.

Each pair is scanned, previewed, and confirmed independently, so you can approve one pair and skip another in the same run. When more than one pair is configured, a combined summary table is printed at the end showing created/updated/deleted/conflicts/errors per pair.

---

## Mtime Tolerance (Cloud Drive Fix)

Cloud-mapped drives (OneDrive, Google Drive, WebDAV, SMB shares) frequently shift a file's modification time by a few seconds up to ~60 seconds when it's re-uploaded, even though the content hasn't changed. Without accounting for this, every sync run would misread that drift as a genuine edit and trigger an unnecessary UPDATE.

`syncv9.py` ignores mtime differences smaller than a configurable tolerance (default: **60 seconds**):

- Set globally in `config.json` via `"mtime_tolerance": <seconds>`
- Override per pair with a `mtime_tolerance` key inside that pair's config block
- Override at the CLI with `--mtime-tolerance <seconds>` (takes priority over config)
- Set to `0` to disable tolerance entirely and use exact mtime comparison

Content is still verified with an MD5 hash regardless of tolerance, so identical files are always skipped and genuinely changed files are never missed.

---

## Pre-flight Preview & Confirmation

Every run follows a four-pass flow:

1. **Scan** — reads both folders and the last state snapshot, decides every planned action, but touches nothing
2. **Preview** — prints a color-coded table of every CREATE, UPDATE, DELETE, CONFLICT, and SKIP, plus a summary count row
3. **Confirm** — prompts `Proceed with sync? [Y] Yes  [N] No / Abort (default: N)` — press Enter or anything but `y`/`yes` to abort safely
4. **Execute** — only after confirmation does it copy or delete anything, then writes the session log and updates the state snapshot

For unattended/scheduled runs, pass `--yes` (or `-y`) to skip the confirmation prompt automatically. `--dry-run` stops after the preview step and never prompts or writes changes.

---

## CLI Reference

```bash
# Basic — reads folder pairs from config.json, previews, then asks to confirm
python syncv9.py

# Explicit paths (single-pair CLI mode, overrides config.json)
python syncv9.py /path/to/folder_a /path/to/folder_b

# Skip the confirmation prompt — for scheduled/automated runs
python syncv9.py --yes

# Dry run — shows the preview only, never prompts, never changes anything
python syncv9.py --dry-run

# Verbose output — also prints DEBUG-level lines (skipped files, state saves)
python syncv9.py --verbose

# Custom config file
python syncv9.py --config my_config.json

# Custom log file path
python syncv9.py --log /var/log/mysync.log

# Override mtime tolerance (seconds) for this run
python syncv9.py --mtime-tolerance 30

# Reset sync state — deletes state file(s) so the next run is treated as a first run
# Use this if you manually added/removed files and want to re-baseline
python syncv9.py --reset-state
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
│  SOURCE  →  D:/CODING/New Projects/9_backupSync/v8/A                │
│  DEST    →  D:/CODING/New Projects/9_backupSync/v8/B                │
│  MTIME TOLERANCE  →  60s                                             │
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

The console preview mirrors this with ANSI color coding before you confirm. The log file always captures `DEBUG` level (including skipped files), regardless of the `--verbose` flag. In multi-pair runs, each pair writes its own session block, tagged by its `name`.

---

## Automating Runs

Use `--yes` in any scheduled context so the run doesn't hang waiting on a confirmation prompt that no one is there to answer.

**Linux / macOS — cron** (every 5 minutes):
```bash
crontab -e
*/5 * * * * /path/to/filesync/run_sync.sh
```

**Windows — Task Scheduler:**
- Program: `python`
- Arguments: `C:\path\to\filesync\syncv9.py --config config.json --yes`
- Trigger: On schedule, on login, on USB connect, etc.

> Note: `run_sync.bat` as shipped runs interactively (no `--yes`), which is fine for double-click use but will block indefinitely if triggered unattended via Task Scheduler. Add `--yes` to the script or the scheduled task's arguments for hands-off automation.

---

## Project Structure

```
filesync/
├── syncv9.py           # Core sync engine — pairs, tolerance, preview, all 4 rules
├── watcher.py          # Real-time file watcher (requires watchdog)
├── test_sync.py        # Test suite — covers all rules and edge cases
├── config.json         # Folder pair configuration — edit this
├── run_sync.bat        # Windows one-click runner
├── run_sync.sh         # Linux / macOS one-click runner
├── sync_state_*.json   # Auto-generated per pair after first sync — do not edit
└── sync.log            # Auto-generated log file
```

---

## How Delete Detection Works

After every successful sync, a per-pair `sync_state_<name>.json` file is written with a snapshot of every file and its modification time in both folders. On the next run, the engine compares the current state of each folder against that snapshot:

- File in snapshot, still present → normal CREATE / UPDATE / SKIP logic
- File in snapshot, now missing from one side → treated as a deliberate DELETE, propagated to the other side

This is why the state file must not be deleted between runs unless you intentionally want to reset state (use `--reset-state` for that).

---

## Running Tests

```bash
python test_sync.py
```

The test suite covers all four sync rules, nested directories, first-run safety (no spurious deletes), dry-run isolation, mtime-tolerance behavior, and conflict resolution with controlled timestamps.

---

## Requirements

- Python 3.7+
- No external packages for `syncv9.py` or `test_sync.py`
- `pip install watchdog` only if using `watcher.py`

---

## License

MIT