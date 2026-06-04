"""
BettingPros API — fetches consensus sportsbook props for NBA, WNBA, MLB.
Aggregates lines from DraftKings, FanDuel, BetMGM, Caesars, etc.
Free, no API key, works from EC2.
"""

import requests
import json
from collections import defaultdict

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# Market ID → stat name mapping per sport
MARKETS = {
    "nba": {
        151: "Assists",
        156: "Points",
        157: "Rebounds",
        162: "3-Pointers Made",
        335: "Pts+Asts",
        336: "Pts+Rebs",
        337: "Rebs+Asts",
        338: "Pts+Rebs+Asts",
    },
    "wnba": {
        390: "3-Pointers Made",
        391: "Assists",
        393: "Points",
        394: "Pts+Asts",
        395: "Pts+Rebs",
        396: "Pts+Rebs+Asts",
        397: "Rebounds",
        398: "Rebs+Asts",
    },
    "mlb": {
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
    },
}


def fetch_props(sport, max_pages=4):
    """
    Fetch player props from BettingPros for a given sport.
    Returns list of prop dicts with consensus lines from real sportsbooks.
    """
    sport = sport.lower()
    market_map = MARKETS.get(sport, {})
    if not market_map:
        print(f"  Unknown sport: {sport}")
        return []

    print(f"Fetching BettingPros {sport.upper()} props...")
    all_props = []
    page = 1
    total_pages = 1

    while page <= total_pages and page <= max_pages:
        url = f"https://api.bettingpros.com/v3/props?sport={sport}&limit=200&page={page}"
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
                stat_name = market_map.get(market_id)
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

    players = len(set(p["player"] for p in all_props))
    print(f"  Found {len(all_props)} {sport.upper()} props ({players} players)")
    if not all_props:
        print(f"  WARNING: No props returned from BettingPros for {sport.upper()}")
    return all_props


def organize_by_player(props):
    """Group props by player → stat → median line."""
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
