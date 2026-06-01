#!/usr/bin/env python3
"""
WNBA Props Fetcher — pulls player props from PrizePicks + Underdog.
"""

import requests
import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
PRIZEPICKS_LEAGUE_ID = 3  # WNBA

TARGET_STATS = [
    "Points", "Rebounds", "Assists", "3-Pointers Made",
    "Pts+Rebs+Asts", "Pts+Rebs", "Pts+Asts", "Rebs+Asts",
]


def fetch_underdog_wnba():
    """Fetch WNBA player props from Underdog Fantasy (fallback when PrizePicks blocked)."""
    print("Fetching Underdog Fantasy WNBA props...")
    try:
        resp = requests.get(
            "https://api.underdogfantasy.com/beta/v5/over_under_lines",
            headers=HEADERS, timeout=15
        )
        if resp.status_code != 200:
            print(f"  Underdog error: {resp.status_code}")
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
            over_under = line.get("over_under", {})
            stat_key = over_under.get("appearance_stat", {}).get("stat", "")
            if stat_key not in stat_map:
                continue

            appearance_id = over_under.get("appearance_stat", {}).get("appearance_id", "")
            appearance = appearances.get(appearance_id, {})
            player_id = appearance.get("player_id", "")
            player = players_data.get(player_id, {})

            if player.get("sport_id") != "WNBA":
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

        print(f"  Found {len(props)} WNBA props from Underdog ({len(set(p['player'] for p in props))} players)")
        return props
    except Exception as e:
        print(f"  Underdog fetch error: {e}")
        return []


def fetch_prizepicks_wnba():
    """Fetch WNBA player props from PrizePicks, fallback to Underdog."""
    print("Fetching PrizePicks WNBA props...")
    try:
        resp = requests.get(
            f"https://api.prizepicks.com/projections?league_id={PRIZEPICKS_LEAGUE_ID}&per_page=500",
            headers=HEADERS, timeout=15
        )
        if resp.status_code != 200:
            print(f"  PrizePicks error: {resp.status_code}, trying Underdog...")
            ud_props = fetch_underdog_wnba()
            return ud_props, {}
    except Exception as e:
        print(f"  PrizePicks error: {e}, trying Underdog...")
        ud_props = fetch_underdog_wnba()
        return ud_props, {}

    data = resp.json()
    projections = data.get("data", [])
    included = data.get("included", [])

    players = {}
    for item in included:
        if item.get("type") == "new_player":
            pid = item["id"]
            attrs = item.get("attributes", {})
            players[pid] = {
                "name": attrs.get("display_name", ""),
                "team": attrs.get("team", ""),
                "position": attrs.get("position", ""),
            }

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
            "start_time": attrs.get("start_time", ""),
            "source": "prizepicks",
        })

    print(f"  Found {len(props)} relevant props ({len(set(p['player'] for p in props))} players)")
    return props, players


def get_games():
    """Get WNBA games from ESPN."""
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
            headers=HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return []

        data = resp.json()
        games = []
        for event in data.get("events", []):
            comps = event.get("competitions", [])
            for comp in comps:
                competitors = comp.get("competitors", [])
                odds_list = comp.get("odds", [])

                home = away = {}
                for c in competitors:
                    team_data = {
                        "abbr": c.get("team", {}).get("abbreviation", ""),
                        "name": c.get("team", {}).get("displayName", ""),
                    }
                    if c.get("homeAway") == "home":
                        home = team_data
                    else:
                        away = team_data

                odds = {}
                if odds_list:
                    o = odds_list[0]
                    odds = {
                        "spread": o.get("details", ""),
                        "over_under": o.get("overUnder", ""),
                    }

                games.append({
                    "home": home,
                    "away": away,
                    "odds": odds,
                    "status": event.get("status", {}).get("type", {}).get("description", ""),
                })
        return games
    except Exception:
        return []


def organize_by_player(props):
    """Group props by player with median lines."""
    player_lines = defaultdict(lambda: defaultdict(list))
    for p in props:
        player_lines[p["player"]][p["stat"]].append(p["line"])

    organized = {}
    for player, stats in player_lines.items():
        organized[player] = {}
        for stat, lines in stats.items():
            sorted_lines = sorted(lines)
            organized[player][stat] = sorted_lines[len(sorted_lines) // 2]

    return organized


def display_summary(props, games):
    """Print summary."""
    by_player = organize_by_player(props)

    print(f"\n{'='*80}")
    print(f"  WNBA PLAYER PROPS — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"  {len(props)} props across {len(by_player)} players | {len(games)} games")
    print(f"{'='*80}\n")

    # Show games
    print("  Games:")
    for g in games:
        spread = g["odds"].get("spread", "N/A")
        ou = g["odds"].get("over_under", "N/A")
        print(f"    {g['away']['abbr']} @ {g['home']['abbr']} | {spread} | O/U: {ou}")
    print()

    # Show top players
    print(f"  {'Player':<25} {'Team':<5} {'Pts':<6} {'Reb':<6} {'Ast':<6} {'PRA':<6}")
    print(f"  {'-'*55}")
    sorted_players = sorted(by_player.items(), key=lambda x: x[1].get("Points", 0), reverse=True)
    for player, lines in sorted_players[:25]:
        team = next((p["team"] for p in props if p["player"] == player), "")
        pts = lines.get("Points", "")
        reb = lines.get("Rebounds", "")
        ast = lines.get("Assists", "")
        pra = lines.get("Pts+Rebs+Asts", "")
        print(f"  {player:<25} {team:<5} {pts:<6} {reb:<6} {ast:<6} {pra:<6}")


def main():
    print(f"=== WNBA Props Fetcher — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    props, players = fetch_prizepicks_wnba()
    games = get_games()

    if not props:
        print("No WNBA props available today.")
        return

    display_summary(props, games)

    # Save
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output = {
        "date": date_str,
        "sport": "WNBA",
        "games": games,
        "total_props": len(props),
        "props": props,
        "players": organize_by_player(props),
    }
    output_path = DATA_DIR / f"{date_str}_props.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved to {output_path}")


if __name__ == "__main__":
    main()
