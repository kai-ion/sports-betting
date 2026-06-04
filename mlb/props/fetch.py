#!/usr/bin/env python3
"""
MLB Props Fetcher — pulls player props from BettingPros (real sportsbook lines).
BettingPros aggregates DraftKings, FanDuel, BetMGM, Caesars, etc.
"""

import requests
import json
from datetime import datetime
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# BettingPros market IDs for MLB
MLB_MARKETS = {
    285: "Pitcher Strikeouts",
    287: "Hits",
    288: "Runs",
    289: "RBIs",
    290: "Earned Runs",
    293: "Total Bases",
    294: "Stolen Bases",
    299: "Home Runs",
    403: "Hits+Runs+RBIs",
    404: "Hits Allowed",
    405: "Outs Recorded",
}

NBA_MARKETS = {
    136: "Points",
    142: "Rebounds",
    147: "Assists",
    151: "Pts+Rebs+Asts",
    152: "Pts+Rebs",
    156: "Pts+Asts",
    157: "Rebs+Asts",
    160: "3-Pointers Made",
}

WNBA_MARKETS = NBA_MARKETS  # Same market IDs


def fetch_bettingpros(sport="mlb", limit=200):
    """Fetch player props from BettingPros API with consensus sportsbook lines."""
    print(f"Fetching BettingPros {sport.upper()} props...")

    all_props = []
    page = 1
    total_pages = 1

    while page <= total_pages and page <= 4:  # Cap at 4 pages
        url = f"https://api.bettingpros.com/v3/props?sport={sport}&limit={limit}&page={page}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"  Error: {resp.status_code}")
                break

            data = resp.json()
            props = data.get("props", [])
            pagination = data.get("_pagination", {})
            total_pages = pagination.get("total_pages", 1)

            for p in props:
                participant = p.get("participant", {})
                player_info = participant.get("player", {})
                over = p.get("over", {})
                under = p.get("under", {})
                projection = p.get("projection", {})

                market_id = p.get("market_id")
                if sport == "mlb":
                    stat_name = MLB_MARKETS.get(market_id)
                elif sport in ("nba", "wnba"):
                    stat_name = NBA_MARKETS.get(market_id)
                else:
                    stat_name = None

                if not stat_name:
                    continue

                all_props.append({
                    "player": participant.get("name", ""),
                    "team": player_info.get("team", ""),
                    "position": player_info.get("position", ""),
                    "stat": stat_name,
                    "line": over.get("consensus_line", over.get("line", 0)),
                    "over_odds": over.get("consensus_odds", over.get("odds")),
                    "under_odds": under.get("consensus_odds", under.get("odds")),
                    "bp_projection": projection.get("value"),
                    "bp_side": projection.get("recommended_side"),
                    "bp_ev": projection.get("expected_value"),
                    "bp_rating": projection.get("bet_rating"),
                    "source": "bettingpros",
                })

            page += 1
        except Exception as e:
            print(f"  Error on page {page}: {e}")
            break

    print(f"  Found {len(all_props)} props ({len(set(p['player'] for p in all_props))} players)")
    return all_props


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
                    probables = c.get("probables", [])
                    team_data = {
                        "abbr": c.get("team", {}).get("abbreviation", ""),
                        "name": c.get("team", {}).get("displayName", ""),
                        "record": c.get("records", [{}])[0].get("summary", "") if c.get("records") else "",
                        "probable_pitcher": probables[0].get("athlete", {}).get("displayName", "") if probables else "",
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
        player_lines[p["player"]][p["stat"]].append(p)

    organized = {}
    for player, stats in player_lines.items():
        organized[player] = {}
        for stat, prop_list in stats.items():
            # Use median line
            sorted_props = sorted(prop_list, key=lambda x: x["line"])
            median = sorted_props[len(sorted_props) // 2]
            organized[player][stat] = median["line"]

    return organized


def main():
    print(f"=== MLB Props Fetcher — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    props = fetch_bettingpros("mlb")
    games = get_games()

    if not props:
        print("No MLB props available today.")
        return

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
        "source": "bettingpros",
        "games": games,
        "total_props": len(props),
        "players": by_player,
        "props_with_projections": [p for p in props if p.get("bp_projection")],
    }
    with open(DATA_DIR / f"{date_str}_props.json", "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Saved to {DATA_DIR / f'{date_str}_props.json'}")


if __name__ == "__main__":
    main()
