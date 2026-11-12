#!/usr/bin/env bash
# Runs flip-scout forever inside proot-Ubuntu: restart 30 s after a crash.
# Log:        data/flip-scout.log  (rotates at 5 MB, handled by Python)
# Crashes:    data/crash.log       (Python tracebacks before logging starts)
# Config:     after editing profiles.toml or .env: pkill -f flip_scout  -> restarts with new config
set -u
cd "$(dirname "$0")/../.."
mkdir -p data

# only one instance (Termux:Boot + manual start would otherwise double everything)
exec 9>data/run.lock
if ! flock -n 9; then
    echo "flip-scout läuft bereits."
    exit 0
fi

while true; do
    if [ -f data/crash.log ] && [ "$(stat -c%s data/crash.log)" -gt 1000000 ]; then
        mv data/crash.log data/crash.log.1
    fi
    .venv/bin/python -u -m flip_scout --log-file data/flip-scout.log >/dev/null 2>>data/crash.log
    echo "$(date '+%F %T') flip-scout beendet (Code $?) – Neustart in 30 s" >> data/flip-scout.log
    sleep 30
done
