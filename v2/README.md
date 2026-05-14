# FolderSync

Two-way folder synchronization with full CREATE / UPDATE / DELETE / CONFLICT handling.  
Pure Python standard library — no pip installs required for basic usage.

---

## Quick Start

**1. Edit `config.json`**
```json
{
  "folder_a": "C:/Users/you/Documents",
  "folder_b": "D:/Backup/Documents"
}
```

**2. Run sync**
```bash
# Windows
run_sync.bat

# Linux / macOS
chmod +x run_sync.sh && ./run_sync.sh

# Or directly
python sync.py
```

---

## The 4 Sync Rules

| Rule | What happens |
|------|-------------|
| **CREATE** | File in A not in B (or vice versa) → copied to the other folder |
| **UPDATE** | Same file in both → newer timestamp overwrites older |
| **DELETE** | File deleted from one folder since last sync → deleted from the other |
| **CONFLICT** | Same file edited in both since last sync → newer timestamp wins, both end up with that version |

---

## CLI Usage

```bash
# Basic — reads from config.json
python sync.py

# Explicit paths
python sync.py /path/to/folder_a /path/to/folder_b

# Dry run — shows what WOULD happen, makes no changes
python sync.py --dry-run

# Verbose output
python sync.py --verbose

# Custom config file
python sync.py --config my_config.json

# Reset sync state (treat as first run — no deletes will be inferred)
python sync.py --reset-state

# Custom log file
python sync.py --log my_sync.log
```

---

## Real-Time Watching (Optional)

Install watchdog once:
```bash
pip install watchdog
```

Then run the watcher — it monitors both folders and syncs automatically on any change:
```bash
python watcher.py
```

The watcher debounces rapid events (2 second delay) to avoid spamming syncs during large file writes or saves.

---

## Project Structure

```
filesync/
├── sync.py            ← Core sync engine (all 4 rules)
├── watcher.py         ← Real-time file watcher (optional)
├── test_sync.py       ← Test suite
├── config.json        ← Your folder paths (edit this)
├── run_sync.bat       ← Windows double-click runner
├── run_sync.sh        ← Linux/macOS runner
├── sync_state.json    ← Auto-generated after first sync (don't edit)
└── sync.log           ← Auto-generated log file
```

---

## How DELETE Detection Works

After every sync, `sync_state.json` is written with a snapshot of every file in both folders. On the next run, if a file was in the snapshot but is now missing from one folder, that's treated as a deliberate deletion and propagated to the other.

> ⚠️ **First run note:** On the very first run (no state file), deletes are never inferred. Every file found is treated as a CREATE. This is intentional — you can't know if a missing file was deleted vs simply never existed.

---

## Scheduling (Automated Runs)

**Windows Task Scheduler:**
- Action: `python C:\path\to\filesync\sync.py`
- Trigger: Every 5 minutes, on login, etc.

**Linux/macOS cron** (every 5 minutes):
```bash
crontab -e
*/5 * * * * /path/to/filesync/run_sync.sh >> /var/log/filesync.log 2>&1
```

---

## Running Tests

```bash
python test_sync.py
```

Tests cover all 4 rules including nested files, first-run behavior, dry-run mode, and conflict resolution.

---

## Requirements

- Python 3.7+
- No external packages needed for `sync.py`
- `pip install watchdog` only if using `watcher.py`
