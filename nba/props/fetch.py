#!/usr/bin/env python3
"""
Sports Props Analyzer — Game & Props Data Fetcher
Pulls NBA games, moneylines, and player props from free sources.

Data sources:
- PrizePicks API (props — free, no key)
- Underdog Fantasy API (props — free, no key)
- nba_api (player stats — free, no key)
"""

import requests
import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Stats we care about
TARGET_STATS = [
    "Points", "Rebounds", "Assists", "3-Pointers Made",
    "Pts+Rebs+Asts", "Pts+Rebs", "Pts+Asts", "Rebs+Asts",
]


def fetch_prizepicks_nba():
    """Fetch NBA player props from PrizePicks (free, no key). Falls back to Underdog."""
    print("Fetching PrizePicks NBA props...")
    try:
        resp = requests.get(
            "https://api.prizepicks.com/projections?league_id=7&per_page=500",
            headers=HEADERS, timeout=15
        )
        if resp.status_code != 200:
            print(f"  PrizePicks error: {resp.status_code}, using Underdog only")
            ud_props = fetch_underdog_nba()
            return ud_props, {}
    except Exception as e:
        print(f"  PrizePicks error: {e}, using Underdog only")
        ud_props = fetch_underdog_nba()
        return ud_props, {}

    data = resp.json()
    projections = data.get("data", [])
    included = data.get("included", [])

    # Build player lookup
    players = {}
    for item in included:
        if item.get("type") == "new_player":
            pid = item["id"]
            attrs = item.get("attributes", {})
            players[pid] = {
                "name": attrs.get("display_name", ""),
                "team": attrs.get("team", ""),
                "position": attrs.get("position", ""),
                "image_url": attrs.get("image_url", ""),
            }

    # Parse props
    props = []
    for p in projections:
        attrs = p.get("attributes", {})
        stat_type = attrs.get("stat_type", "")
        if stat_type not in TARGET_STATS:
            continue

        player_rel = p.get("relationships", {}).get("new_player", {}).get("data", {})
        player_id = player_rel.get("id", "")
        player_info = players.get(player_id, {})

        props.append({
            "player": player_info.get("name", attrs.get("description", "")),
            "team": player_info.get("team", ""),
            "position": player_info.get("position", ""),
            "stat": stat_type,
            "line": float(attrs.get("line_score", 0)),
            "description": attrs.get("description", ""),
            "start_time": attrs.get("start_time", ""),
            "source": "prizepicks",
        })

    print(f"  Found {len(props)} relevant props ({len(set(p['player'] for p in props))} players)")
    return props, players


def fetch_underdog_nba():
    """Fetch NBA player props from Underdog Fantasy (free, no key)."""
    print("Fetching Underdog Fantasy props...")
    resp = requests.get(
        "https://api.underdogfantasy.com/beta/v5/over_under_lines",
        headers=HEADERS, timeout=15
    )
    if resp.status_code != 200:
        print(f"  Error: {resp.status_code}")
        return []

    data = resp.json()
    lines = data.get("over_under_lines", [])
    players_data = {p["id"]: p for p in data.get("players", [])}
    appearances = {a["id"]: a for a in data.get("appearances", [])}

    # Build team UUID → abbreviation map from games
    team_map = {}
    for game in data.get("games", data.get("matches", [])):
        title = game.get("abbreviated_title", "")
        if " @ " in title:
            away_abbr, home_abbr = title.split(" @ ")
            team_map[game.get("away_team_id", "")] = away_abbr.strip()
            team_map[game.get("home_team_id", "")] = home_abbr.strip()

    # Filter to NBA and our target stats
    stat_map = {
        "points": "Points",
        "rebounds": "Rebounds",
        "assists": "Assists",
        "three_points_made": "3-Pointers Made",
        "pts_rebs_asts": "Pts+Rebs+Asts",
        "pts_rebs": "Pts+Rebs",
        "pts_asts": "Pts+Asts",
        "rebs_asts": "Rebs+Asts",
    }

    props = []
    for line in lines:
        stat_key = line.get("over_under", {}).get("appearance_stat", {}).get("stat", "")
        if stat_key not in stat_map:
            continue

        appearance_id = line.get("over_under", {}).get("appearance_stat", {}).get("appearance_id", "")
        appearance = appearances.get(appearance_id, {})

        player_id = appearance.get("player_id", "")
        player = players_data.get(player_id, {})

        if player.get("sport_id") != "NBA":
            continue

        team_id = player.get("team_id", "")
        team_abbr = team_map.get(team_id, team_id)

        props.append({
            "player": f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
            "team": team_abbr,
            "position": player.get("position", ""),
            "stat": stat_map[stat_key],
            "line": float(line.get("stat_value", 0)),
            "source": "underdog",
        })

    print(f"  Found {len(props)} relevant NBA props")
    return props


def get_games_from_props(props):
    """Extract unique games from the props data."""
    games = set()
    for p in props:
        desc = p.get("description", "")
        if "@" in desc or "vs" in desc.lower():
            games.add(desc)
        elif p.get("team"):
            games.add(p["team"])
    return sorted(games)


def organize_by_player(props):
    """Group props by player for easy comparison."""
    by_player = defaultdict(list)
    for p in props:
        by_player[p["player"]].append(p)
    return dict(by_player)


def display_summary(all_props):
    """Print a clean summary of available props."""
    by_player = organize_by_player(all_props)

    # Sort by most props available
    sorted_players = sorted(by_player.items(), key=lambda x: len(x[1]), reverse=True)

    print(f"\n{'='*80}")
    print(f"  NBA PLAYER PROPS SUMMARY — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"  {len(all_props)} props across {len(by_player)} players")
    print(f"{'='*80}\n")

    print(f"{'Player':<25} {'Team':<5} {'Pts':<6} {'Reb':<6} {'Ast':<6} {'PRA':<6} {'P+R':<6} {'P+A':<6} {'R+A':<6}")
    print("-" * 75)

    for player, props in sorted_players[:30]:
        team = props[0].get("team", "")
        lines = {}
        for p in props:
            stat = p["stat"]
            # Take the most common line (mode) or first one
            if stat not in lines:
                lines[stat] = p["line"]

        pts = lines.get("Points", "")
        reb = lines.get("Rebounds", "")
        ast = lines.get("Assists", "")
        pra = lines.get("Pts+Rebs+Asts", "")
        pr = lines.get("Pts+Rebs", "")
        pa = lines.get("Pts+Asts", "")
        ra = lines.get("Rebs+Asts", "")

        print(f"{player:<25} {team:<5} {pts:<6} {reb:<6} {ast:<6} {pra:<6} {pr:<6} {pa:<6} {ra:<6}")


def main():
    print(f"=== NBA Props Fetcher — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # Fetch from both sources
    pp_props, pp_players = fetch_prizepicks_nba()
    ud_props = fetch_underdog_nba()

    # Combine
    all_props = pp_props + ud_props
    print(f"\nTotal: {len(all_props)} props from {len(set(p['player'] for p in all_props))} players")

    # Display summary
    display_summary(all_props)

    # Save raw data
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output = {
        "date": date_str,
        "fetch_time": datetime.now().isoformat(),
        "sources": ["prizepicks", "underdog"],
        "total_props": len(all_props),
        "props": all_props,
        "players": organize_by_player(all_props),
    }
    output_path = DATA_DIR / f"{date_str}_props.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {output_path}")


if __name__ == "__main__":
    main()
