#!/usr/bin/env python3
"""
Sports Props Analyzer — Enhanced Model
Compares player prop lines vs:
1. Season averages (regular season + playoffs)
2. Series-specific performance (vs this opponent)
3. Team defensive stats (what opponent allows)
4. Game context (spread, total, home/away)

Finds edges where the line is significantly off from projected performance.
"""

import requests
import json
import time
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from nba_api.stats.endpoints import (
    playergamelog,
    leaguedashteamstats,
)
from nba_api.stats.static import players as nba_players, teams as nba_teams

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def find_player_id(name):
    """Find NBA player ID from name."""
    results = nba_players.find_players_by_full_name(name)
    if results:
        return results[0]["id"]
    parts = name.split()
    if len(parts) >= 2:
        results = nba_players.find_players_by_last_name(parts[-1])
        for r in results:
            if r["is_active"] and parts[0].lower() in r["full_name"].lower():
                return r["id"]
    return None


def get_game_odds():
    """Get current NBA game odds from ESPN."""
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
                odds_list = comp.get("odds", [])
                competitors = comp.get("competitors", [])

                home_team = away_team = ""
                for c in competitors:
                    if c.get("homeAway") == "home":
                        home_team = c.get("team", {}).get("abbreviation", "")
                    else:
                        away_team = c.get("team", {}).get("abbreviation", "")

                game_info = {
                    "home_team": home_team,
                    "away_team": away_team,
                    "status": event.get("status", {}).get("type", {}).get("description", ""),
                    "date": event.get("date", ""),
                }

                if odds_list:
                    o = odds_list[0]
                    game_info["spread"] = o.get("details", "")
                    game_info["over_under"] = o.get("overUnder", "")
                    game_info["provider"] = o.get("provider", {}).get("name", "")

                games.append(game_info)
        return games
    except Exception:
        return []


def get_team_defense(team_abbr, season="2025-26", season_type="Playoffs"):
    """Get what a team allows per game (opponent stats)."""
    try:
        time.sleep(0.6)
        stats = leaguedashteamstats.LeagueDashTeamStats(
            season=season,
            season_type_all_star=season_type,
            measure_type_detailed_defense="Opponent"
        )
        data = stats.get_normalized_dict()["LeagueDashTeamStats"]

        team = nba_teams.find_team_by_abbreviation(team_abbr)
        if not team:
            return None

        for t in data:
            if t["TEAM_ID"] == team["id"]:
                gp = t.get("GP", 1)
                return {
                    "team": team_abbr,
                    "games_played": gp,
                    "opp_pts_per_game": round(t.get("OPP_PTS", 0) / gp, 1),
                    "opp_reb_per_game": round(t.get("OPP_REB", 0) / gp, 1),
                    "opp_ast_per_game": round(t.get("OPP_AST", 0) / gp, 1),
                    "opp_fg_pct": round(t.get("OPP_FG_PCT", 0), 3),
                    "opp_3p_pct": round(t.get("OPP_FG3_PCT", 0), 3),
                }
        return None
    except Exception:
        return None


def get_player_game_log(player_id, season="2025-26", season_type="Playoffs"):
    """Get player game log."""
    try:
        time.sleep(0.6)
        log = playergamelog.PlayerGameLog(
            player_id=player_id,
            season=season,
            season_type_all_star=season_type
        )
        return log.get_normalized_dict()["PlayerGameLog"]
    except Exception:
        return []


def calculate_props_analysis(games, lines, opponent=None, team_defense=None, game_odds=None):
    """Full analysis of player props vs historical performance."""
    results = {}
    n = len(games)
    if n == 0:
        return results

    # Filter to vs specific opponent if provided
    vs_opponent_games = []
    if opponent:
        vs_opponent_games = [g for g in games if opponent in g.get("MATCHUP", "")]

    for stat_name, line in lines.items():
        # Calculate stat values
        if stat_name == "Points":
            all_values = [g["PTS"] for g in games]
            vs_opp_values = [g["PTS"] for g in vs_opponent_games] if vs_opponent_games else []
        elif stat_name == "Rebounds":
            all_values = [g["REB"] for g in games]
            vs_opp_values = [g["REB"] for g in vs_opponent_games] if vs_opponent_games else []
        elif stat_name == "Assists":
            all_values = [g["AST"] for g in games]
            vs_opp_values = [g["AST"] for g in vs_opponent_games] if vs_opponent_games else []
        elif stat_name == "Pts+Rebs+Asts":
            all_values = [g["PTS"] + g["REB"] + g["AST"] for g in games]
            vs_opp_values = [g["PTS"] + g["REB"] + g["AST"] for g in vs_opponent_games] if vs_opponent_games else []
        elif stat_name == "Pts+Rebs":
            all_values = [g["PTS"] + g["REB"] for g in games]
            vs_opp_values = [g["PTS"] + g["REB"] for g in vs_opponent_games] if vs_opponent_games else []
        elif stat_name == "Pts+Asts":
            all_values = [g["PTS"] + g["AST"] for g in games]
            vs_opp_values = [g["PTS"] + g["AST"] for g in vs_opponent_games] if vs_opponent_games else []
        elif stat_name == "Rebs+Asts":
            all_values = [g["REB"] + g["AST"] for g in games]
            vs_opp_values = [g["REB"] + g["AST"] for g in vs_opponent_games] if vs_opponent_games else []
        else:
            continue

        avg = sum(all_values) / n
        median = sorted(all_values)[n // 2]
        hits_over = sum(1 for v in all_values if v > line)
        over_rate = hits_over / n * 100

        # Series-specific stats
        vs_opp_avg = sum(vs_opp_values) / len(vs_opp_values) if vs_opp_values else None
        vs_opp_over_rate = (sum(1 for v in vs_opp_values if v > line) / len(vs_opp_values) * 100) if vs_opp_values else None

        # Determine recommendation with confidence
        edge = avg - line
        confidence = "LOW"
        if abs(edge) >= 5 and over_rate <= 25:
            confidence = "HIGH"
            recommendation = "UNDER"
        elif abs(edge) >= 5 and over_rate >= 75:
            confidence = "HIGH"
            recommendation = "OVER"
        elif abs(edge) >= 3 and over_rate <= 35:
            confidence = "MEDIUM"
            recommendation = "UNDER"
        elif abs(edge) >= 3 and over_rate >= 65:
            confidence = "MEDIUM"
            recommendation = "OVER"
        elif over_rate <= 40:
            recommendation = "LEAN UNDER"
        elif over_rate >= 60:
            recommendation = "LEAN OVER"
        else:
            recommendation = "SKIP"

        results[stat_name] = {
            "line": line,
            "playoff_avg": round(avg, 1),
            "median": median,
            "over_rate_pct": round(over_rate, 1),
            "under_rate_pct": round(100 - over_rate, 1),
            "edge": round(edge, 1),
            "sample_size": n,
            "vs_opponent_avg": round(vs_opp_avg, 1) if vs_opp_avg else None,
            "vs_opponent_games": len(vs_opp_values),
            "vs_opponent_over_rate": round(vs_opp_over_rate, 1) if vs_opp_over_rate is not None else None,
            "recommendation": recommendation,
            "confidence": confidence,
        }

    return results


def main():
    print(f"=== NBA Props Analyzer (Enhanced) — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # 1. Get game odds
    print("Fetching game odds...")
    game_odds = get_game_odds()
    for g in game_odds:
        spread = g.get("spread", "N/A")
        ou = g.get("over_under", "N/A")
        print(f"  {g['away_team']} @ {g['home_team']} | Spread: {spread} | O/U: {ou} | {g['status']}")
    print()

    # 2. Get team defensive stats
    print("Fetching team defense...")
    team_defense = {}
    for g in game_odds:
        for team in [g["home_team"], g["away_team"]]:
            if team and team not in team_defense:
                defense = get_team_defense(team)
                if defense:
                    team_defense[team] = defense
                    print(f"  {team} allows: {defense['opp_pts_per_game']} pts, {defense['opp_reb_per_game']} reb, {defense['opp_ast_per_game']} ast")
    print()

    # 3. Load props
    date_str = datetime.now().strftime("%Y-%m-%d")
    props_file = DATA_DIR / f"{date_str}_props.json"
    if not props_file.exists():
        files = sorted(DATA_DIR.glob("*_props.json"), reverse=True)
        if files:
            props_file = files[0]

    if not props_file.exists():
        print("No props data. Run fetch_games.py first.")
        return

    with open(props_file) as f:
        props_data = json.load(f)

    players_props = props_data.get("players", {})
    print(f"Analyzing {len(players_props)} players...\n")

    # 4. Analyze each player
    all_results = []
    for player_name, props in players_props.items():
        # Use median line (standard line, not goblin/demon extremes)
        from collections import defaultdict as _dd
        stat_lines = _dd(list)
        team = props[0].get("team", "") if props else ""
        for p in props:
            stat_lines[p["stat"]].append(p["line"])

        lines = {}
        for stat, all_lines in stat_lines.items():
            sorted_l = sorted(all_lines)
            lines[stat] = sorted_l[len(sorted_l) // 2]  # median

        if not lines:
            continue

        player_id = find_player_id(player_name)
        if not player_id:
            continue

        print(f"  {player_name} ({team})...", end=" ")

        # Get playoff games
        playoff_games = get_player_game_log(player_id, "2025-26", "Playoffs")
        if len(playoff_games) < 3:
            playoff_games = get_player_game_log(player_id, "2025-26", "Regular Season")[:15]

        if not playoff_games:
            print("no data")
            continue

        # Determine opponent
        opponent = ""
        for g in game_odds:
            if team == g["home_team"]:
                opponent = g["away_team"]
            elif team == g["away_team"]:
                opponent = g["home_team"]

        # Run analysis
        results = calculate_props_analysis(
            playoff_games, lines,
            opponent=opponent,
            team_defense=team_defense.get(opponent),
            game_odds=game_odds
        )
        print(f"{len(playoff_games)} games, opponent={opponent or 'unknown'}")

        for stat, res in results.items():
            all_results.append({
                "player": player_name,
                "team": team,
                "opponent": opponent,
                "stat": stat,
                **res,
            })

    # 5. Sort and display
    all_results.sort(key=lambda x: abs(x["edge"]), reverse=True)

    # HIGH confidence picks
    high_conf = [r for r in all_results if r["confidence"] == "HIGH"]
    med_conf = [r for r in all_results if r["confidence"] == "MEDIUM"]

    print(f"\n{'='*95}")
    print(f"  🔥 HIGH CONFIDENCE PICKS ({len(high_conf)})")
    print(f"{'='*95}\n")
    print(f"{'Player':<22} {'Stat':<14} {'Line':<6} {'Avg':<6} {'vsOpp':<6} {'Over%':<7} {'Edge':<7} {'Pick':<12} {'Games'}")
    print("-" * 95)
    for r in high_conf[:20]:
        vs_opp = f"{r['vs_opponent_avg']}" if r['vs_opponent_avg'] else "—"
        print(f"{r['player']:<22} {r['stat']:<14} {r['line']:<6} {r['playoff_avg']:<6} {vs_opp:<6} {r['over_rate_pct']:<7} {r['edge']:+.1f}{'':>3} {r['recommendation']:<12} {r['sample_size']}g")

    print(f"\n{'='*95}")
    print(f"  🟡 MEDIUM CONFIDENCE ({len(med_conf)})")
    print(f"{'='*95}\n")
    print(f"{'Player':<22} {'Stat':<14} {'Line':<6} {'Avg':<6} {'vsOpp':<6} {'Over%':<7} {'Edge':<7} {'Pick':<12} {'Games'}")
    print("-" * 95)
    for r in med_conf[:15]:
        vs_opp = f"{r['vs_opponent_avg']}" if r['vs_opponent_avg'] else "—"
        print(f"{r['player']:<22} {r['stat']:<14} {r['line']:<6} {r['playoff_avg']:<6} {vs_opp:<6} {r['over_rate_pct']:<7} {r['edge']:+.1f}{'':>3} {r['recommendation']:<12} {r['sample_size']}g")

    # Game context
    print(f"\n{'='*95}")
    print(f"  📊 GAME CONTEXT")
    print(f"{'='*95}")
    for g in game_odds:
        print(f"  {g['away_team']} @ {g['home_team']} | Spread: {g.get('spread', 'N/A')} | O/U: {g.get('over_under', 'N/A')}")
    for team, defense in team_defense.items():
        print(f"  {team} defense allows: {defense['opp_pts_per_game']}pts {defense['opp_reb_per_game']}reb {defense['opp_ast_per_game']}ast (FG%: {defense['opp_fg_pct']})")

    # Save
    output = {
        "date": date_str,
        "game_odds": game_odds,
        "team_defense": team_defense,
        "analysis": all_results,
        "high_confidence": high_conf,
        "medium_confidence": med_conf,
    }
    output_path = DATA_DIR / f"{date_str}_analysis.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nSaved to {output_path}")


if __name__ == "__main__":
    main()
