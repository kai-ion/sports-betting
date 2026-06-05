#!/usr/bin/env python3
"""
NBA Props Picks — quantitative edge scoring + Claude validation.
Uses shared picks_engine for scoring and report generation.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))
from picks_engine import (
    ABBR_VARIANTS, compute_hit_rates, score_edge, balance_and_filter,
    get_claude_picks, generate_game_predictions_md, generate_picks_table_md,
    get_stat_val,
)
from errors import PipelineErrors, ErrorType
from nicknames import resolve_name

from model import build_player_profile, project_stats
from analyze import get_game_odds, get_team_defense

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))
from bettingpros import fetch_props, organize_by_player

DATA_DIR = Path(__file__).parent.parent / "data"
REGION = os.environ.get("AWS_REGION", "us-east-1")

errors = PipelineErrors()

# NBA stat keys for game dict
STAT_KEYS = {"Points": "PTS", "Rebounds": "REB", "Assists": "AST",
             "3-Pointers Made": "FG3", "Pts+Rebs+Asts": "PRA",
             "Pts+Rebs": "PR", "Pts+Asts": "PA", "Rebs+Asts": "RA"}


def rank_edges(players_data):
    """Pre-rank all props by quantitative edge score."""
    edges = []

    for player in players_data:
        projections = player.get("projections", {})

        for stat_name, proj in projections.items():
            if proj.get("recommendation") == "SKIP":
                continue

            line = proj["line"]
            projection = proj["projection"]
            edge = proj["edge"]
            direction = "OVER" if edge > 0 else "UNDER"

            # Hit rates (L5, L10, H2H)
            all_games = player.get("recent_games", [])
            l5_hr, l10_hr, h2h_hr = compute_hit_rates(
                all_games, stat_name, line, player.get("opponent", ""), STAT_KEYS
            )
            if direction == "UNDER":
                l5_hr = (1 - l5_hr) if l5_hr is not None else None
                l10_hr = (1 - l10_hr) if l10_hr is not None else None
                h2h_hr = (1 - h2h_hr) if h2h_hr is not None else None
            effective_hr = l10_hr if l10_hr is not None else 0.5

            # Consistency
            consistency_data = player.get("consistency", {})
            if "pts" in stat_name.lower() or stat_name == "Points":
                std = consistency_data.get("pts_std", 8)
            elif "reb" in stat_name.lower() or stat_name == "Rebounds":
                std = consistency_data.get("reb_std", 4)
            elif "ast" in stat_name.lower() or stat_name == "Assists":
                std = consistency_data.get("ast_std", 3)
            else:
                std = consistency_data.get("pts_std", 8)

            # Trend alignment
            trend = player.get("trend", {})
            pts_dir = trend.get("pts_direction", "NEUTRAL")
            trend_aligned = (
                (direction == "OVER" and pts_dir == "HOT") or
                (direction == "UNDER" and pts_dir == "COLD")
            )

            score = score_edge(projection, line, effective_hr, std, trend_aligned)

            edges.append({
                "player": player["player"],
                "team": player["team"],
                "opponent": player["opponent"],
                "prop": stat_name,
                "line": line,
                "pick": direction,
                "projected": projection,
                "edge": edge,
                "edge_pct": round(abs(edge) / max(line, 1) * 100, 1),
                "hit_rate": round(effective_hr * 100),
                "l5_hr": round(l5_hr * 100) if l5_hr is not None else None,
                "l10_hr": round(l10_hr * 100) if l10_hr is not None else None,
                "h2h_hr": round(h2h_hr * 100) if h2h_hr is not None else None,
                "consistency": consistency_data.get("predictability", "MEDIUM"),
                "trend": pts_dir,
                "score": score,
            })

    edges.sort(key=lambda x: x["score"], reverse=True)
    return edges


def main():
    print(f"=== NBA Props Picks — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # 1. Get game context
    print("Fetching game odds...")
    game_odds = get_game_odds()
    game_context = {"games": game_odds}

    # 2. Get props from BettingPros (real sportsbook consensus lines), fallback to Underdog
    print("Fetching props...")
    pp_props = fetch_props("nba")

    # If BettingPros has low coverage, supplement with Underdog
    if len(pp_props) < 30:
        print(f"  BettingPros only has {len(pp_props)} props, supplementing with Underdog...")
        try:
            from fetch import fetch_underdog_nba
            ud_props = fetch_underdog_nba()
            bp_players = {p["player"] for p in pp_props}
            for p in ud_props:
                if p["player"] not in bp_players:
                    pp_props.append(p)
            print(f"  Total after supplement: {len(pp_props)} props")
        except Exception:
            pass

    if not pp_props:
        errors.add(ErrorType.PROPS_EMPTY, "No NBA props from any source")
        print("No props available.")
        return

    by_player = organize_by_player(pp_props)

    # 3. Build profiles and projections
    print("Building player profiles...")
    players_data = []
    for player_name, lines in by_player.items():
        if not lines:
            continue

        team = next((p["team"] for p in pp_props if p["player"] == player_name), "")
        opponent = ""
        is_home = False
        team_variants = ABBR_VARIANTS.get(team, [team])
        for g in game_odds:
            home = g.get("home_team", "")
            away = g.get("away_team", "")
            if any(t == home or t in ABBR_VARIANTS.get(home, [home]) for t in team_variants):
                opponent = away
                is_home = True
                break
            elif any(t == away or t in ABBR_VARIANTS.get(away, [away]) for t in team_variants):
                opponent = home
                is_home = False
                break

        print(f"  {player_name} ({team} vs {opponent})...", end=" ")
        profile = build_player_profile(player_name, opponent=opponent, is_home=is_home)
        if not profile:
            errors.add(ErrorType.GAME_LOG_EMPTY, player_name)
            print("skip")
            continue

        projections = project_stats(profile, lines)
        print("done")

        players_data.append({
            "player": player_name,
            "team": team,
            "opponent": opponent,
            "is_home": is_home,
            "lines": lines,
            "projections": projections,
            "trend": profile.get("trend", {}),
            "consistency": profile.get("consistency", {}),
            "vs_opponent": profile.get("vs_opponent", {}),
            "l5_avg": profile.get("l5_avg", {}),
            "playoffs_avg": profile.get("playoffs_avg", {}),
            "usage": profile.get("usage", {}),
            "home_away": profile.get("home_away", {}),
            "recent_games": profile.get("recent_games", []),
        })

    if not players_data:
        print("No player data built.")
        return

    # 4. Rank edges
    print(f"\nScoring edges for {len(players_data)} players...")
    ranked_edges = rank_edges(players_data)
    print(f"  {len(ranked_edges)} props scored")

    # 5. Filter and balance
    viable_edges = balance_and_filter(ranked_edges)
    print(f"\nClaude validating {len(viable_edges)} edges...")

    # 6. Claude validates
    picks = get_claude_picks(viable_edges, game_context, sport="NBA", errors=errors)

    # 7. Display
    print(f"\n{'='*70}")
    print(f"  FINAL PICKS (model + Claude)")
    print(f"{'='*70}\n")
    for i, pick in enumerate(picks, 1):
        conf_bar = "█" * pick.get("confidence", 0) + "░" * (10 - pick.get("confidence", 0))
        print(f"{i:<3} {pick['player']:<25} {pick['prop']:<14} {pick['line']:<6} {pick['pick']:<6} {pick.get('projected', ''):<6} {conf_bar}")

    # 8. Save JSON
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    output = {
        "date": date_str,
        "picks": picks,
        "ranked_edges": ranked_edges[:20],
        "game_context": game_context,
        "players_analyzed": len(players_data),
    }
    with open(DATA_DIR / f"{date_str}_picks.json", "w") as f:
        json.dump(output, f, indent=2, default=str)

    # 9. Generate report
    md = f"# NBA — {date_str}\n\n"

    # Game predictions
    game_pred_file = DATA_DIR / f"{date_str}_game_predictions.json"
    game_preds = []
    if game_pred_file.exists():
        with open(game_pred_file) as f:
            game_preds = json.load(f).get("predictions", [])
    md += generate_game_predictions_md(game_preds, game_context, errors)

    # Build team→game mapping for picks table
    player_teams = {}
    for p in pp_props:
        if p["player"] and p.get("team"):
            if p["player"] not in player_teams:
                player_teams[p["player"]] = p["team"]

    team_to_game = {}
    for g in game_odds:
        away = g.get("away_team", "")
        home = g.get("home_team", "")
        matchup = f"{away} @ {home}"
        team_to_game[away] = matchup
        team_to_game[home] = matchup
        for alias, variants in ABBR_VARIANTS.items():
            if away in variants:
                team_to_game[alias] = matchup
            if home in variants:
                team_to_game[alias] = matchup

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
