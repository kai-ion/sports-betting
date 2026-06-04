#!/usr/bin/env python3
"""
MLB Player Model — pitcher and batter profiles from pybaseball + MLB Stats API.
Provides the data needed for edge scoring on player props.
"""

import json
import time
import requests
import statistics
from datetime import datetime, timedelta
from pathlib import Path

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
CACHE_DIR = Path(__file__).parent / "cache"

try:
    from pybaseball import (
        playerid_lookup,
        statcast_pitcher,
        statcast_batter,
        pitching_stats,
        batting_stats,
    )
    HAS_PYBASEBALL = True
except ImportError:
    HAS_PYBASEBALL = False


def _get_cached(name, category):
    """Load today's cached data."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_name = name.replace(" ", "_").replace("'", "")
    cache_file = CACHE_DIR / f"{date_str}_{safe_name}_{category}.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    return None


def _save_cache(name, category, data):
    """Save to today's cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_name = name.replace(" ", "_").replace("'", "")
    cache_file = CACHE_DIR / f"{date_str}_{safe_name}_{category}.json"
    with open(cache_file, "w") as f:
        json.dump(data, f)


def get_pitcher_profile(name):
    """Build pitcher profile with advanced stats."""
    cached = _get_cached(name, "pitcher")
    if cached:
        return cached

    # Try MLB Stats API for game log
    profile = _get_pitcher_from_mlb_api(name)
    if profile:
        _save_cache(name, "pitcher", profile)
        return profile

    # Fallback: pybaseball
    if HAS_PYBASEBALL:
        profile = _get_pitcher_from_pybaseball(name)
        if profile:
            _save_cache(name, "pitcher", profile)
            return profile

    return None


def _get_pitcher_from_mlb_api(name):
    """Get pitcher stats from MLB Stats API."""
    try:
        # Search for player
        resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/search?names={name}&sportIds=1",
            headers=HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return None

        people = resp.json().get("people", [])
        if not people:
            return None

        player_id = people[0]["id"]
        full_name = people[0].get("fullName", name)
        throws = people[0].get("pitchHand", {}).get("code", "R")

        # Get season stats
        time.sleep(0.3)
        stats_resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&season=2026&group=pitching",
            headers=HEADERS, timeout=10
        )

        season_stats = {}
        if stats_resp.status_code == 200:
            splits = stats_resp.json().get("stats", [{}])[0].get("splits", [])
            if splits:
                s = splits[0].get("stat", {})
                season_stats = {
                    "era": float(s.get("era", 0)),
                    "whip": float(s.get("whip", 0)),
                    "k_per_9": float(s.get("strikeoutsPer9Inn", 0)),
                    "bb_per_9": float(s.get("walksPer9Inn", 0)),
                    "hr_per_9": float(s.get("homeRunsPer9", 0)),
                    "ip": float(s.get("inningsPitched", 0)),
                    "strikeouts": int(s.get("strikeOuts", 0)),
                    "walks": int(s.get("baseOnBalls", 0)),
                    "hits_allowed": int(s.get("hits", 0)),
                    "earned_runs": int(s.get("earnedRuns", 0)),
                    "games_started": int(s.get("gamesStarted", 0)),
                }

        # Get game log for recent starts
        time.sleep(0.3)
        log_resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=gameLog&season=2026&group=pitching",
            headers=HEADERS, timeout=10
        )

        recent_starts = []
        if log_resp.status_code == 200:
            splits = log_resp.json().get("stats", [{}])[0].get("splits", [])
            for split in splits[:10]:
                s = split.get("stat", {})
                opp = split.get("opponent", {}).get("abbreviation", "")
                recent_starts.append({
                    "date": split.get("date", ""),
                    "opponent": opp,
                    "ip": float(s.get("inningsPitched", 0)),
                    "hits": int(s.get("hits", 0)),
                    "earned_runs": int(s.get("earnedRuns", 0)),
                    "strikeouts": int(s.get("strikeOuts", 0)),
                    "walks": int(s.get("baseOnBalls", 0)),
                    "home_runs": int(s.get("homeRuns", 0)),
                    "pitches": int(s.get("numberOfPitches", 0)),
                })

        # L5 averages
        l5 = recent_starts[:5]
        l5_stats = {}
        if l5:
            l5_stats = {
                "avg_ip": round(sum(g["ip"] for g in l5) / len(l5), 1),
                "avg_k": round(sum(g["strikeouts"] for g in l5) / len(l5), 1),
                "avg_er": round(sum(g["earned_runs"] for g in l5) / len(l5), 1),
                "avg_hits": round(sum(g["hits"] for g in l5) / len(l5), 1),
                "avg_walks": round(sum(g["walks"] for g in l5) / len(l5), 1),
            }

        return {
            "name": full_name,
            "player_id": player_id,
            "throws": throws,
            "season": season_stats,
            "l5": l5_stats,
            "recent_starts": recent_starts,
            "games_sample": len(recent_starts),
        }
    except Exception:
        return None


def _get_pitcher_from_pybaseball(name):
    """Fallback: get pitcher data from pybaseball/FanGraphs."""
    try:
        parts = name.split()
        if len(parts) < 2:
            return None
        lookup = playerid_lookup(parts[-1], parts[0])
        if lookup.empty:
            return None
        # Get FanGraphs pitching stats
        stats = pitching_stats(2026, qual=1)
        player_row = stats[stats["Name"].str.contains(name, case=False)]
        if player_row.empty:
            return None
        row = player_row.iloc[0]
        return {
            "name": name,
            "season": {
                "era": float(row.get("ERA", 0)),
                "fip": float(row.get("FIP", 0)),
                "xfip": float(row.get("xFIP", 0)),
                "whip": float(row.get("WHIP", 0)),
                "k_pct": float(row.get("K%", 0)),
                "bb_pct": float(row.get("BB%", 0)),
                "k_per_9": float(row.get("K/9", 0)),
                "hr_per_9": float(row.get("HR/9", 0)),
            },
            "recent_starts": [],
            "games_sample": 0,
        }
    except Exception:
        return None


def get_batter_profile(name):
    """Build batter profile with splits."""
    cached = _get_cached(name, "batter")
    if cached:
        return cached

    profile = _get_batter_from_mlb_api(name)
    if profile:
        _save_cache(name, "batter", profile)
    return profile


def _get_batter_from_mlb_api(name):
    """Get batter stats from MLB Stats API."""
    try:
        resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/search?names={name}&sportIds=1",
            headers=HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return None

        people = resp.json().get("people", [])
        if not people:
            return None

        player_id = people[0]["id"]
        full_name = people[0].get("fullName", name)
        bats = people[0].get("batSide", {}).get("code", "R")

        # Season stats
        time.sleep(0.3)
        stats_resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=season&season=2026&group=hitting",
            headers=HEADERS, timeout=10
        )

        season_stats = {}
        if stats_resp.status_code == 200:
            splits = stats_resp.json().get("stats", [{}])[0].get("splits", [])
            if splits:
                s = splits[0].get("stat", {})
                season_stats = {
                    "avg": float(s.get("avg", 0)),
                    "obp": float(s.get("obp", 0)),
                    "slg": float(s.get("slg", 0)),
                    "ops": float(s.get("ops", 0)),
                    "hits": int(s.get("hits", 0)),
                    "home_runs": int(s.get("homeRuns", 0)),
                    "rbi": int(s.get("rbi", 0)),
                    "stolen_bases": int(s.get("stolenBases", 0)),
                    "strikeouts": int(s.get("strikeOuts", 0)),
                    "walks": int(s.get("baseOnBalls", 0)),
                    "games": int(s.get("gamesPlayed", 0)),
                    "at_bats": int(s.get("atBats", 0)),
                    "total_bases": int(s.get("totalBases", 0)),
                    "runs": int(s.get("runs", 0)),
                }

        # Game log for recent form
        time.sleep(0.3)
        log_resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{player_id}/stats?stats=gameLog&season=2026&group=hitting",
            headers=HEADERS, timeout=10
        )

        recent_games = []
        if log_resp.status_code == 200:
            splits = log_resp.json().get("stats", [{}])[0].get("splits", [])
            for split in splits[:15]:
                s = split.get("stat", {})
                opp = split.get("opponent", {}).get("abbreviation", "")
                recent_games.append({
                    "date": split.get("date", ""),
                    "opponent": opp,
                    "hits": int(s.get("hits", 0)),
                    "at_bats": int(s.get("atBats", 0)),
                    "home_runs": int(s.get("homeRuns", 0)),
                    "rbi": int(s.get("rbi", 0)),
                    "runs": int(s.get("runs", 0)),
                    "stolen_bases": int(s.get("stolenBases", 0)),
                    "strikeouts": int(s.get("strikeOuts", 0)),
                    "walks": int(s.get("baseOnBalls", 0)),
                    "total_bases": int(s.get("totalBases", 0)),
                })

        # L5 and L10 averages
        l5 = recent_games[:5]
        l10 = recent_games[:10]

        def avg_stat(games, key):
            if not games:
                return 0
            return round(sum(g.get(key, 0) for g in games) / len(games), 2)

        return {
            "name": full_name,
            "player_id": player_id,
            "bats": bats,
            "season": season_stats,
            "l5": {
                "hits": avg_stat(l5, "hits"),
                "total_bases": avg_stat(l5, "total_bases"),
                "runs": avg_stat(l5, "runs"),
                "rbi": avg_stat(l5, "rbi"),
                "home_runs": avg_stat(l5, "home_runs"),
                "strikeouts": avg_stat(l5, "strikeouts"),
                "walks": avg_stat(l5, "walks"),
            },
            "l10": {
                "hits": avg_stat(l10, "hits"),
                "total_bases": avg_stat(l10, "total_bases"),
                "runs": avg_stat(l10, "runs"),
                "rbi": avg_stat(l10, "rbi"),
                "home_runs": avg_stat(l10, "home_runs"),
                "strikeouts": avg_stat(l10, "strikeouts"),
                "walks": avg_stat(l10, "walks"),
            },
            "recent_games": recent_games,
            "games_sample": len(recent_games),
        }
    except Exception:
        return None


def get_batter_vs_pitcher(batter_name, pitcher_name):
    """Get batter's career stats against a specific pitcher."""
    cached = _get_cached(f"{batter_name}_vs_{pitcher_name}", "h2h")
    if cached:
        return cached

    try:
        # Get batter ID
        resp = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/search?names={batter_name}&sportIds=1",
            headers=HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return None
        batter_people = resp.json().get("people", [])
        if not batter_people:
            return None
        batter_id = batter_people[0]["id"]

        # Get pitcher ID
        time.sleep(0.3)
        resp2 = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/search?names={pitcher_name}&sportIds=1",
            headers=HEADERS, timeout=10
        )
        if resp2.status_code != 200:
            return None
        pitcher_people = resp2.json().get("people", [])
        if not pitcher_people:
            return None
        pitcher_id = pitcher_people[0]["id"]

        # Get batter vs pitcher stats
        time.sleep(0.3)
        resp3 = requests.get(
            f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=vsPlayer&opposingPlayerId={pitcher_id}&group=hitting",
            headers=HEADERS, timeout=10
        )
        if resp3.status_code != 200:
            return None

        splits = resp3.json().get("stats", [{}])[0].get("splits", [])
        if not splits:
            return None

        s = splits[0].get("stat", {})
        ab = int(s.get("atBats", 0))
        if ab < 3:
            return None  # Too small sample

        result = {
            "at_bats": ab,
            "hits": int(s.get("hits", 0)),
            "home_runs": int(s.get("homeRuns", 0)),
            "strikeouts": int(s.get("strikeOuts", 0)),
            "walks": int(s.get("baseOnBalls", 0)),
            "avg": s.get("avg", ""),
            "ops": s.get("ops", ""),
            "rbi": int(s.get("rbi", 0)),
        }
        _save_cache(f"{batter_name}_vs_{pitcher_name}", "h2h", result)
        return result
    except Exception:
        return None


if __name__ == "__main__":
    print("Testing MLB model...")
    p = get_pitcher_profile("Gerrit Cole")
    if p:
        print(f"Pitcher: {p['name']} ({p['throws']})")
        print(f"  ERA: {p['season'].get('era')}, K/9: {p['season'].get('k_per_9')}")
        print(f"  L5 avg K: {p['l5'].get('avg_k')}")
    b = get_batter_profile("Aaron Judge")
    if b:
        print(f"Batter: {b['name']} ({b['bats']})")
        print(f"  AVG: {b['season'].get('avg')}, HR: {b['season'].get('home_runs')}")
        print(f"  L5 hits/game: {b['l5'].get('hits')}")
    h2h = get_batter_vs_pitcher("Aaron Judge", "Slade Cecconi")
    if h2h:
        print(f"  vs Cecconi: {h2h['hits']}/{h2h['at_bats']} ({h2h['avg']}), {h2h['home_runs']} HR")
