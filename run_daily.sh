#!/bin/bash
# Daily Sports Props Runner
# Two phases:
#   Phase 1 (9:00 AM): Pull player stats + grade yesterday
#   Phase 2 (11:30 AM): Fetch fresh lines + generate picks

cd /Users/cailinn/personal/SportsAnalysis

export HOME=/Users/cailinn
export PATH="$HOME/.local/share/mise/installs/python/3.12.11/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
export AWS_SHARED_CREDENTIALS_FILE="$HOME/.aws/credentials"
export AWS_CONFIG_FILE="$HOME/.aws/config"
export AWS_PROFILE=personal
export AWS_DEFAULT_REGION=us-east-1

PHASE="${1:-all}"

if [ "$PHASE" = "stats" ] || [ "$PHASE" = "all" ]; then
    echo "=== Phase 1: Stats + Grading — $(date) ==="

    # Grade yesterday's picks
    python3 common/tracker.py --grade 2>/dev/null

    # Pre-fetch player stats (slow part — nba_api calls)
    # This caches the data for the picks phase later
    echo "Pre-fetching NBA player stats..."
    python3 -c "
import sys; sys.path.insert(0, 'nba/props')
from model import build_player_profile
from fetch import fetch_prizepicks_nba
props, _ = fetch_prizepicks_nba()
players = list(set(p['player'] for p in props))[:30]
for name in players:
    build_player_profile(name)
" 2>/dev/null

    echo "Phase 1 done."
fi

if [ "$PHASE" = "picks" ] || [ "$PHASE" = "all" ]; then
    echo ""
    echo "=== Phase 2: Fresh Lines + Picks — $(date) ==="

    # NBA
    echo "--- NBA ---"
    python3 nba/games/predict.py
    python3 nba/props/fetch.py
    python3 nba/props/picks.py

    # WNBA
    echo "--- WNBA ---"
    python3 wnba/games/predict.py
    python3 wnba/props/fetch.py
    python3 wnba/props/picks.py

    echo ""
    echo "=== Done ==="
fi
