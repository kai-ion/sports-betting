#!/usr/bin/env python3
"""
MLB Props Fetcher — pulls player props from Underdog Fantasy + PrizePicks.
"""

import requests
import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
PRIZEPICKS_LEAGUE_ID = 2  # MLB

TARGET_STATS = [
    "Hits+Runs+RBIs", "Total Bases", "Hits", "Runs", "RBIs",
    "Home Runs", "Stolen Bases", "Walks",
    "Pitcher Strikeouts", "Pitching Outs", "Earned Runs Allowed",
    "Hits Allowed",
]

# Underdog stat mapping
UNDERDOG_STAT_MAP = {
    "hits_runs_rbis": "Hits+Runs+RBIs",
    "total_bases": "Total Bases",
    "hits": "Hits",
    "runs": "Runs",
    "rbis": "RBIs",
    "home_runs": "Home Runs",
    "stolen_bases": "Stolen Bases",
    "walks": "Walks",
    "pitcher_strikeouts": "Pitcher Strikeouts",
    "pitching_outs": "Pitching Outs",
    "earned_runs_allowed": "Earned Runs Allowed",
    "hits_allowed": "Hits Allowed",
    "batter_strikeouts": "Batter Strikeouts",
    "singles": "Singles",
    "doubles": "Doubles",
}


def fetch_prizepicks_mlb():
    """Fetch MLB player props from PrizePicks."""
    print("Fetching PrizePicks MLB props...")
    try:
        resp = requests.get(
            f"https://api.prizepicks.com/projections?league_id={PRIZEPICKS_LEAGUE_ID}&per_page=500",
            headers=HEADERS, timeout=15
        )
        if resp.status_code != 200:
            print(f"  PrizePicks error: {resp.status_code}")
            return [], {}

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
                "player": player_info.get("name", ""),
                "team": player_info.get("team", ""),
                "position": player_info.get("position", ""),
                "stat": stat_type,
                "line": float(attrs.get("line_score", 0)),
                "source": "prizepicks",
            })

        print(f"  Found {len(props)} MLB props ({len(set(p['player'] for p in props))} players)")
        return props, players
    except Exception as e:
        print(f"  PrizePicks error: {e}")
        return [], {}


def fetch_underdog_mlb():
    """Fetch MLB player props from Underdog Fantasy."""
    print("Fetching Underdog Fantasy MLB props...")
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

        props = []
        for line in lines:
            over_under = line.get("over_under", {})
            stat_key = over_under.get("appearance_stat", {}).get("stat", "")
            if stat_key not in UNDERDOG_STAT_MAP:
                continue

            appearance_id = over_under.get("appearance_stat", {}).get("appearance_id", "")
            appearance = appearances.get(appearance_id, {})
            player_id = appearance.get("player_id", "")
            player = players_data.get(player_id, {})

            if player.get("sport_id") != "MLB":
                continue

            team_id = player.get("team_id", "")
            team_abbr = team_map.get(team_id, team_id)

            props.append({
                "player": f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
                "team": team_abbr,
                "position": player.get("position", ""),
                "stat": UNDERDOG_STAT_MAP[stat_key],
                "line": float(line.get("stat_value", 0)),
                "source": "underdog",
            })

        print(f"  Found {len(props)} MLB props from Underdog ({len(set(p['player'] for p in props))} players)")
        return props
    except Exception as e:
        print(f"  Underdog error: {e}")
        return []


def get_games():
    """Get today's MLB games from ESPN."""
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
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
                        "record": c.get("records", [{}])[0].get("summary", "") if c.get("records") else "",
                        "probable_pitcher": c.get("probables", [{}])[0].get("athlete", {}).get("displayName", "") if c.get("probables") else "",
                    }
                    if c.get("homeAway") == "home":
                        home = team_data
                    else:
                        away = team_data

                odds = {}
                if odds_list:
                    o = odds_list[0]
                    ml = o.get("moneyline", {})
                    odds = {
                        "spread": o.get("details", ""),
                        "over_under": o.get("overUnder", ""),
                        "home_ml": ml.get("home", {}).get("close", {}).get("odds", ""),
                        "away_ml": ml.get("away", {}).get("close", {}).get("odds", ""),
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


def main():
    print(f"=== MLB Props Fetcher — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # Try PrizePicks first, fallback to Underdog
    pp_props, _ = fetch_prizepicks_mlb()
    ud_props = fetch_underdog_mlb()
    props = pp_props + ud_props

    games = get_games()

    if not props:
        print("No MLB props available today.")
        return

    # Display
    by_player = organize_by_player(props)
    print(f"\n{'='*80}")
    print(f"  MLB PLAYER PROPS — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"  {len(props)} props across {len(by_player)} players | {len(games)} games")
    print(f"{'='*80}\n")

    # Show games
    print("  Games:")
    for g in games:
        if g["status"] == "Scheduled":
            away_p = g["away"].get("probable_pitcher", "TBD")
            home_p = g["home"].get("probable_pitcher", "TBD")
            spread = g["odds"].get("spread", "N/A")
            ou = g["odds"].get("over_under", "N/A")
            print(f"    {g['away']['abbr']} ({away_p}) @ {g['home']['abbr']} ({home_p}) | {spread} | O/U: {ou}")
    print()

    # Save
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output = {
        "date": date_str,
        "sport": "MLB",
        "games": games,
        "total_props": len(props),
        "props": props,
        "players": by_player,
    }
    with open(DATA_DIR / f"{date_str}_props.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved to {DATA_DIR / f'{date_str}_props.json'}")


if __name__ == "__main__":
    main()
