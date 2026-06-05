"""
Shared picks engine for NBA + WNBA prop predictions.
Handles edge scoring, Claude validation, and report generation.
"""

import boto3
import json
import os
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from botocore.config import Config

from errors import PipelineErrors, ErrorType

MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# ESPN → Underdog abbreviation variants (both directions)
ABBR_VARIANTS = {
    # NBA
    "SA": ["SA", "SAS"], "GS": ["GS", "GSW"], "NY": ["NY", "NYK"],
    "NO": ["NO", "NOP"], "UTAH": ["UTAH", "UTA"], "PHX": ["PHX", "PHO"],
    "WSH": ["WSH", "WAS"], "SAS": ["SAS", "SA"], "GSW": ["GSW", "GS"],
    "NYK": ["NYK", "NY"], "NOP": ["NOP", "NO"], "UTA": ["UTA", "UTAH"],
    "WAS": ["WAS", "WSH"], "PHO": ["PHO", "PHX"],
    # WNBA
    "GSV": ["GSV", "GS"], "LVA": ["LVA", "LV"], "LV": ["LV", "LVA"],
    "CON": ["CON", "CONN"], "CONN": ["CONN", "CON"],
}


def get_stat_val(g, stat_name, keys=None):
    """Extract stat value from a game dict. keys maps stat names to dict keys."""
    if keys is None:
        keys = {"Points": "PTS", "Rebounds": "REB", "Assists": "AST",
                "3-Pointers Made": "FG3", "Pts+Rebs+Asts": "PRA",
                "Pts+Rebs": "PR", "Pts+Asts": "PA", "Rebs+Asts": "RA"}

    k = keys.get(stat_name)
    if k == "PRA":
        return g.get(keys["Points"], g.get("PTS", 0)) + g.get(keys["Rebounds"], g.get("REB", 0)) + g.get(keys["Assists"], g.get("AST", 0))
    elif k == "PR":
        return g.get(keys["Points"], g.get("PTS", 0)) + g.get(keys["Rebounds"], g.get("REB", 0))
    elif k == "PA":
        return g.get(keys["Points"], g.get("PTS", 0)) + g.get(keys["Assists"], g.get("AST", 0))
    elif k == "RA":
        return g.get(keys["Rebounds"], g.get("REB", 0)) + g.get(keys["Assists"], g.get("AST", 0))
    elif k:
        return g.get(k, g.get(k.lower(), 0))
    return 0


def hit_rate_over(games, stat_name, line, keys=None):
    """Fraction of games where player went OVER the line."""
    if not games:
        return None
    hits = sum(1 for g in games if get_stat_val(g, stat_name, keys) > line)
    return hits / len(games)


def compute_hit_rates(all_games, stat_name, line, opponent="", keys=None):
    """Compute L5, L10, and H2H hit rates."""
    l10 = all_games[:10]
    l5 = all_games[:5]

    # Match opponent across abbreviation variants
    opp_variants = ABBR_VARIANTS.get(opponent, [opponent]) if opponent else []
    matchup_key = "MATCHUP" if "MATCHUP" in (all_games[0] if all_games else {}) else "opponent"
    h2h = [g for g in all_games if any(v in g.get(matchup_key, "") for v in opp_variants)] if opp_variants else []

    l10_hr = hit_rate_over(l10, stat_name, line, keys)
    l5_hr = hit_rate_over(l5, stat_name, line, keys)
    h2h_hr = hit_rate_over(h2h, stat_name, line, keys) if h2h else None

    return l5_hr, l10_hr, h2h_hr


def score_edge(projection, line, hit_rate, consistency, trend_aligned):
    """
    Composite edge score (0-100).
    - Edge size (40%): how far projection is from line as % of line
    - Hit rate (30%): how often player actually clears this line
    - Consistency (20%): low std dev = more predictable
    - Trend (10%): bonus if recent form aligns with pick direction
    """
    edge_pct = abs(projection - line) / max(line, 1) * 100
    edge_score = min(edge_pct / 5 * 40, 40)
    hr_deviation = abs(hit_rate - 0.5)
    hr_score = min(hr_deviation / 0.3 * 30, 30)
    std = consistency if consistency > 0 else 8
    cons_score = max(0, min((10 - std) / 6 * 20, 20))
    trend_score = 10 if trend_aligned else 0
    return round(edge_score + hr_score + cons_score + trend_score, 1)


def balance_and_filter(edges, min_edge=10, balance_threshold=0.7, balance_min_edge=15):
    """Filter edges by minimum edge% and apply direction balance check."""
    viable = [e for e in edges if e["score"] >= 30 and e["edge_pct"] >= min_edge]

    if viable:
        under_count = sum(1 for e in viable if e["pick"] == "UNDER")
        over_count = len(viable) - under_count
        total = len(viable)
        if total > 0 and under_count / total > balance_threshold:
            viable = [e for e in viable if e["pick"] == "OVER" or e["edge_pct"] >= balance_min_edge]
        elif total > 0 and over_count / total > balance_threshold:
            viable = [e for e in viable if e["pick"] == "UNDER" or e["edge_pct"] >= balance_min_edge]

    return viable[:30]


def get_claude_picks(top_edges, game_context, sport="NBA", lessons="", errors=None):
    """Claude validates and reorders the top quantitative edges."""
    config = Config(read_timeout=120)
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    prompt = f"""You are a {sport} betting analyst. The model below has pre-computed quantitative edges for today's props.

Your job: return ALL edges below with a confidence score. You must return every single pick — do NOT remove any unless you have confirmed information about an injury or a player being ruled out. Reorder by confidence.
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

IMPORTANT: Return ALL picks in the JSON array. You MUST include every single edge listed above. Do not filter, do not remove, do not trim. Just reorder by confidence and add your confidence score (1-10):
[
  {{"player": "Name", "prop": "Points", "line": 23.5, "pick": "OVER", "confidence": 9, "projected": 27.2, "score": 72.1}},
  ...
]

Rules:
- You MUST return every pick. Do not remove any.
- Lower confidence for blowout risk (spread > 10) but still include them.
- Combo props amplify edges — give them higher confidence.
- Return ONLY the JSON array with ALL picks included."""

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
        if errors:
            errors.add(ErrorType.BEDROCK_FAILED, str(e)[:100])
        return top_edges

    json_match = re.search(r'\[.*?\]', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            # Try finding the last complete object in the array
            try:
                partial = json_match.group()
                last_brace = partial.rfind("}")
                if last_brace > 0:
                    fixed = partial[:last_brace + 1] + "]"
                    return json.loads(fixed)
            except json.JSONDecodeError:
                pass
            if errors:
                errors.add(ErrorType.BEDROCK_EMPTY, "Claude returned malformed JSON")
            return top_edges
    if errors:
        errors.add(ErrorType.BEDROCK_EMPTY, "Claude response had no JSON array")
    return top_edges


def generate_game_predictions_md(game_preds, game_context, errors=None):
    """Generate the game predictions markdown section."""
    md = "## Game Predictions\n\n"

    if not game_preds and errors:
        errors.add(ErrorType.PREDICTIONS_EMPTY, "No game predictions available (ESPN may not have games posted yet)")

    if game_preds:
        for p in game_preds:
            market = p.get("market_odds", {})
            spread_market = market.get("spread", "N/A")
            total_market = market.get("over_under", "N/A")

            md += f"### {p['away_team']} ({p.get('away_record','')}) @ {p['home_team']} ({p.get('home_record','')})\n\n"
            md += f"| | {p['away_team']} | {p['home_team']} |\n|---|---|---|\n"
            md += f"| Conf | {p.get('away_conf', '')} | {p.get('home_conf', '')} |\n"
            md += f"| Regular Season | {p.get('away_reg_record', '')} | {p.get('home_reg_record', '')} |\n"
            if p.get('away_playoff_record') or p.get('home_playoff_record'):
                md += f"| Playoffs | {p.get('away_playoff_record', '')} | {p.get('home_playoff_record', '')} |\n"
            if p.get('away_l5') or p.get('home_l5'):
                md += f"| L5 | {p.get('away_l5', '')} | {p.get('home_l5', '')} |\n"
            md += f"| L10 | {p.get('away_l10', '')} | {p.get('home_l10', '')} |\n"
            md += f"| Streak | {p.get('away_streak', '')} | {p.get('home_streak', '')} |\n"
            md += f"| Home/Away | {p.get('away_away_record', '')} (away) | {p.get('home_home_record', '')} (home) |\n\n"

            away_ml = market.get('away_ml') or '—'
            home_ml = market.get('home_ml') or '—'
            md += f"| | Model | Market |\n|---|---|---|\n"
            md += f"| Winner | {p['home_team']} {p['home_win_prob']}% / {p['away_team']} {p['away_win_prob']}% | ML: {away_ml} / {home_ml} |\n"
            md += f"| Spread | {p['home_team']} {p['projected_spread']:+.1f} | {spread_market} |\n"
            md += f"| Total | {p['projected_total']} | {total_market} |\n"
            md += f"| Projected Score | {p['away_team']} {p['away_projected']} — {p['home_team']} {p['home_projected']} | |\n"

            # Edges
            edges = []
            try:
                total_edge = p["projected_total"] - float(total_market)
                if abs(total_edge) > 3:
                    direction = "OVER" if total_edge > 0 else "UNDER"
                    edges.append(f"{direction} {total_market} ({total_edge:+.1f})")
            except (ValueError, TypeError):
                pass
            try:
                spread_num = float(re.search(r'[-\d.]+', str(spread_market)).group())
                spread_diff = abs(p["projected_spread"] - spread_num)
                if spread_diff > 2:
                    edges.append(f"{p['away_team']} +{abs(spread_num)} has value (model: {p['projected_spread']:+.1f})")
            except (ValueError, TypeError, AttributeError):
                pass
            if edges:
                sep = " | "
                md += f"| **Edge** | {sep.join(edges)} | |\n"
            md += "\n"
    else:
        # Fallback: show games from context
        games = game_context if isinstance(game_context, list) else game_context.get("games", [])
        for g in games:
            if isinstance(g, dict):
                away = g.get("away_team", g.get("away", {}).get("abbr", ""))
                home = g.get("home_team", g.get("home", {}).get("abbr", ""))
                spread = g.get("spread", g.get("odds", {}).get("spread", ""))
                ou = g.get("over_under", g.get("odds", {}).get("over_under", ""))
                status = g.get("status", "")
                if not spread and not ou and status != "Scheduled":
                    md += f"- {away} @ {home} | In Progress\n"
                else:
                    md += f"- {away} @ {home} | {spread or 'TBD'} | O/U: {ou or 'TBD'}\n"
        md += "\n"

    return md


def generate_picks_table_md(picks, viable_edges, team_to_game, player_teams, errors=None):
    """Generate the player props picks markdown section grouped by game."""
    edge_lookup = {(e["player"], e["prop"]): e for e in viable_edges}

    by_game = defaultdict(list)
    for i, pick in enumerate(picks, 1):
        team = player_teams.get(pick["player"], "")
        game = team_to_game.get(team, "Unknown")
        if game == "Unknown" and team and errors:
            errors.add(ErrorType.TEAM_UNKNOWN, f"{team} ({pick['player']})")
        by_game[game].append((i, pick, team))

    md = "## Player Props Picks\n\n"
    for game in sorted(by_game.keys()):
        game_picks = by_game[game]
        # Sort within game: OVERs first, then UNDERs
        overs = [(i, pick, team) for i, pick, team in game_picks if pick.get("pick") == "OVER"]
        unders = [(i, pick, team) for i, pick, team in game_picks if pick.get("pick") == "UNDER"]
        game_picks_sorted = overs + unders

        md += f"### {game}\n\n"
        md += "| # | Player | Team | Prop | Line | Pick | Proj | Edge% | L5 HR | L10 HR | H2H | Conf |\n"
        md += "|---|--------|------|------|------|------|------|-------|-------|--------|-----|------|\n"
        for i, pick, team in game_picks_sorted:
            edge_data = edge_lookup.get((pick["player"], pick["prop"]), {})
            edge_pct = edge_data.get("edge_pct", pick.get("edge_pct", ""))
            l5 = edge_data.get("l5_hr", pick.get("l5_hr"))
            l10 = edge_data.get("l10_hr", pick.get("l10_hr"))
            h2h = edge_data.get("h2h_hr", pick.get("h2h_hr"))
            edge_s = f"{edge_pct}%" if edge_pct else "—"
            l5_s = f"{l5}%" if l5 is not None else "—"
            l10_s = f"{l10}%" if l10 is not None else "—"
            h2h_s = f"{h2h}%" if h2h is not None else "—"
            md += f"| {i} | {pick['player']} | {team} | {pick['prop']} | {pick['line']} | **{pick['pick']}** | {pick.get('projected', '')} | {edge_s} | {l5_s} | {l10_s} | {h2h_s} | {pick.get('confidence', '')}/10 |\n"
        md += "\n"

    return md
