#!/bin/sh
# Start validation independently of this terminal. No point clouds are downloaded.
set -eu
cd -- "$(dirname -- "$0")/.."
mkdir -p logs
if [ -f logs/validate.pid ] && kill -0 "$(cat logs/validate.pid)" 2>/dev/null; then
    echo "A validation process is already running (PID $(cat logs/validate.pid))."
    exit 1
fi
nohup python3 -u validate.py >> logs/validate.log 2>&1 < /dev/null &
echo "$!" > logs/validate.pid
echo "Validation started (PID $!)."
echo "Progress:   python3 validate.py --status"
echo "Log:        tail -f validate.log"
