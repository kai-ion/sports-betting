#!/usr/bin/env python3
"""
NBA Props Picks — quantitative edge scoring + Claude validation.
The code computes projections, hit rates, and edge scores.
Claude validates the top edges and removes any with hidden risk.
"""

import boto3
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))
from errors import PipelineErrors, ErrorType

from fetch import fetch_prizepicks_nba
from model import build_player_profile, project_stats
from analyze import get_game_odds, get_team_defense

DATA_DIR = Path(__file__).parent.parent / "data"
REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = "us.anthropic.claude-sonnet-4-6"

errors = PipelineErrors()


def _get_stat_val(g, stat_name):
    """Extract the stat value from a game dict."""
    if stat_name == "Points":
        return g.get("PTS", 0)
    elif stat_name == "Rebounds":
        return g.get("REB", 0)
    elif stat_name == "Assists":
        return g.get("AST", 0)
    elif stat_name == "3-Pointers Made":
        return g.get("FG3", g.get("3PM", 0))
    elif stat_name == "Pts+Rebs+Asts":
        return g.get("PTS", 0) + g.get("REB", 0) + g.get("AST", 0)
    elif stat_name == "Pts+Rebs":
        return g.get("PTS", 0) + g.get("REB", 0)
    elif stat_name == "Pts+Asts":
        return g.get("PTS", 0) + g.get("AST", 0)
    elif stat_name == "Rebs+Asts":
        return g.get("REB", 0) + g.get("AST", 0)
    return 0


def _hit_rate_over(games, stat_name, line):
    """Fraction of games where player went OVER the line."""
    if not games:
        return None
    hits = sum(1 for g in games if _get_stat_val(g, stat_name) > line)
    return hits / len(games)


def compute_hit_rates(profile, stat_name, line, opponent=""):
    """Compute L5, L10, and H2H hit rates."""
    all_games = profile.get("recent_games", [])

    l10 = all_games[:10]
    l5 = all_games[:5]
    h2h = [g for g in all_games if opponent and opponent in g.get("MATCHUP", "")] if opponent else []

    l10_hr = _hit_rate_over(l10, stat_name, line)
    l5_hr = _hit_rate_over(l5, stat_name, line)
    h2h_hr = _hit_rate_over(h2h, stat_name, line) if h2h else None

    return l5_hr, l10_hr, h2h_hr


def score_edge(projection, line, hit_rate, consistency, trend_aligned):
    """
    Composite edge score (0-100). Higher = stronger pick.
    - Edge size (40%): how far projection is from line as % of line
    - Hit rate (30%): how often player actually clears this line
    - Consistency (20%): low std dev = more predictable
    - Trend (10%): bonus if recent form aligns with pick direction
    """
    edge = projection - line
    edge_pct = abs(edge) / max(line, 1) * 100

    # Edge component (0-40): 5%+ edge = max score
    edge_score = min(edge_pct / 5 * 40, 40)

    # Hit rate component (0-30): >80% or <20% = max score
    hr_deviation = abs(hit_rate - 0.5)
    hr_score = min(hr_deviation / 0.3 * 30, 30)

    # Consistency component (0-20): std < 4 = very predictable
    std = consistency if consistency > 0 else 8
    cons_score = max(0, min((10 - std) / 6 * 20, 20))

    # Trend component (0-10)
    trend_score = 10 if trend_aligned else 0

    return round(edge_score + hr_score + cons_score + trend_score, 1)


def rank_edges(players_data):
    """Pre-rank all props by quantitative edge score."""
    edges = []

    for player in players_data:
        projections = player.get("projections", {})
        profile = player

        for stat_name, proj in projections.items():
            if proj.get("recommendation") == "SKIP":
                continue

            line = proj["line"]
            projection = proj["projection"]
            edge = proj["edge"]
            direction = "OVER" if edge > 0 else "UNDER"

            # Hit rates (L5, L10, H2H)
            l5_hr, l10_hr, h2h_hr = compute_hit_rates(profile, stat_name, line, player.get("opponent", ""))
            # For UNDER picks, invert
            if direction == "UNDER":
                l5_hr = (1 - l5_hr) if l5_hr is not None else None
                l10_hr = (1 - l10_hr) if l10_hr is not None else None
                h2h_hr = (1 - h2h_hr) if h2h_hr is not None else None
            effective_hr = l10_hr if l10_hr is not None else 0.5

            # Consistency (use stat-specific std)
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


def get_claude_picks(top_edges, game_context):
    """Claude validates and reorders the top quantitative edges."""
    config = Config(read_timeout=120)
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    lessons = ""
    lessons_file = DATA_DIR / "lessons.json"
    if lessons_file.exists():
        with open(lessons_file) as f:
            lessons_data = json.load(f)
            if lessons_data.get("lessons"):
                lessons = "\nLESSONS FROM PAST PICKS:\n" + "\n".join(f"- {l}" for l in lessons_data["lessons"][-10:]) + "\n"

    prompt = f"""You are a sports betting analyst. The model below has pre-computed quantitative edges for today's NBA props.

Your job: review ALL edges below and REMOVE ONLY those with a clear disqualifying reason (confirmed injury, guaranteed blowout, known minutes restriction). Keep everything else. Reorder by your confidence.
{lessons}
GAME CONTEXT:
{json.dumps(game_context, indent=2)}

PRE-RANKED EDGES (by quantitative score):
{json.dumps(top_edges, indent=2)}

Each edge has:
- score: composite (0-100) combining edge size, hit rate, consistency, trend
- edge_pct: how far projection is from line (%)
- hit_rate: % of recent games where player cleared this line
- projected: model's weighted projection

Return ALL picks that pass your filter as a JSON array. Only remove picks with a CLEAR reason — do not trim just to make the list shorter. Add your confidence (1-10):
[
  {{"player": "Name", "prop": "Points", "line": 23.5, "pick": "OVER", "confidence": 9, "projected": 27.2, "score": 72.1}},
  ...
]

Rules:
- Trust the model's math — keep most picks. Only cut if there's a real disqualifier.
- Blowout risk (spread > 10) reduces star minutes — lower confidence but don't remove unless spread > 14
- Combo props (PRA, P+R) amplify edges — keep them
- Return ONLY the JSON array."""

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}]
    })

    try:
        response = bedrock.invoke_model(modelId=MODEL_ID, body=body)
        result = json.loads(response["body"].read())
        text = result["content"][0]["text"]
    except Exception as e:
        errors.add(ErrorType.BEDROCK_FAILED, str(e)[:100])
        return top_edges

    import re
    json_match = re.search(r'\[.*\]', text, re.DOTALL)
    if json_match:
        return json.loads(json_match.group())
    errors.add(ErrorType.BEDROCK_EMPTY, "Claude response had no JSON array")
    return top_edges


def main():
    print(f"=== NBA Props Picks — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # 1. Get game context
    print("Fetching game odds...")
    game_odds = get_game_odds()
    team_defense = {}
    for g in game_odds:
        for team in [g.get("home_team", ""), g.get("away_team", "")]:
            if team and team not in team_defense:
                defense = get_team_defense(team)
                if defense:
                    team_defense[team] = defense

    game_context = {
        "games": game_odds,
        "team_defense": team_defense,
    }

    # 2. Get props
    print("Fetching props...")
    pp_props, _ = fetch_prizepicks_nba()
    if not pp_props:
        print("No props available.")
        return

    # Group by player — use the MEDIAN line
    player_lines_all = defaultdict(lambda: defaultdict(list))
    for p in pp_props:
        player_lines_all[p["player"]][p["stat"]].append(p["line"])

    by_player = defaultdict(dict)
    for player, stats in player_lines_all.items():
        for stat, lines in stats.items():
            sorted_lines = sorted(lines)
            median_line = sorted_lines[len(sorted_lines) // 2]
            by_player[player][stat] = median_line

    # 3. Build profiles and projections
    print("Building player profiles...")
    players_data = []
    for player_name, lines in by_player.items():
        if not lines:
            continue

        team = next((p["team"] for p in pp_props if p["player"] == player_name), "")
        opponent = ""
        is_home = False
        for g in game_odds:
            if team == g.get("home_team", ""):
                opponent = g.get("away_team", "")
                is_home = True
            elif team == g.get("away_team", ""):
                opponent = g.get("home_team", "")
                is_home = False

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

    # 4. Rank edges quantitatively
    print(f"\nScoring edges for {len(players_data)} players...")
    ranked_edges = rank_edges(players_data)
    print(f"  {len(ranked_edges)} props scored, top edge: {ranked_edges[0]['score'] if ranked_edges else 0}")

    # Show top 10 edges before Claude
    print(f"\n{'='*70}")
    print(f"  TOP QUANTITATIVE EDGES (pre-Claude)")
    print(f"{'='*70}\n")
    print(f"{'#':<3} {'Player':<22} {'Prop':<12} {'Line':<6} {'Pick':<6} {'Proj':<6} {'Edge%':<6} {'HR':<5} {'Score'}")
    print("-" * 70)
    for i, e in enumerate(ranked_edges[:15], 1):
        print(f"{i:<3} {e['player']:<22} {e['prop']:<12} {e['line']:<6} {e['pick']:<6} {e['projected']:<6} {e['edge_pct']:<6} {e['hit_rate']:<5} {e['score']}")

    # 5. Filter — minimum 10% edge required
    viable_edges = [e for e in ranked_edges if e["score"] >= 30 and e["edge_pct"] >= 10]

    # Balance check — if >70% lean one direction, raise threshold for dominant side
    if viable_edges:
        under_count = sum(1 for e in viable_edges if e["pick"] == "UNDER")
        over_count = len(viable_edges) - under_count
        total = len(viable_edges)
        if under_count / total > 0.7:
            min_edge_under = 20
            viable_edges = [e for e in viable_edges if e["pick"] == "OVER" or e["edge_pct"] >= min_edge_under]
            print(f"  Balance: {under_count}/{total} were UNDER — raised UNDER threshold to {min_edge_under}% edge")
        elif over_count / total > 0.7:
            min_edge_over = 20
            viable_edges = [e for e in viable_edges if e["pick"] == "UNDER" or e["edge_pct"] >= min_edge_over]
            print(f"  Balance: {over_count}/{total} were OVER — raised OVER threshold to {min_edge_over}% edge")

    viable_edges = viable_edges[:30]
    print(f"\nClaude validating {len(viable_edges)} edges...")
    picks = get_claude_picks(viable_edges, game_context)

    # 6. Display final
    print(f"\n{'='*70}")
    print(f"  FINAL PICKS (model + Claude)")
    print(f"{'='*70}\n")
    print(f"{'#':<3} {'Player':<25} {'Prop':<14} {'Line':<6} {'Pick':<6} {'Proj':<6} {'Conf'}")
    print("-" * 70)
    for i, pick in enumerate(picks, 1):
        conf_bar = "█" * pick.get("confidence", 0) + "░" * (10 - pick.get("confidence", 0))
        print(f"{i:<3} {pick['player']:<25} {pick['prop']:<14} {pick['line']:<6} {pick['pick']:<6} {pick.get('projected', ''):<6} {conf_bar}")

    # Save JSON
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

    # Save combined markdown
    md = f"# NBA — {date_str}\n\n"

    # Game predictions section
    md += "## Game Predictions\n\n"
    game_pred_file = DATA_DIR / f"{date_str}_game_predictions.json"
    game_preds = []
    if game_pred_file.exists():
        with open(game_pred_file) as f:
            game_preds = json.load(f).get("predictions", [])
    if game_preds:
        for p in game_preds:
            market = p.get("market_odds", {})
            spread_market = market.get("spread", "N/A")
            total_market = market.get("over_under", "N/A")

            edges = []
            try:
                total_edge = p["projected_total"] - float(total_market)
                if abs(total_edge) > 3:
                    direction = "OVER" if total_edge > 0 else "UNDER"
                    edges.append(f"{direction} {total_market} ({total_edge:+.1f})")
            except (ValueError, TypeError):
                pass

            try:
                import re as _re
                spread_num = float(_re.search(r'[-\d.]+', str(spread_market)).group())
                spread_diff = abs(p["projected_spread"] - spread_num)
                if spread_diff > 2:
                    edges.append(f"{p['away_team']} +{abs(spread_num)} has value (model: {p['projected_spread']:+.1f})")
            except (ValueError, TypeError, AttributeError):
                pass

            md += f"### {p['away_team']} @ {p['home_team']}\n\n"
            md += f"| | Model | Market |\n|---|---|---|\n"
            md += f"| Winner | {p['home_team']} {p['home_win_prob']}% / {p['away_team']} {p['away_win_prob']}% | {p['home_team']} favored |\n"
            md += f"| Spread | {p['home_team']} {p['projected_spread']:+.1f} | {spread_market} |\n"
            md += f"| Total | {p['projected_total']} | {total_market} |\n"
            md += f"| Projected Score | {p['away_team']} {p['away_projected']} — {p['home_team']} {p['home_projected']} | |\n"
            if edges:
                sep = " | "
                md += f"| **Edge** | {sep.join(edges)} | |\n"
            md += "\n"
    else:
        for g in game_context.get("games", []):
            away = g.get("away_team", "")
            home = g.get("home_team", "")
            spread = g.get("spread", "N/A")
            ou = g.get("over_under", "N/A")
            md += f"### {away} @ {home}\n\n"
            md += f"- Spread: {spread}\n- O/U: {ou}\n\n"

    # Player props picks section — grouped by game
    md += "## Player Props Picks\n\n"
    player_teams = {}
    for p in pp_props:
        if p["player"] and p.get("team"):
            if p["player"] not in player_teams:
                player_teams[p["player"]] = p["team"]

    # Build game matchups from game_context
    # ESPN uses short abbrs (SA, GS, NY) while Underdog uses full (SAS, GSW, NYK)
    abbr_variants = {
        "SA": "SAS", "GS": "GSW", "NY": "NYK", "NO": "NOP",
        "UTAH": "UTA", "PHX": "PHO", "WSH": "WAS",
    }
    team_to_game = {}
    for g in game_context.get("games", []):
        away = g.get("away_team", "")
        home = g.get("home_team", "")
        matchup = f"{away} @ {home}"
        team_to_game[away] = matchup
        team_to_game[home] = matchup
        if away in abbr_variants:
            team_to_game[abbr_variants[away]] = matchup
        if home in abbr_variants:
            team_to_game[abbr_variants[home]] = matchup

    edge_lookup = {(e["player"], e["prop"]): e for e in viable_edges}

    from collections import defaultdict as _dd
    by_game = _dd(list)
    for i, pick in enumerate(picks, 1):
        team = player_teams.get(pick["player"], "Unknown")
        game = team_to_game.get(team, "Unknown")
        if game == "Unknown" and team != "Unknown":
            errors.add(ErrorType.TEAM_UNKNOWN, f"{team} ({pick['player']})")
        by_game[game].append((i, pick, team))

    for game in sorted(by_game.keys()):
        game_picks = by_game[game]
        md += f"### {game}\n\n"
        md += "| # | Player | Team | Prop | Line | Pick | Proj | Edge% | L5 HR | L10 HR | H2H | Conf |\n"
        md += "|---|--------|------|------|------|------|------|-------|-------|--------|-----|------|\n"
        for i, pick, team in game_picks:
            edge_data = edge_lookup.get((pick["player"], pick["prop"]), {})
            edge_pct = edge_data.get("edge_pct", pick.get("edge_pct", ""))
            l5 = edge_data.get("l5_hr", pick.get("l5_hr"))
            l10 = edge_data.get("l10_hr", pick.get("l10_hr"))
            h2h = edge_data.get("h2h_hr", pick.get("h2h_hr"))
            edge_s = f"{edge_pct}%" if edge_pct else "—"
            l5_s = f"{l5}%" if l5 is not None else "—"
            l10_s = f"{l10}%" if l10 is not None else "—"
            h2h_s = f"{h2h}%" if h2h is not None else "—"
            md += f"| {i} | {pick['player']} | {team} | {pick['prop']} | {pick['line']} | {pick['pick']} | {pick.get('projected', '')} | {edge_s} | {l5_s} | {l10_s} | {h2h_s} | {pick.get('confidence', '')}/10 |\n"
        md += "\n"

    # Append errors if any
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
