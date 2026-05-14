"""
FolderSync Watcher — Real-time auto-sync using watchdog
Monitors both folders and triggers sync on any file system event.

Install watchdog first:
    pip install watchdog

Run:
    python watcher.py
    python watcher.py --config config.json
    python watcher.py /path/to/a /path/to/b
"""

import os
import sys
import json
import time
import logging
import argparse
import threading
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("ERROR: watchdog not installed. Run: pip install watchdog")
    sys.exit(1)

from sync import sync_folders, setup_logging

log = logging.getLogger(__name__)

# Debounce timer — avoids spamming syncs during rapid file writes
DEBOUNCE_SECONDS = 2.0


class SyncHandler(FileSystemEventHandler):
    def __init__(self, folder_a: str, folder_b: str):
        self.folder_a = folder_a
        self.folder_b = folder_b
        self._timer = None
        self._lock = threading.Lock()

    def _schedule_sync(self):
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._run_sync)
            self._timer.start()

    def _run_sync(self):
        log.info("Change detected — running sync...")
        result = sync_folders(self.folder_a, self.folder_b)
        print(result.summary())

    def on_any_event(self, event):
        if event.is_directory:
            return
        # Ignore the state file itself
        if "sync_state.json" in event.src_path:
            return
        log.debug(f"Event: {event.event_type} → {event.src_path}")
        self._schedule_sync()


def watch(folder_a: str, folder_b: str):
    handler = SyncHandler(folder_a, folder_b)
    observer = Observer()
    observer.schedule(handler, folder_a, recursive=True)
    observer.schedule(handler, folder_b, recursive=True)
    observer.start()

    log.info(f"Watching for changes...")
    log.info(f"  A: {folder_a}")
    log.info(f"  B: {folder_b}")
    log.info("Press Ctrl+C to stop.\n")

    # Run an initial sync on startup
    log.info("Running initial sync...")
    result = sync_folders(folder_a, folder_b)
    print(result.summary())

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping watcher...")
        observer.stop()
    observer.join()


def main():
    parser = argparse.ArgumentParser(description="FolderSync Watcher — Real-time auto-sync")
    parser.add_argument("folder_a", nargs="?")
    parser.add_argument("folder_b", nargs="?")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    setup_logging("sync.log", args.verbose)

    folder_a = args.folder_a
    folder_b = args.folder_b

    if not folder_a or not folder_b:
        if os.path.exists(args.config):
            with open(args.config) as f:
                cfg = json.load(f)
            folder_a = folder_a or cfg.get("folder_a")
            folder_b = folder_b or cfg.get("folder_b")

    if not folder_a or not folder_b:
        print("ERROR: Provide folder paths as arguments or in config.json")
        sys.exit(1)

    watch(folder_a, folder_b)


if __name__ == "__main__":
    main()
