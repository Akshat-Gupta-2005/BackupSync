#!/bin/bash
# FolderSync — Linux/macOS runner
# chmod +x run_sync.sh, then ./run_sync.sh
# Or add to cron: */5 * * * * /path/to/run_sync.sh

cd "$(dirname "$0")"

echo "================================"
echo "  FolderSync - Two-Way Sync"
echo "================================"

python3 sync.py --config config.json

if [ $? -ne 0 ]; then
    echo ""
    echo "[ERROR] Sync completed with errors. Check sync.log for details."
    exit 1
else
    echo ""
    echo "[OK] Sync completed successfully."
fi
