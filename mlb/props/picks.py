#!/usr/bin/env python3
"""
MLB Props Picks — quantitative edge scoring + Claude validation.
Uses pitcher/batter matchup data for prop edges.
"""

import json
import os
import sys
import time
import requests
from datetime import datetime
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))
from picks_engine import (
    ABBR_VARIANTS, score_edge, balance_and_filter,
    get_claude_picks, generate_picks_table_md,
)
from errors import PipelineErrors, ErrorType

from fetch import fetch_prizepicks_mlb, fetch_underdog_mlb, get_games, organize_by_player
from model import get_pitcher_profile, get_batter_profile

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

errors = PipelineErrors()


def compute_pitcher_k_edge(pitcher_profile, line):
    """Compute edge for pitcher strikeout prop."""
    if not pitcher_profile:
        return None

    l5 = pitcher_profile.get("l5", {})
    season = pitcher_profile.get("season", {})
    recent = pitcher_profile.get("recent_starts", [])

    # Projection: 50% L5 avg, 30% season rate, 20% game log consistency
    l5_k = l5.get("avg_k", 0)
    season_k_per_start = 0
    if season.get("games_started") and season.get("strikeouts"):
        season_k_per_start = season["strikeouts"] / season["games_started"]
    elif season.get("k_per_9") and season.get("ip"):
        season_k_per_start = (season["k_per_9"] / 9) * (season["ip"] / max(season.get("games_started", 1), 1))

    if l5_k == 0 and season_k_per_start == 0:
        return None

    projection = l5_k * 0.50 + season_k_per_start * 0.30 + l5_k * 0.20

    # Hit rate
    hits_over = sum(1 for g in recent[:10] if g.get("strikeouts", 0) > line)
    l10_hr = hits_over / max(len(recent[:10]), 1)
    l5_hits = sum(1 for g in recent[:5] if g.get("strikeouts", 0) > line)
    l5_hr = l5_hits / max(len(recent[:5]), 1)

    edge = projection - line
    direction = "OVER" if edge > 0 else "UNDER"

    if direction == "UNDER":
        l10_hr = 1 - l10_hr
        l5_hr = 1 - l5_hr

    # Consistency
    if len(recent) >= 3:
        import statistics
        std = statistics.stdev([g.get("strikeouts", 0) for g in recent[:10]])
    else:
        std = 3

    return {
        "projection": round(projection, 1),
        "edge": round(edge, 1),
        "direction": direction,
        "l5_hr": round(l5_hr * 100),
        "l10_hr": round(l10_hr * 100),
        "std": std,
    }


def compute_batter_edge(batter_profile, stat_name, line):
    """Compute edge for batter props (hits, total bases, HRR, runs, RBI)."""
    if not batter_profile:
        return None

    recent = batter_profile.get("recent_games", [])
    season = batter_profile.get("season", {})

    if not recent or not season:
        return None

    # Map stat name to game log key
    stat_key_map = {
        "Hits": "hits",
        "Total Bases": "total_bases",
        "Home Runs": "home_runs",
        "Runs": "runs",
        "RBIs": "rbi",
        "Stolen Bases": "stolen_bases",
        "Walks": "walks",
        "Hits+Runs+RBIs": "hrr",
        "Batter Strikeouts": "strikeouts",
        "Singles": "singles",
        "Doubles": "doubles",
    }

    key = stat_key_map.get(stat_name)
    if not key:
        return None

    # Get per-game values
    if key == "hrr":
        game_vals = [g.get("hits", 0) + g.get("runs", 0) + g.get("rbi", 0) for g in recent]
    else:
        game_vals = [g.get(key, 0) for g in recent]

    if not game_vals:
        return None

    # Projection: 45% L5, 35% L10, 20% season per-game
    l5_avg = sum(game_vals[:5]) / max(len(game_vals[:5]), 1)
    l10_avg = sum(game_vals[:10]) / max(len(game_vals[:10]), 1)

    # Season per-game rate
    games = season.get("games", 1) or 1
    if key == "hrr":
        season_rate = (season.get("hits", 0) + season.get("runs", 0) + season.get("rbi", 0)) / games
    elif key in season:
        season_rate = season[key] / games
    else:
        season_rate = l10_avg

    projection = l5_avg * 0.45 + l10_avg * 0.35 + season_rate * 0.20

    # Hit rates
    l5_hits = sum(1 for v in game_vals[:5] if v > line)
    l5_hr = l5_hits / max(len(game_vals[:5]), 1)
    l10_hits = sum(1 for v in game_vals[:10] if v > line)
    l10_hr = l10_hits / max(len(game_vals[:10]), 1)

    edge = projection - line
    direction = "OVER" if edge > 0 else "UNDER"

    if direction == "UNDER":
        l5_hr = 1 - l5_hr
        l10_hr = 1 - l10_hr

    # Consistency
    import statistics
    std = statistics.stdev(game_vals[:10]) if len(game_vals) >= 3 else 1.5

    return {
        "projection": round(projection, 1),
        "edge": round(edge, 1),
        "direction": direction,
        "l5_hr": round(l5_hr * 100),
        "l10_hr": round(l10_hr * 100),
        "std": std,
    }


def compute_hr_edge(batter_profile, line, facing_pitcher=None):
    """
    Specialized HR prop edge.
    HR props are volatile — weight power indicators heavily:
    - HR/game rate (season + L10)
    - Total bases rate (proxy for power)
    - Facing pitcher's HR/9
    """
    if not batter_profile:
        return None

    recent = batter_profile.get("recent_games", [])
    season = batter_profile.get("season", {})

    if not recent or not season:
        return None

    games = season.get("games", 1) or 1
    hr_per_game = season.get("home_runs", 0) / games

    # L10 HR rate
    l10_hrs = [g.get("home_runs", 0) for g in recent[:10]]
    l10_hr_rate = sum(l10_hrs) / max(len(l10_hrs), 1)

    # L5 HR rate
    l5_hrs = [g.get("home_runs", 0) for g in recent[:5]]
    l5_hr_rate = sum(l5_hrs) / max(len(l5_hrs), 1)

    # Pitcher HR/9 boost
    pitcher_boost = 1.0
    if facing_pitcher:
        pitcher_hr9 = facing_pitcher.get("season", {}).get("hr_per_9", 1.0)
        if pitcher_hr9 > 1.5:
            pitcher_boost = 1.15  # Pitcher gives up lots of HRs
        elif pitcher_hr9 < 0.8:
            pitcher_boost = 0.85  # Pitcher suppresses HRs

    projection = (l5_hr_rate * 0.40 + l10_hr_rate * 0.35 + hr_per_game * 0.25) * pitcher_boost

    # Hit rate (how often they hit 1+ HR)
    l5_hit = sum(1 for v in l5_hrs if v > line) / max(len(l5_hrs), 1)
    l10_hit = sum(1 for v in l10_hrs if v > line) / max(len(l10_hrs), 1)

    edge = projection - line
    direction = "OVER" if edge > 0 else "UNDER"

    if direction == "UNDER":
        l5_hit = 1 - l5_hit
        l10_hit = 1 - l10_hit

    import statistics
    std = statistics.stdev(l10_hrs) if len(l10_hrs) >= 3 else 0.8

    return {
        "projection": round(projection, 2),
        "edge": round(edge, 2),
        "direction": direction,
        "l5_hr": round(l5_hit * 100),
        "l10_hr": round(l10_hit * 100),
        "std": std,
    }


def compute_hrr_edge(batter_profile, line, facing_pitcher=None):
    """
    Specialized Hits+Runs+RBIs edge.
    Combines three counting stats — high-upside prop that rewards:
    - Lineup position (more AB = more chances)
    - Team scoring environment
    - Batter's multi-category production
    """
    if not batter_profile:
        return None

    recent = batter_profile.get("recent_games", [])
    season = batter_profile.get("season", {})

    if not recent or not season:
        return None

    # Per-game HRR values
    game_vals = [g.get("hits", 0) + g.get("runs", 0) + g.get("rbi", 0) for g in recent]

    if not game_vals:
        return None

    l5_avg = sum(game_vals[:5]) / max(len(game_vals[:5]), 1)
    l10_avg = sum(game_vals[:10]) / max(len(game_vals[:10]), 1)

    games = season.get("games", 1) or 1
    season_hrr = (season.get("hits", 0) + season.get("runs", 0) + season.get("rbi", 0)) / games

    # Pitcher WHIP boost (high WHIP = more baserunners = more runs/RBI)
    pitcher_boost = 1.0
    if facing_pitcher:
        whip = facing_pitcher.get("season", {}).get("whip", 1.3)
        if whip > 1.4:
            pitcher_boost = 1.10
        elif whip < 1.1:
            pitcher_boost = 0.90

    projection = (l5_avg * 0.45 + l10_avg * 0.35 + season_hrr * 0.20) * pitcher_boost

    # Hit rates
    l5_hit = sum(1 for v in game_vals[:5] if v > line) / max(len(game_vals[:5]), 1)
    l10_hit = sum(1 for v in game_vals[:10] if v > line) / max(len(game_vals[:10]), 1)

    edge = projection - line
    direction = "OVER" if edge > 0 else "UNDER"

    if direction == "UNDER":
        l5_hit = 1 - l5_hit
        l10_hit = 1 - l10_hit

    import statistics
    std = statistics.stdev(game_vals[:10]) if len(game_vals) >= 3 else 2.0

    return {
        "projection": round(projection, 1),
        "edge": round(edge, 1),
        "direction": direction,
        "l5_hr": round(l5_hit * 100),
        "l10_hr": round(l10_hit * 100),
        "std": std,
    }


def rank_all_edges(players_lines, player_team_map, games):
    """Score all props and rank by edge."""
    edges = []

    # Build opponent lookup from games
    team_pitcher = {}
    for g in games:
        if g.get("status") != "Scheduled":
            continue
        away = g.get("away", {}).get("abbr", "")
        home = g.get("home", {}).get("abbr", "")
        away_pitcher = g.get("away", {}).get("probable_pitcher", "")
        home_pitcher = g.get("home", {}).get("probable_pitcher", "")
        team_pitcher[away] = {"opponent": home, "facing_pitcher": home_pitcher}
        team_pitcher[home] = {"opponent": away, "facing_pitcher": away_pitcher}

    pitcher_props = ["Pitcher Strikeouts", "Pitching Outs", "Earned Runs Allowed", "Hits Allowed"]
    batter_props = ["Hits", "Total Bases", "Home Runs", "Runs", "RBIs", "Stolen Bases",
                    "Walks", "Hits+Runs+RBIs", "Batter Strikeouts"]

    for player_name, lines in players_lines.items():
        team = player_team_map.get(player_name, "")
        matchup_info = team_pitcher.get(team, {})

        for stat_name, line in lines.items():
            result = None

            if stat_name in pitcher_props:
                # Pitcher prop
                profile = get_pitcher_profile(player_name)
                if not profile:
                    continue

                if stat_name == "Pitcher Strikeouts":
                    result = compute_pitcher_k_edge(profile, line)
                elif stat_name in ("Earned Runs Allowed", "Hits Allowed", "Pitching Outs"):
                    # Use game log for ER/Hits/Outs like strikeouts
                    recent = profile.get("recent_starts", [])
                    if not recent:
                        continue
                    key_map = {
                        "Earned Runs Allowed": "earned_runs",
                        "Hits Allowed": "hits",
                        "Pitching Outs": "ip",
                    }
                    key = key_map[stat_name]
                    game_vals = [g.get(key, 0) for g in recent]
                    # For pitching outs, convert IP to outs (6.0 IP = 18 outs)
                    if stat_name == "Pitching Outs":
                        game_vals = [int(v) * 3 + int((v % 1) * 10) for v in game_vals]

                    l5_avg = sum(game_vals[:5]) / max(len(game_vals[:5]), 1)
                    l10_avg = sum(game_vals[:10]) / max(len(game_vals[:10]), 1)
                    projection = l5_avg * 0.50 + l10_avg * 0.50

                    l5_hit = sum(1 for v in game_vals[:5] if v > line) / max(len(game_vals[:5]), 1)
                    l10_hit = sum(1 for v in game_vals[:10] if v > line) / max(len(game_vals[:10]), 1)

                    edge = projection - line
                    direction = "OVER" if edge > 0 else "UNDER"
                    if direction == "UNDER":
                        l5_hit = 1 - l5_hit
                        l10_hit = 1 - l10_hit

                    import statistics as _stats
                    std = _stats.stdev(game_vals[:10]) if len(game_vals) >= 3 else 1.5
                    result = {
                        "projection": round(projection, 1),
                        "edge": round(edge, 1),
                        "direction": direction,
                        "l5_hr": round(l5_hit * 100),
                        "l10_hr": round(l10_hit * 100),
                        "std": std,
                    }

            elif stat_name in batter_props:
                # Batter prop — use specialized functions for HR and HRR
                profile = get_batter_profile(player_name)
                facing_pitcher_name = matchup_info.get("facing_pitcher", "")
                facing_pitcher = get_pitcher_profile(facing_pitcher_name) if facing_pitcher_name else None

                if stat_name == "Home Runs" and profile:
                    result = compute_hr_edge(profile, line, facing_pitcher)
                elif stat_name == "Hits+Runs+RBIs" and profile:
                    result = compute_hrr_edge(profile, line, facing_pitcher)
                elif profile:
                    result = compute_batter_edge(profile, stat_name, line)

            if not result:
                continue

            # Skip binary 0.5 lines — these are just "yes/no" with trivial UNDER edges
            if line == 0.5:
                continue

            edge_pct = abs(result["edge"]) / max(line, 0.5) * 100
            if edge_pct < 15:
                continue

            # For UNDER picks on low lines (1.5), require very high hit rate
            # Otherwise every batter with <1.5 avg is UNDER which isn't useful
            if result["direction"] == "UNDER" and line <= 1.5 and result["l10_hr"] < 70:
                continue

            effective_hr = result["l10_hr"] / 100
            trend_aligned = result["l5_hr"] > result["l10_hr"]

            score = score_edge(
                result["projection"], line, effective_hr, result["std"], trend_aligned
            )

            if score < 20:
                continue

            edges.append({
                "player": player_name,
                "team": team,
                "opponent": matchup_info.get("opponent", ""),
                "prop": stat_name,
                "line": line,
                "pick": result["direction"],
                "projected": result["projection"],
                "edge": result["edge"],
                "edge_pct": round(edge_pct, 1),
                "hit_rate": result["l10_hr"],
                "l5_hr": result["l5_hr"],
                "l10_hr": result["l10_hr"],
                "h2h_hr": None,
                "score": score,
            })

    edges.sort(key=lambda x: x["score"], reverse=True)
    return edges


def main():
    print(f"=== MLB Props Picks — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    date_str = datetime.now().strftime("%Y-%m-%d")

    # Fetch props
    pp_props, _ = fetch_prizepicks_mlb()
    ud_props = fetch_underdog_mlb()
    all_props = pp_props + ud_props
    games = get_games()

    if not all_props:
        errors.add(ErrorType.PROPS_EMPTY, "No MLB props from any source")
        print("No MLB props available.")
        return

    # Filter: only keep meaningful lines (drop all 0.5 batter lines, keep pitcher props + batter 1.5+)
    pitcher_stats = {"Pitcher Strikeouts", "Pitching Outs", "Earned Runs Allowed", "Hits Allowed"}
    filtered_props = []
    for p in all_props:
        if p["stat"] in pitcher_stats:
            filtered_props.append(p)
        elif p["line"] >= 1.5:
            filtered_props.append(p)

    players_lines = organize_by_player(filtered_props)
    player_team_map = {}
    for p in filtered_props:
        if p.get("player") and p.get("team"):
            if p["player"] not in player_team_map:
                player_team_map[p["player"]] = p["team"]

    # Cap at 60 players to manage API calls
    if len(players_lines) > 60:
        # Prioritize: pitchers first, then batters with most prop types
        pitcher_players = {name for name, lines in players_lines.items() if any(s in pitcher_stats for s in lines)}
        batter_players = sorted(
            [(name, len(lines)) for name, lines in players_lines.items() if name not in pitcher_players],
            key=lambda x: -x[1]
        )
        keep = pitcher_players | {name for name, _ in batter_players[:60 - len(pitcher_players)]}
        players_lines = {name: lines for name, lines in players_lines.items() if name in keep}

    print(f"\n{len(players_lines)} players with lines, {len(games)} games")
    print(f"Computing edges...")

    # Score all edges
    ranked_edges = rank_all_edges(players_lines, player_team_map, games)
    print(f"  {len(ranked_edges)} edges scored")

    if not ranked_edges:
        print("No viable edges found.")
        return

    # Show top edges
    print(f"\n{'='*70}")
    print(f"  TOP QUANTITATIVE EDGES")
    print(f"{'='*70}\n")
    print(f"{'#':<3} {'Player':<22} {'Prop':<18} {'Line':<6} {'Pick':<6} {'Proj':<6} {'Edge%':<6} {'Score'}")
    print("-" * 70)
    for i, e in enumerate(ranked_edges[:15], 1):
        print(f"{i:<3} {e['player']:<22} {e['prop']:<18} {e['line']:<6} {e['pick']:<6} {e['projected']:<6} {e['edge_pct']:<6} {e['score']}")

    # Filter and send to Claude
    # MLB needs stricter balance — lines are set high so UNDERs dominate
    viable_edges = balance_and_filter(ranked_edges, min_edge=15, balance_threshold=0.6, balance_min_edge=25)
    print(f"\nClaude validating {len(viable_edges)} edges...")

    game_context = {"games": [{"away": g["away"]["abbr"], "home": g["home"]["abbr"],
                               "odds": g["odds"], "away_pitcher": g["away"].get("probable_pitcher", ""),
                               "home_pitcher": g["home"].get("probable_pitcher", "")}
                              for g in games if g["status"] == "Scheduled"]}

    picks = get_claude_picks(viable_edges, game_context, sport="MLB", errors=errors)

    # Display
    print(f"\n{'='*70}")
    print(f"  MLB — FINAL PICKS")
    print(f"{'='*70}\n")
    for i, pick in enumerate(picks, 1):
        conf = pick.get("confidence", 0)
        bar = "█" * conf + "░" * (10 - conf)
        print(f"{i:<3} {pick['player']:<22} {pick['prop']:<18} {pick['line']:<6} {pick['pick']:<6} {pick.get('projected', ''):<6} {bar}")

    # Save JSON
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "date": date_str,
        "sport": "MLB",
        "picks": picks,
        "ranked_edges": ranked_edges[:20],
        "games": game_context,
        "players_analyzed": len(players_lines),
    }
    with open(DATA_DIR / f"{date_str}_picks.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Generate report
    md = f"# MLB — {date_str}\n\n"

    # Games section
    md += "## Today's Games\n\n"
    md += "| Away | Home | Pitchers | Spread | O/U |\n"
    md += "|------|------|----------|--------|-----|\n"
    for g in games:
        away_p = g["away"].get("probable_pitcher", "TBD")
        home_p = g["home"].get("probable_pitcher", "TBD")
        spread = g["odds"].get("spread", "N/A")
        ou = g["odds"].get("over_under", "N/A")
        md += f"| {g['away']['abbr']} | {g['home']['abbr']} | {away_p} vs {home_p} | {spread} | {ou} |\n"
    md += "\n"

    # Picks section
    team_to_game = {}
    for g in games:
        away = g["away"]["abbr"]
        home = g["home"]["abbr"]
        matchup = f"{away} @ {home}"
        team_to_game[away] = matchup
        team_to_game[home] = matchup
    # Also get from Underdog games for teams ESPN didn't list
    try:
        ud_resp = requests.get("https://api.underdogfantasy.com/beta/v5/over_under_lines",
                               headers=HEADERS, timeout=10)
        if ud_resp.status_code == 200:
            for g in ud_resp.json().get("games", ud_resp.json().get("matches", [])):
                title = g.get("abbreviated_title", "")
                if " @ " in title:
                    parts = title.split(" @ ")
                    team_to_game[parts[0].strip()] = title
                    team_to_game[parts[1].strip()] = title
    except Exception:
        pass

    md += generate_picks_table_md(picks, viable_edges, team_to_game, player_team_map, errors)
    md += errors.to_markdown()

    REPORTS_DIR = DATA_DIR.parent / "reports"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(REPORTS_DIR / f"{date_str}.md", "w") as f:
        f.write(md)

    if errors.has_errors():
        print(errors.summary())
    print(f"\nSaved to {REPORTS_DIR / f'{date_str}.md'}")


if __name__ == "__main__":
    main()
