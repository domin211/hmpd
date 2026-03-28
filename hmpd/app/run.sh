#!/usr/bin/with-contenv sh
set -eu

echo "Starting HMPD bridge runner"

while true; do
    echo "Launching /app/main.py"
    python3 -u /app/main.py
    exit_code=$?

    echo "main.py exited with code ${exit_code}, restarting in 5 seconds..."
    sleep 5
done
