#!/usr/bin/env python3
"""
WNBA Props Picks — quantitative edge scoring + Claude validation.
Computes projections from ESPN game logs, scores edges, Claude validates.
"""

import boto3
import requests
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

DATA_DIR = Path(__file__).parent.parent / "data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = "us.anthropic.claude-sonnet-4-6"

errors = PipelineErrors()


def find_espn_player_id(name):
    """Search ESPN for a WNBA player ID."""
    try:
        resp = requests.get(
            f"https://site.api.espn.com/apis/common/v3/search?query={name}&type=player&sport=basketball&league=wnba&limit=1",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if items:
                return items[0].get("id", "")
        elif resp.status_code >= 400:
            errors.add(ErrorType.ESPN_TIMEOUT, f"{name}: HTTP {resp.status_code}")
    except Exception as e:
        errors.add(ErrorType.ESPN_TIMEOUT, f"{name}: {e}")
    errors.add(ErrorType.PLAYER_NOT_FOUND, name)
    return None


def get_player_game_log(espn_id, season=None):
    """Get player game log from ESPN (current + past season)."""
    games = []
    seasons_to_check = [season] if season else ["2026", "2025"]

    for s in seasons_to_check:
        time.sleep(0.3)
        try:
            url = f"https://site.api.espn.com/apis/common/v3/sports/basketball/wnba/athletes/{espn_id}/gamelog"
            if s:
                url += f"?season={s}"
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                labels = data.get("labels", [])
                pts_idx = labels.index("PTS") if "PTS" in labels else 1
                reb_idx = labels.index("REB") if "REB" in labels else 2
                ast_idx = labels.index("AST") if "AST" in labels else 3
                min_idx = labels.index("MIN") if "MIN" in labels else 0
                fg3_idx = labels.index("3PM") if "3PM" in labels else None

                for st in data.get("seasonTypes", []):
                    for cat in st.get("categories", []):
                        for event in cat.get("events", []):
                            stats = event.get("stats", [])
                            if len(stats) > max(pts_idx, reb_idx, ast_idx):
                                try:
                                    games.append({
                                        "season": s,
                                        "date": event.get("gameDate", ""),
                                        "opponent": event.get("opponent", {}).get("abbreviation", ""),
                                        "min": int(stats[min_idx]) if stats[min_idx].isdigit() else 0,
                                        "pts": int(stats[pts_idx]) if stats[pts_idx].isdigit() else 0,
                                        "reb": int(stats[reb_idx]) if stats[reb_idx].isdigit() else 0,
                                        "ast": int(stats[ast_idx]) if stats[ast_idx].isdigit() else 0,
                                        "fg3": int(stats[fg3_idx]) if fg3_idx and fg3_idx < len(stats) and stats[fg3_idx].isdigit() else 0,
                                    })
                                except (ValueError, IndexError):
                                    continue
        except Exception:
            continue

    return games


def compute_player_edge(name, lines):
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

    # Season averages
    avg_pts = sum(g["pts"] for g in games) / n
    avg_reb = sum(g["reb"] for g in games) / n
    avg_ast = sum(g["ast"] for g in games) / n

    # L5 averages (recency weight)
    l5_pts = sum(g["pts"] for g in l5) / len(l5)
    l5_reb = sum(g["reb"] for g in l5) / len(l5)
    l5_ast = sum(g["ast"] for g in l5) / len(l5)
    l5_fg3 = sum(g.get("fg3", 0) for g in l5) / len(l5)

    # L10 averages
    l10_pts = sum(g["pts"] for g in l10) / len(l10)
    l10_reb = sum(g["reb"] for g in l10) / len(l10)
    l10_ast = sum(g["ast"] for g in l10) / len(l10)
    l10_fg3 = sum(g.get("fg3", 0) for g in l10) / len(l10)

    # Season average for 3PM
    avg_fg3 = sum(g.get("fg3", 0) for g in games) / n

    # Standard deviations (from L10 for relevance)
    import statistics
    pts_std = statistics.stdev([g["pts"] for g in l10]) if len(l10) >= 3 else 5
    reb_std = statistics.stdev([g["reb"] for g in l10]) if len(l10) >= 3 else 3
    ast_std = statistics.stdev([g["ast"] for g in l10]) if len(l10) >= 3 else 2
    fg3_std = statistics.stdev([g.get("fg3", 0) for g in l10]) if len(l10) >= 3 else 1.5

    # Trend
    pts_trend = "HOT" if l5_pts > avg_pts * 1.1 else "COLD" if l5_pts < avg_pts * 0.9 else "NEUTRAL"

    # Projection: 45% L5, 35% L10, 20% season
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

        # Hit rates across windows
        def _val(g, sn):
            if sn == "Points": return g["pts"]
            elif sn == "Rebounds": return g["reb"]
            elif sn == "Assists": return g["ast"]
            elif sn == "3-Pointers Made": return g.get("fg3", 0)
            elif sn == "Pts+Rebs+Asts": return g["pts"] + g["reb"] + g["ast"]
            elif sn == "Pts+Rebs": return g["pts"] + g["reb"]
            elif sn == "Pts+Asts": return g["pts"] + g["ast"]
            elif sn == "Rebs+Asts": return g["reb"] + g["ast"]
            return 0

        def _hr(game_list, sn, ln):
            if not game_list: return None
            return sum(1 for g in game_list if _val(g, sn) > ln) / len(game_list)

        l10_hr = _hr(l10, stat_name, line)
        l5_hr = _hr(l5, stat_name, line)
        # H2H not available from ESPN game log (no opponent filter in WNBA data)
        h2h_hr = None

        if direction == "UNDER":
            l10_hr = (1 - l10_hr) if l10_hr is not None else None
            l5_hr = (1 - l5_hr) if l5_hr is not None else None

        effective_hr = l10_hr if l10_hr is not None else 0.5

        # Edge score
        edge_pct = abs(edge) / max(line, 1) * 100
        edge_score = min(edge_pct / 5 * 40, 40)
        hr_score = min(abs(effective_hr - 0.5) / 0.3 * 30, 30)
        cons_score = max(0, min((10 - std) / 6 * 20, 20))
        trend_aligned = (
            (direction == "OVER" and pts_trend == "HOT") or
            (direction == "UNDER" and pts_trend == "COLD")
        )
        trend_score = 10 if trend_aligned else 0
        score = round(edge_score + hr_score + cons_score + trend_score, 1)

        # Skip weak edges — require minimum 10% edge
        if score < 20 or edge_pct < 10:
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


def get_claude_picks(top_edges, games):
    """Claude validates and reorders the top quantitative edges."""
    config = Config(read_timeout=120)
    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    lessons = ""
    lessons_file = DATA_DIR.parent.parent / "common" / "lessons_wnba.json"
    if lessons_file.exists():
        with open(lessons_file) as f:
            lessons_data = json.load(f)
            if lessons_data.get("lessons"):
                lessons = "\nLESSONS FROM PAST PICKS:\n" + "\n".join(f"- {l}" for l in lessons_data["lessons"][-10:]) + "\n"

    prompt = f"""You are a WNBA betting analyst. The model below has pre-computed quantitative edges for tonight's props.

Your job: review ALL edges below and REMOVE ONLY those with a clear disqualifying reason (confirmed injury, guaranteed blowout, known minutes restriction). Keep everything else. Reorder by your confidence.
{lessons}
GAMES TONIGHT:
{json.dumps(games, indent=2)}

PRE-RANKED EDGES (by quantitative score):
{json.dumps(top_edges, indent=2)}

Each edge has:
- score: composite (0-100) combining edge size, hit rate, consistency, trend
- edge_pct: how far projection is from line (%)
- hit_rate: % of recent games supporting this direction
- projected: model's weighted projection (45% L5, 35% L10, 20% season)

Return ALL picks that pass your filter as a JSON array. Only remove picks with a CLEAR reason — do not trim just to make the list shorter. Add your confidence (1-10):
[
  {{"player": "Name", "prop": "Points", "line": 20.5, "pick": "OVER", "confidence": 8, "projected": 24.0, "score": 65.3}},
  ...
]

Rules:
- Trust the model's math — keep most picks. Only cut if there's a real disqualifier.
- Combo props amplify edges — keep them
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
    print(f"=== WNBA Props Picks — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # Load props
    date_str = datetime.now().strftime("%Y-%m-%d")
    props_file = DATA_DIR / f"{date_str}_props.json"
    if not props_file.exists():
        from fetch import fetch_prizepicks_wnba, get_games, organize_by_player
        props, _ = fetch_prizepicks_wnba()
        games = get_games()
        if not props:
            print("No WNBA props. Run fetch.py first or no games today.")
            return
        players_lines = organize_by_player(props)
    else:
        with open(props_file) as f:
            data = json.load(f)
        players_lines = data.get("players", {})
        games = data.get("games", [])
        props = []

    # Build player→team lookup from props
    player_team_map = {}
    if props:
        for p in props:
            if p.get("player") and p.get("team"):
                if p["player"] not in player_team_map:
                    player_team_map[p["player"]] = p["team"]
    else:
        # From saved props file
        props_raw = data.get("props", []) if 'data' in dir() else []
        for p in props_raw:
            if p.get("player") and p.get("team"):
                if p["player"] not in player_team_map:
                    player_team_map[p["player"]] = p["team"]

    # Compute edges for each player
    player_names = list(players_lines.keys())
    print(f"Computing edges for {len(player_names)} players from ESPN game logs...")

    all_edges = []
    for name in player_names[:30]:
        time.sleep(0.3)
        player_edges = compute_player_edge(name, players_lines[name])
        if player_edges:
            team = player_team_map.get(name, "")
            for e in player_edges:
                e["team"] = team
            all_edges.extend(player_edges)
            print(f"  {name}: {len(player_edges)} edges")

    all_edges.sort(key=lambda x: x["score"], reverse=True)
    print(f"\n  Total: {len(all_edges)} edges scored")

    if not all_edges:
        print("No edges found.")
        return

    # Show top edges
    print(f"\n{'='*70}")
    print(f"  TOP QUANTITATIVE EDGES (pre-Claude)")
    print(f"{'='*70}\n")
    print(f"{'#':<3} {'Player':<22} {'Prop':<10} {'Line':<6} {'Pick':<6} {'Proj':<6} {'Edge%':<6} {'HR':<5} {'Score'}")
    print("-" * 70)
    for i, e in enumerate(all_edges[:15], 1):
        print(f"{i:<3} {e['player']:<22} {e['prop']:<10} {e['line']:<6} {e['pick']:<6} {e['projected']:<6} {e['edge_pct']:<6} {e['hit_rate']:<5} {e['score']}")

    # Balance check — if >70% lean one direction, raise threshold for the dominant side
    viable_edges = [e for e in all_edges if e["score"] >= 30]
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
    picks = get_claude_picks(viable_edges, games)

    # Display
    print(f"\n{'='*70}")
    print(f"  WNBA — FINAL PICKS (model + Claude)")
    print(f"{'='*70}\n")
    print(f"{'#':<3} {'Player':<25} {'Prop':<8} {'Line':<6} {'Pick':<6} {'Proj':<6} {'Conf'}")
    print("-" * 70)
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

    # Save combined markdown
    md = f"# WNBA — {date_str}\n\n"

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
            md += f"### {p['away_team']} ({p.get('away_record','')}) @ {p['home_team']} ({p.get('home_record','')})\n\n"
            md += f"| | Model | Market |\n|---|---|---|\n"
            away_ml = market.get('away_ml') or '—'
            home_ml = market.get('home_ml') or '—'
            md += f"| Winner | {p['home_team']} {p['home_win_prob']}% / {p['away_team']} {p['away_win_prob']}% | ML: {away_ml} / {home_ml} |\n"
            md += f"| Spread | {p['home_team']} {p['projected_spread']:+.1f} | {market.get('spread', 'N/A')} |\n"
            md += f"| Total | {p['projected_total']} | {market.get('over_under', 'N/A')} |\n"
            md += f"| Score | {p['away_team']} {p['away_projected']} — {p['home_team']} {p['home_projected']} | |\n\n"
    else:
        for g in games:
            spread = g.get("odds", {}).get("spread", "N/A")
            ou = g.get("odds", {}).get("over_under", "N/A")
            md += f"- {g['away']['abbr']} @ {g['home']['abbr']} | {spread} | O/U: {ou}\n"
        md += "\n"

    # Player props picks section — grouped by game
    edge_lookup = {(e["player"], e["prop"]): e for e in viable_edges}

    # Build player→team and team→game mappings
    player_teams = {}
    for e in viable_edges:
        if e.get("player"):
            player_teams[e["player"]] = e.get("team", "")

    # Build team→game from ESPN games + Underdog game titles
    team_to_game = {}
    for g in games:
        away = g.get("away", {}).get("abbr", "")
        home = g.get("home", {}).get("abbr", "")
        if away and home:
            matchup = f"{away} @ {home}"
            team_to_game[away] = matchup
            team_to_game[home] = matchup

    # Try to get Underdog games for better coverage
    try:
        import requests as _req
        ud_resp = _req.get("https://api.underdogfantasy.com/beta/v5/over_under_lines",
                           headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if ud_resp.status_code == 200:
            ud_data = ud_resp.json()
            for g in ud_data.get("games", ud_data.get("matches", [])):
                title = g.get("abbreviated_title", "")
                if " @ " in title:
                    parts = title.split(" @ ")
                    team_to_game[parts[0].strip()] = title
                    team_to_game[parts[1].strip()] = title
    except Exception:
        pass

    from collections import defaultdict as _dd2
    by_game = _dd2(list)
    for i, pick in enumerate(picks, 1):
        team = player_teams.get(pick["player"], "")
        game = team_to_game.get(team, "Unknown")
        if game == "Unknown" and team:
            errors.add(ErrorType.TEAM_UNKNOWN, f"{team} ({pick['player']})")
        by_game[game].append((i, pick, team))

    md += "## Player Props Picks\n\n"
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
