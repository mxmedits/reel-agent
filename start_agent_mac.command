#!/bin/bash
# Double-click to start the Reel Agent. Add it to System Settings > General > Login Items to run at login.
cd "$(dirname "$0")"
python3 -m reelagent watch
