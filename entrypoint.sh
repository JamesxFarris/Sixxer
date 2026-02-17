#!/bin/bash
# Start Xvfb virtual display in background
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
sleep 1

# Verify Xvfb is running
if ! pgrep -x Xvfb > /dev/null; then
    echo "ERROR: Xvfb failed to start"
    exit 1
fi

echo "Xvfb started on display :99"

# Run the application
exec python main.py
