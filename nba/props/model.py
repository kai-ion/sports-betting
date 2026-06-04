#!/usr/bin/env python3
"""
Player Props Model — Enhanced statistical model for NBA player props.

Data points per player:
1. Season averages (regular season + playoffs)
2. Last 3 / Last 5 / Last 10 game performance (trend/hotness)
3. Series-specific performance (vs this opponent)
4. Historical performance vs this opponent (all-time this season)
5. Home/Away splits
6. Minutes trend (opportunity level)
7. Shot attempts trend (usage proxy)
8. Consistency score (standard deviation — lower = more predictable)
9. Team defensive stats (what opponent allows)
10. Game context (spread, total, rest days)
"""

import time
import json
import statistics
import requests
from datetime import datetime
from pathlib import Path

CACHE_DIR = Path(__file__).parent / "cache"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def _find_espn_player_id(name):
    """Search ESPN for an NBA player ID. Tries nickname resolution on failure."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))
    from nicknames import resolve_name

    search_name = resolve_name(name)
    try:
        resp = requests.get(
            f"https://site.api.espn.com/apis/common/v3/search?query={search_name}&type=player&sport=basketball&league=nba&limit=1",
            headers=HEADERS, timeout=10
        )
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if items:
                return items[0].get("id", "")
    except Exception:
        pass
    # If resolved name didn't work and it was different, try original
    if search_name != name:
        try:
            resp = requests.get(
                f"https://site.api.espn.com/apis/common/v3/search?query={name}&type=player&sport=basketball&league=nba&limit=1",
                headers=HEADERS, timeout=10
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                if items:
                    return items[0].get("id", "")
        except Exception:
            pass
    return None


def _get_espn_game_log(espn_id):
    """Get player game log from ESPN (current + past season)."""
    games = []
    for season in ["2026", "2025"]:
        time.sleep(0.3)
        try:
            url = f"https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{espn_id}/gamelog?season={season}"
            resp = requests.get(url, headers=HEADERS, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            labels = data.get("labels", [])

            # Build event ID → opponent mapping from top-level events dict
            events_lookup = data.get("events", {})
            event_opponent = {}
            for eid, einfo in events_lookup.items():
                opp = einfo.get("opponent", {}).get("abbreviation", "")
                event_opponent[eid] = opp

            idx = {}
            for stat in ["PTS", "REB", "AST", "MIN", "FGA", "FG%", "3PT", "3PM"]:
                if stat in labels:
                    idx[stat] = labels.index(stat)

            for st in data.get("seasonTypes", []):
                for cat in st.get("categories", []):
                    for event in cat.get("events", []):
                        stats = event.get("stats", [])
                        if len(stats) <= max(idx.values(), default=0):
                            continue
                        try:
                            eid = event.get("eventId", "")
                            g = {
                                "GAME_DATE": events_lookup.get(eid, {}).get("gameDate", ""),
                                "MATCHUP": event_opponent.get(eid, ""),
                                "PTS": int(stats[idx["PTS"]]) if stats[idx.get("PTS", 0)].isdigit() else 0,
                                "REB": int(stats[idx["REB"]]) if stats[idx.get("REB", 0)].isdigit() else 0,
                                "AST": int(stats[idx["AST"]]) if stats[idx.get("AST", 0)].isdigit() else 0,
                                "FG3": int(stats[idx["3PT"]].split("-")[0]) if "3PT" in idx and stats[idx["3PT"]] else (int(stats[idx["3PM"]]) if "3PM" in idx and stats[idx["3PM"]].isdigit() else 0),
                                "MIN": int(stats[idx["MIN"]]) if stats[idx.get("MIN", 0)].isdigit() else 0,
                                "FGA": int(stats[idx["FGA"]]) if "FGA" in idx and stats[idx["FGA"]].isdigit() else 0,
                            }
                            games.append(g)
                        except (ValueError, IndexError, KeyError):
                            continue
        except Exception:
            continue
    return games


def _get_cached_games(player_name):
    """Load today's cached game logs if available."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_name = player_name.replace(" ", "_").replace("'", "")
    cache_file = CACHE_DIR / f"{date_str}_{safe_name}.json"
    if cache_file.exists():
        with open(cache_file) as f:
            return json.load(f)
    return None


def _save_cache(player_name, all_games):
    """Save game logs to today's cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_name = player_name.replace(" ", "_").replace("'", "")
    cache_file = CACHE_DIR / f"{date_str}_{safe_name}.json"
    with open(cache_file, "w") as f:
        json.dump(all_games, f)


def build_player_profile(player_name, opponent="", is_home=False, season="2025-26"):
    """Build comprehensive statistical profile for a player using ESPN."""
    # Check cache first
    cached = _get_cached_games(player_name)
    if cached is not None:
        all_games = cached
    else:
        espn_id = _find_espn_player_id(player_name)
        if not espn_id:
            return None
        all_games = _get_espn_game_log(espn_id)
        if all_games:
            _save_cache(player_name, all_games)

    if not all_games:
        return None

    # --- COMPUTE ALL STATS ---
    profile = {
        "player": player_name,
        "opponent": opponent,
        "is_home": is_home,
        "total_games": len(all_games),
    }

    # 1. Season averages
    for games, label in [(all_games, "season"), (all_games, "playoffs")]:
        if not games:
            continue
        n = len(games)
        profile[f"{label}_avg"] = {
            "pts": round(sum(g["PTS"] for g in games) / n, 1),
            "reb": round(sum(g["REB"] for g in games) / n, 1),
            "ast": round(sum(g["AST"] for g in games) / n, 1),
            "fg3": round(sum(g.get("FG3", g.get("3PM", 0)) for g in games) / n, 1),
            "min": round(sum(g["MIN"] for g in games) / n, 1),
            "fga": round(sum(g.get("FGA", 0) for g in games) / n, 1),
            "pra": round(sum(g["PTS"] + g["REB"] + g["AST"] for g in games) / n, 1),
            "games": n,
        }

    # 2. Recent form (L3, L5, L10)
    for window, label in [(3, "l3"), (5, "l5"), (10, "l10")]:
        recent = all_games[:window]
        if len(recent) < window:
            recent = all_games[:len(all_games)]
        n = len(recent)
        if n == 0:
            continue
        profile[f"{label}_avg"] = {
            "pts": round(sum(g["PTS"] for g in recent) / n, 1),
            "reb": round(sum(g["REB"] for g in recent) / n, 1),
            "ast": round(sum(g["AST"] for g in recent) / n, 1),
            "fg3": round(sum(g.get("FG3", g.get("3PM", 0)) for g in recent) / n, 1),
            "min": round(sum(g["MIN"] for g in recent) / n, 1),
            "fga": round(sum(g.get("FGA", 0) for g in recent) / n, 1),
            "pra": round(sum(g["PTS"] + g["REB"] + g["AST"] for g in recent) / n, 1),
        }

    # 3. Trend analysis (hot/cold)
    l3 = all_games[:3]
    l10 = all_games[:min(10, len(all_games))]
    l3_pts = sum(g["PTS"] for g in l3) / len(l3) if l3 else 0
    l10_pts = sum(g["PTS"] for g in l10) / len(l10) if l10 else 0
    l3_min = sum(g["MIN"] for g in l3) / len(l3) if l3 else 0
    l10_min = sum(g["MIN"] for g in l10) / len(l10) if l10 else 0

    if l10_pts > 0:
        pts_trend_pct = (l3_pts - l10_pts) / l10_pts * 100
    else:
        pts_trend_pct = 0

    profile["trend"] = {
        "pts_direction": "HOT" if pts_trend_pct > 10 else "COLD" if pts_trend_pct < -10 else "NEUTRAL",
        "pts_trend_pct": round(pts_trend_pct, 1),
        "min_direction": "UP" if l3_min > l10_min + 2 else "DOWN" if l3_min < l10_min - 2 else "STABLE",
        "l3_min": round(l3_min, 1),
        "l10_min": round(l10_min, 1),
    }

    # 4. Consistency (standard deviation)
    if len(all_games) >= 5:
        pts_values = [g["PTS"] for g in all_games[:10]]
        reb_values = [g["REB"] for g in all_games[:10]]
        ast_values = [g["AST"] for g in all_games[:10]]
        profile["consistency"] = {
            "pts_std": round(statistics.stdev(pts_values), 1),
            "reb_std": round(statistics.stdev(reb_values), 1),
            "ast_std": round(statistics.stdev(ast_values), 1),
            "pts_range": f"{min(pts_values)}-{max(pts_values)}",
            "predictability": "HIGH" if statistics.stdev(pts_values) < 5 else "MEDIUM" if statistics.stdev(pts_values) < 8 else "LOW",
        }

    # 5. vs Opponent (historical)
    if opponent:
        vs_opp = [g for g in all_games if opponent in g.get("MATCHUP", "")]
        if vs_opp:
            n = len(vs_opp)
            profile["vs_opponent"] = {
                "games": n,
                "pts": round(sum(g["PTS"] for g in vs_opp) / n, 1),
                "reb": round(sum(g["REB"] for g in vs_opp) / n, 1),
                "ast": round(sum(g["AST"] for g in vs_opp) / n, 1),
                "min": round(sum(g["MIN"] for g in vs_opp) / n, 1),
                "pra": round(sum(g["PTS"] + g["REB"] + g["AST"] for g in vs_opp) / n, 1),
                "last_3": [
                    {"date": g["GAME_DATE"], "pts": g["PTS"], "reb": g["REB"], "ast": g["AST"], "min": g["MIN"]}
                    for g in vs_opp[:3]
                ],
            }

    # 6. Home/Away splits (not available from ESPN game log — skip)
    if False:
        profile["home_away"] = {
            "home_pts": 0,
            "away_pts": 0,
            "home_reb": 0,
            "away_reb": round(sum(g["REB"] for g in away_games) / len(away_games), 1),
            "home_ast": round(sum(g["AST"] for g in home_games) / len(home_games), 1),
            "away_ast": round(sum(g["AST"] for g in away_games) / len(away_games), 1),
            "home_games": len(home_games),
            "away_games": len(away_games),
        }

    # 7. Usage/FGA trend
    l5_fga = sum(g.get("FGA", 0) for g in all_games[:5]) / min(5, len(all_games))
    season_fga = sum(g.get("FGA", 0) for g in all_games) / len(all_games)
    profile["usage"] = {
        "l5_fga": round(l5_fga, 1),
        "season_fga": round(season_fga, 1),
        "fga_trend": "UP" if l5_fga > season_fga * 1.1 else "DOWN" if l5_fga < season_fga * 0.9 else "STABLE",
    }

    profile["recent_games"] = all_games

    return profile


def project_stats(profile, lines, team_defense=None):
    """Project player stats and compare to lines."""
    projections = {}

    for stat_name, line in lines.items():
        # Weighted projection: 40% L5, 30% vs opponent, 20% playoffs avg, 10% season
        weights = []

        # L5 average
        l5 = profile.get("l5_avg", {})
        if stat_name == "Points":
            l5_val = l5.get("pts", 0)
        elif stat_name == "Rebounds":
            l5_val = l5.get("reb", 0)
        elif stat_name == "Assists":
            l5_val = l5.get("ast", 0)
        elif stat_name == "3-Pointers Made":
            l5_val = l5.get("fg3", 0)
        elif stat_name == "Pts+Rebs+Asts":
            l5_val = l5.get("pra", 0)
        elif stat_name == "Pts+Rebs":
            l5_val = l5.get("pts", 0) + l5.get("reb", 0)
        elif stat_name == "Pts+Asts":
            l5_val = l5.get("pts", 0) + l5.get("ast", 0)
        elif stat_name == "Rebs+Asts":
            l5_val = l5.get("reb", 0) + l5.get("ast", 0)
        else:
            continue

        weights.append((l5_val, 0.35))

        # vs Opponent
        vs_opp = profile.get("vs_opponent", {})
        if vs_opp:
            if stat_name == "Points":
                opp_val = vs_opp.get("pts", l5_val)
            elif stat_name == "Rebounds":
                opp_val = vs_opp.get("reb", l5_val)
            elif stat_name == "Assists":
                opp_val = vs_opp.get("ast", l5_val)
            elif stat_name == "3-Pointers Made":
                opp_val = vs_opp.get("fg3", l5_val)
            elif stat_name == "Pts+Rebs+Asts":
                opp_val = vs_opp.get("pra", l5_val)
            elif stat_name == "Pts+Rebs":
                opp_val = vs_opp.get("pts", 0) + vs_opp.get("reb", 0)
            elif stat_name == "Pts+Asts":
                opp_val = vs_opp.get("pts", 0) + vs_opp.get("ast", 0)
            elif stat_name == "Rebs+Asts":
                opp_val = vs_opp.get("reb", 0) + vs_opp.get("ast", 0)
            else:
                opp_val = l5_val
            weights.append((opp_val, 0.30))
        else:
            weights.append((l5_val, 0.30))

        # Playoffs average
        po = profile.get("playoffs_avg", profile.get("season_avg", {}))
        if stat_name == "Points":
            po_val = po.get("pts", l5_val)
        elif stat_name == "Rebounds":
            po_val = po.get("reb", l5_val)
        elif stat_name == "Assists":
            po_val = po.get("ast", l5_val)
        elif stat_name == "3-Pointers Made":
            po_val = po.get("fg3", l5_val)
        elif stat_name == "Pts+Rebs+Asts":
            po_val = po.get("pra", l5_val)
        elif stat_name == "Pts+Rebs":
            po_val = po.get("pts", 0) + po.get("reb", 0)
        elif stat_name == "Pts+Asts":
            po_val = po.get("pts", 0) + po.get("ast", 0)
        elif stat_name == "Rebs+Asts":
            po_val = po.get("reb", 0) + po.get("ast", 0)
        else:
            po_val = l5_val
        weights.append((po_val, 0.25))

        # Home/away adjustment
        ha = profile.get("home_away", {})
        is_home = profile.get("is_home", False)
        ha_adj = 0
        if ha and stat_name == "Points":
            home_pts = ha.get("home_pts", 0)
            away_pts = ha.get("away_pts", 0)
            if is_home and home_pts > away_pts:
                ha_adj = (home_pts - away_pts) * 0.5
            elif not is_home and away_pts < home_pts:
                ha_adj = (away_pts - home_pts) * 0.5

        # Weighted projection
        projection = sum(val * weight for val, weight in weights) + ha_adj

        # Trend adjustment (if hot, bump up slightly)
        trend = profile.get("trend", {})
        if trend.get("pts_direction") == "HOT" and stat_name in ["Points", "Pts+Rebs+Asts", "Pts+Asts", "Pts+Rebs"]:
            projection *= 1.05
        elif trend.get("pts_direction") == "COLD" and stat_name in ["Points", "Pts+Rebs+Asts", "Pts+Asts", "Pts+Rebs"]:
            projection *= 0.95

        edge = projection - line
        over_probability = 50 + (edge / (profile.get("consistency", {}).get("pts_std", 8) or 8)) * 15

        projections[stat_name] = {
            "line": line,
            "projection": round(projection, 1),
            "edge": round(edge, 1),
            "over_probability": round(min(max(over_probability, 5), 95), 1),
            "recommendation": "OVER" if edge > 3 else "UNDER" if edge < -3 else "SKIP",
            "confidence": "HIGH" if abs(edge) > 5 else "MEDIUM" if abs(edge) > 3 else "LOW",
        }

    return projections


if __name__ == "__main__":
    # Quick test
    print("Building SGA profile...")
    profile = build_player_profile("Shai Gilgeous-Alexander", opponent="SAS", is_home=False)
    if profile:
        import json as _json
        print(json.dumps(profile, indent=2, default=str))

        lines = {"Points": 39.5, "Rebounds": 7.5, "Assists": 11.5, "Pts+Rebs+Asts": 54.5}
        projections = project_stats(profile, lines)
        print("\n=== Projections vs Lines ===")
        for stat, proj in projections.items():
            print(f"  {stat}: Line={proj['line']} | Proj={proj['projection']} | Edge={proj['edge']:+.1f} | {proj['recommendation']} ({proj['confidence']})")
