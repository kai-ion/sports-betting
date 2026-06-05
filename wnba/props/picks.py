#!/usr/bin/env python3
"""
WNBA Props Picks — quantitative edge scoring + Claude validation.
Uses shared picks_engine for scoring and report generation.
"""

import json
import os
import sys
import time
import requests
import statistics
from datetime import datetime
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))
from picks_engine import (
    ABBR_VARIANTS, compute_hit_rates, score_edge, balance_and_filter,
    get_claude_picks, generate_game_predictions_md, generate_picks_table_md,
)
from errors import PipelineErrors, ErrorType
from nicknames import resolve_name

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

errors = PipelineErrors()

# WNBA stat keys (lowercase in game dicts)
STAT_KEYS = {"Points": "pts", "Rebounds": "reb", "Assists": "ast",
             "3-Pointers Made": "fg3", "Pts+Rebs+Asts": "PRA",
             "Pts+Rebs": "PR", "Pts+Asts": "PA", "Rebs+Asts": "RA"}


def find_espn_player_id(name):
    """Search ESPN for a WNBA player ID with nickname resolution."""
    search_name = resolve_name(name)
    for attempt_name in [search_name, name] if search_name != name else [name]:
        try:
            resp = requests.get(
                f"https://site.api.espn.com/apis/common/v3/search?query={attempt_name}&type=player&sport=basketball&league=wnba&limit=1",
                headers=HEADERS, timeout=10
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                if items:
                    return items[0].get("id", "")
        except Exception as e:
            errors.add(ErrorType.ESPN_TIMEOUT, f"{name}: {e}")
            return None
    errors.add(ErrorType.PLAYER_NOT_FOUND, name)
    return None


def get_player_game_log(espn_id):
    """Get player game log from ESPN (current + past season)."""
    games = []
    for s in ["2026", "2025"]:
        time.sleep(0.3)
        try:
            url = f"https://site.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{espn_id}/gamelog?season={s}"
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            labels = data.get("labels", [])
            pts_idx = labels.index("PTS") if "PTS" in labels else 1
            reb_idx = labels.index("REB") if "REB" in labels else 2
            ast_idx = labels.index("AST") if "AST" in labels else 3
            min_idx = labels.index("MIN") if "MIN" in labels else 0
            fg3_idx = labels.index("3PM") if "3PM" in labels else (labels.index("3PT") if "3PT" in labels else None)

            # Get opponent info from top-level events
            events_lookup = data.get("events", {})

            for st in data.get("seasonTypes", []):
                for cat in st.get("categories", []):
                    for event in cat.get("events", []):
                        stats = event.get("stats", [])
                        if len(stats) > max(pts_idx, reb_idx, ast_idx):
                            try:
                                eid = event.get("eventId", "")
                                event_info = events_lookup.get(eid, {})
                                opp = event_info.get("opponent", {}).get("abbreviation", "")
                                games.append({
                                    "season": s,
                                    "date": event_info.get("gameDate", ""),
                                    "opponent": opp,
                                    "min": int(stats[min_idx]) if stats[min_idx].isdigit() else 0,
                                    "pts": int(stats[pts_idx]) if stats[pts_idx].isdigit() else 0,
                                    "reb": int(stats[reb_idx]) if stats[reb_idx].isdigit() else 0,
                                    "ast": int(stats[ast_idx]) if stats[ast_idx].isdigit() else 0,
                                    "fg3": int(stats[fg3_idx].split("-")[0]) if fg3_idx and fg3_idx < len(stats) and stats[fg3_idx] else 0,
                                })
                            except (ValueError, IndexError):
                                continue
        except Exception:
            continue
    return games


def compute_player_edge(name, lines, opponent=""):
    """Compute projections and edge scores for a player's props."""
    espn_id = find_espn_player_id(name)
    if not espn_id:
        return None

    games = get_player_game_log(espn_id)
    if len(games) < 5:
        return None

    n = len(games)
    l5 = games[:5]
    l10 = games[:10]

    avg_pts = sum(g["pts"] for g in games) / n
    avg_reb = sum(g["reb"] for g in games) / n
    avg_ast = sum(g["ast"] for g in games) / n
    avg_fg3 = sum(g.get("fg3", 0) for g in games) / n

    l5_pts = sum(g["pts"] for g in l5) / len(l5)
    l5_reb = sum(g["reb"] for g in l5) / len(l5)
    l5_ast = sum(g["ast"] for g in l5) / len(l5)
    l5_fg3 = sum(g.get("fg3", 0) for g in l5) / len(l5)

    l10_pts = sum(g["pts"] for g in l10) / len(l10)
    l10_reb = sum(g["reb"] for g in l10) / len(l10)
    l10_ast = sum(g["ast"] for g in l10) / len(l10)
    l10_fg3 = sum(g.get("fg3", 0) for g in l10) / len(l10)

    pts_std = statistics.stdev([g["pts"] for g in l10]) if len(l10) >= 3 else 5
    reb_std = statistics.stdev([g["reb"] for g in l10]) if len(l10) >= 3 else 3
    ast_std = statistics.stdev([g["ast"] for g in l10]) if len(l10) >= 3 else 2
    fg3_std = statistics.stdev([g.get("fg3", 0) for g in l10]) if len(l10) >= 3 else 1.5

    pts_trend = "HOT" if l5_pts > avg_pts * 1.1 else "COLD" if l5_pts < avg_pts * 0.9 else "NEUTRAL"

    def project(l5_val, l10_val, season_val):
        return l5_val * 0.45 + l10_val * 0.35 + season_val * 0.20

    proj_pts = project(l5_pts, l10_pts, avg_pts)
    proj_reb = project(l5_reb, l10_reb, avg_reb)
    proj_ast = project(l5_ast, l10_ast, avg_ast)
    proj_fg3 = project(l5_fg3, l10_fg3, avg_fg3)

    edges = []
    for stat_name, line in lines.items():
        if stat_name == "Points":
            projection = proj_pts
            std = pts_std
        elif stat_name == "Rebounds":
            projection = proj_reb
            std = reb_std
        elif stat_name == "Assists":
            projection = proj_ast
            std = ast_std
        elif stat_name == "3-Pointers Made":
            projection = proj_fg3
            std = fg3_std
        elif stat_name == "Pts+Rebs+Asts":
            projection = proj_pts + proj_reb + proj_ast
            std = pts_std + reb_std * 0.5
        elif stat_name == "Pts+Rebs":
            projection = proj_pts + proj_reb
            std = pts_std + reb_std * 0.5
        elif stat_name == "Pts+Asts":
            projection = proj_pts + proj_ast
            std = pts_std + ast_std * 0.5
        elif stat_name == "Rebs+Asts":
            projection = proj_reb + proj_ast
            std = reb_std + ast_std * 0.5
        else:
            continue

        edge = projection - line
        direction = "OVER" if edge > 0 else "UNDER"
        edge_pct = abs(edge) / max(line, 1) * 100

        # Skip weak edges
        if edge_pct < 10:
            continue

        # Hit rates
        l5_hr, l10_hr, h2h_hr = compute_hit_rates(games, stat_name, line, opponent, STAT_KEYS)
        if direction == "UNDER":
            l5_hr = (1 - l5_hr) if l5_hr is not None else None
            l10_hr = (1 - l10_hr) if l10_hr is not None else None
            h2h_hr = (1 - h2h_hr) if h2h_hr is not None else None
        effective_hr = l10_hr if l10_hr is not None else 0.5

        trend_aligned = (
            (direction == "OVER" and pts_trend == "HOT") or
            (direction == "UNDER" and pts_trend == "COLD")
        )
        score = score_edge(projection, line, effective_hr, std, trend_aligned)

        if score < 20:
            continue

        edges.append({
            "player": name,
            "prop": stat_name,
            "line": line,
            "pick": direction,
            "projected": round(projection, 1),
            "edge": round(edge, 1),
            "edge_pct": round(edge_pct, 1),
            "hit_rate": round(effective_hr * 100),
            "l5_hr": round(l5_hr * 100) if l5_hr is not None else None,
            "l10_hr": round(l10_hr * 100) if l10_hr is not None else None,
            "h2h_hr": round(h2h_hr * 100) if h2h_hr is not None else None,
            "trend": pts_trend,
            "score": score,
            "games_sample": n,
        })

    return edges


def main():
    print(f"=== WNBA Props Picks — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    date_str = datetime.now().strftime("%Y-%m-%d")

    # Fetch from BettingPros (real sportsbook consensus lines), fallback to Underdog
    from bettingpros import fetch_props
    from bettingpros import organize_by_player as bp_organize
    from fetch import get_games, fetch_prizepicks_wnba, fetch_underdog_wnba

    props = fetch_props("wnba")
    games = get_games()

    # If BettingPros has low coverage, supplement with Underdog
    if len(props) < 30:
        print(f"  BettingPros only has {len(props)} props, supplementing with Underdog...")
        ud_props = fetch_underdog_wnba()
        # Only add Underdog props for players NOT already in BettingPros
        bp_players = {p["player"] for p in props}
        for p in ud_props:
            if p["player"] not in bp_players:
                props.append(p)
        print(f"  Total after supplement: {len(props)} props")

    if not props:
        errors.add(ErrorType.PROPS_EMPTY, "No WNBA props from any source")
        print("No WNBA props available.")
        return

    players_lines = bp_organize(props)

    # Build player→team and team→opponent lookups
    player_team_map = {}
    if props:
        for p in (props if isinstance(props, list) else []):
            if p.get("player") and p.get("team"):
                if p["player"] not in player_team_map:
                    player_team_map[p["player"]] = p["team"]

    team_opponent = {}
    for g in games:
        away = g.get("away", {}).get("abbr", "")
        home = g.get("home", {}).get("abbr", "")
        if away and home:
            team_opponent[away] = home
            team_opponent[home] = away
            for alias in ABBR_VARIANTS.get(away, []):
                team_opponent[alias] = home
            for alias in ABBR_VARIANTS.get(home, []):
                team_opponent[alias] = away
    # Also get opponents from Underdog game titles
    try:
        ud_resp = requests.get("https://api.underdogfantasy.com/beta/v5/over_under_lines",
                               headers=HEADERS, timeout=10)
        if ud_resp.status_code == 200:
            ud_data = ud_resp.json()
            ud_players = {p["id"]: p for p in ud_data.get("players", [])}
            for g in ud_data.get("games", ud_data.get("matches", [])):
                title = g.get("abbreviated_title", "")
                if " @ " in title:
                    parts = title.split(" @ ")
                    a, h = parts[0].strip(), parts[1].strip()
                    if a not in team_opponent:
                        team_opponent[a] = h
                    if h not in team_opponent:
                        team_opponent[h] = a
    except Exception:
        pass

    # Compute edges for each player
    player_names = list(players_lines.keys())
    print(f"Computing edges for {len(player_names)} players from ESPN game logs...")

    all_edges = []
    for name in player_names[:30]:
        time.sleep(0.3)
        team = player_team_map.get(name, "")
        opponent = team_opponent.get(team, "")
        player_edges = compute_player_edge(name, players_lines[name], opponent=opponent)
        if player_edges:
            for e in player_edges:
                e["team"] = team
            all_edges.extend(player_edges)
            print(f"  {name}: {len(player_edges)} edges")

    all_edges.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  Total: {len(all_edges)} edges scored")

    if not all_edges:
        print("No edges found.")
        return

    # Filter, balance, and validate
    viable_edges = balance_and_filter(all_edges)
    print(f"\nClaude validating {len(viable_edges)} edges...")
    picks = get_claude_picks(viable_edges, games, sport="WNBA", errors=errors)

    # Display
    print(f"\n{'='*70}")
    print(f"  WNBA — FINAL PICKS (model + Claude)")
    print(f"{'='*70}\n")
    for i, pick in enumerate(picks, 1):
        conf = pick.get("confidence", 0)
        bar = "█" * conf + "░" * (10 - conf)
        print(f"{i:<3} {pick['player']:<25} {pick['prop']:<8} {pick['line']:<6} {pick['pick']:<6} {pick.get('projected', ''):<6} {bar}")

    # Save JSON
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        "date": date_str,
        "sport": "WNBA",
        "picks": picks,
        "ranked_edges": all_edges[:20],
        "games": games,
        "players_analyzed": len(player_names),
    }
    with open(DATA_DIR / f"{date_str}_picks.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    # Generate report
    md = f"# WNBA — {date_str}\n\n"

    # Game predictions
    game_pred_file = DATA_DIR / f"{date_str}_game_predictions.json"
    game_preds = []
    if game_pred_file.exists():
        with open(game_pred_file) as f:
            game_preds = json.load(f).get("predictions", [])
    md += generate_game_predictions_md(game_preds, games, errors)

    # Build team→game mapping
    team_to_game = {}
    for g in games:
        away = g.get("away", {}).get("abbr", "")
        home = g.get("home", {}).get("abbr", "")
        if away and home:
            matchup = f"{away} @ {home}"
            team_to_game[away] = matchup
            team_to_game[home] = matchup
    # Add Underdog game titles
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

    # Expand team_to_game with abbreviation variants
    for team, game in list(team_to_game.items()):
        for alias in ABBR_VARIANTS.get(team, []):
            if alias not in team_to_game:
                team_to_game[alias] = game

    player_teams = {e["player"]: e.get("team", "") for e in viable_edges}
    md += generate_picks_table_md(picks, viable_edges, team_to_game, player_teams, errors)
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
