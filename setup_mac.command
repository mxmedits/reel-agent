#!/bin/bash
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || brew install ffmpeg
python3 -m pip install -r requirements.txt
[ -f config.yaml ] || cp config.example.yaml config.yaml
[ -f .env ] || cp .env.example .env
echo; echo "Next: put your Claude API key in .env, then run:  python3 -m reelagent setup-instagram"
