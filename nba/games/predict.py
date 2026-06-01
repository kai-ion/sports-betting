#!/usr/bin/env python3
"""
NBA Game Predictor — moneyline and spread predictions.
Uses ESPN data for team stats (no nba_api dependency).
"""

import requests
import json
import math
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def get_upcoming_games():
    """Get upcoming NBA games with odds from ESPN."""
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
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
                    }
                    if c.get("homeAway") == "home":
                        home = team_data
                    else:
                        away = team_data

                odds = {}
                if odds_list:
                    o = odds_list[0]
                    ml = o.get("moneyline", {})
                    home_ml = ml.get("home", {}).get("close", {}).get("odds", "")
                    away_ml = ml.get("away", {}).get("close", {}).get("odds", "")
                    odds = {
                        "spread": o.get("details", ""),
                        "over_under": o.get("overUnder", ""),
                        "home_ml": home_ml,
                        "away_ml": away_ml,
                        "provider": o.get("provider", {}).get("name", ""),
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


def get_team_stats():
    """Get NBA team stats from ESPN standings."""
    try:
        resp = requests.get(
            "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings",
            headers=HEADERS, timeout=10
        )
        if resp.status_code != 200:
            return {}

        data = resp.json()
        teams = {}
        for group in data.get("children", []):
            for team_entry in group.get("standings", {}).get("entries", []):
                team = team_entry.get("team", {})
                abbr = team.get("abbreviation", "")
                stats_list = team_entry.get("stats", [])

                stats = {}
                for s in stats_list:
                    name = s.get("name", "")
                    val = s.get("value", 0)
                    stats[name] = val

                teams[abbr] = {
                    "name": team.get("displayName", ""),
                    "wins": int(stats.get("wins", 0)),
                    "losses": int(stats.get("losses", 0)),
                    "win_pct": float(stats.get("winPercent", stats.get("leagueWinPercent", 0))),
                    "ppg": float(stats.get("avgPointsFor", 110)),
                    "opp_ppg": float(stats.get("avgPointsAgainst", 110)),
                    "point_diff": float(stats.get("differential", 0)),
                }
        return teams
    except Exception:
        return {}


def predict_game(home_abbr, away_abbr, team_stats, odds):
    """Predict an NBA game outcome."""
    home = team_stats.get(home_abbr, {})
    away = team_stats.get(away_abbr, {})

    if not home or not away:
        return None

    home_off = home.get("ppg", 110)
    away_def = away.get("opp_ppg", 110)
    away_off = away.get("ppg", 110)
    home_def = home.get("opp_ppg", 110)

    home_court = 3.0

    home_projected = (home_off * 0.5 + away_def * 0.5) + home_court
    away_projected = (away_off * 0.5 + home_def * 0.5)

    # Strength adjustment based on win %
    home_strength = home.get("win_pct", 0.5)
    away_strength = away.get("win_pct", 0.5)
    strength_adj = (home_strength - away_strength) * 8

    home_projected += strength_adj / 2
    away_projected -= strength_adj / 2

    projected_spread = round(away_projected - home_projected, 1)
    projected_total = round(home_projected + away_projected, 1)

    home_win_prob = 1 / (1 + math.exp(-(-projected_spread) / 4))

    return {
        "home_team": home_abbr,
        "away_team": away_abbr,
        "home_projected": round(home_projected, 1),
        "away_projected": round(away_projected, 1),
        "projected_spread": projected_spread,
        "projected_total": projected_total,
        "home_win_prob": round(home_win_prob * 100, 1),
        "away_win_prob": round((1 - home_win_prob) * 100, 1),
        "home_record": f"{home.get('wins', 0)}-{home.get('losses', 0)}",
        "away_record": f"{away.get('wins', 0)}-{away.get('losses', 0)}",
    }


def main():
    print(f"=== NBA Game Predictor — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    games = get_upcoming_games()
    if not games:
        print("No upcoming NBA games.")
        return

    team_stats = get_team_stats()
    print(f"Team stats loaded: {len(team_stats)} teams\n")

    predictions = []
    for game in games:
        if game["status"] != "Scheduled":
            continue

        home_abbr = game["home"]["abbr"]
        away_abbr = game["away"]["abbr"]
        odds = game["odds"]

        pred = predict_game(home_abbr, away_abbr, team_stats, odds)
        if pred:
            pred["market_odds"] = odds
            predictions.append(pred)

    # Display
    print(f"{'='*75}")
    print(f"  NBA PREDICTIONS")
    print(f"{'='*75}\n")

    for p in predictions:
        market = p["market_odds"]
        print(f"  {p['away_team']} ({p['away_record']}) @ {p['home_team']} ({p['home_record']})")
        print(f"  {'─'*50}")
        print(f"  Model:  {p['away_team']} {p['away_projected']} — {p['home_team']} {p['home_projected']}")
        print(f"  Winner: {p['home_team']} {p['home_win_prob']}% | {p['away_team']} {p['away_win_prob']}%")
        print(f"  Spread: Model {p['home_team']} {p['projected_spread']:+.1f} | Market: {market.get('spread', 'N/A')}")
        print(f"  Total:  Model {p['projected_total']} | Market: {market.get('over_under', 'N/A')}")
        print(f"  ML:     {p['away_team']} {market.get('away_ml', '')} / {p['home_team']} {market.get('home_ml', '')}")

        try:
            market_total = float(market.get("over_under", 0))
            total_edge = p["projected_total"] - market_total
            if abs(total_edge) > 4:
                direction = "OVER" if total_edge > 0 else "UNDER"
                print(f"  EDGE: {direction} {market_total} (model: {p['projected_total']}, edge: {total_edge:+.1f})")
        except (ValueError, TypeError):
            pass
        print()

    # Save
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    with open(DATA_DIR / f"{date_str}_game_predictions.json", "w") as f:
        json.dump({"date": date_str, "predictions": predictions}, f, indent=2, default=str)

    print(f"Saved to {DATA_DIR / f'{date_str}_game_predictions.json'}")


if __name__ == "__main__":
    main()
