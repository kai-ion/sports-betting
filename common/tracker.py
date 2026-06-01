#!/usr/bin/env python3
"""
Props Tracker — tracks Claude's picks, grades results, and feeds lessons back.

Flow:
1. After games finish, fetch actual player stats
2. Compare vs Claude's picks (did the over/under hit?)
3. Log results: W/L, edge accuracy, confidence calibration
4. Weekly reflection: what patterns work, what doesn't, adjust model

Similar to the stock pipeline's paper trading + TradingAgents reflection loop.
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict
from nba_api.stats.endpoints import boxscoretraditionalv2, scoreboardv2
from nba_api.stats.static import teams as nba_teams

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = DATA_DIR / "results"
LESSONS_FILE = DATA_DIR / "lessons.json"
MAX_LESSONS = 20  # Only keep most recent/relevant lessons
MAX_DAILY_FILES = 30  # Auto-clean props/analysis older than 30 days


def load_picks(date_str=None):
    """Load Claude's picks for a date."""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    picks_file = DATA_DIR / f"{date_str}_picks.json"
    if picks_file.exists():
        with open(picks_file) as f:
            return json.load(f)
    return None


def get_game_results(date_str):
    """Get actual player stats from completed games."""
    from nba_api.stats.endpoints import leaguegamefinder, boxscoretraditionalv2

    # Find games on that date
    time.sleep(1)
    try:
        games = leaguegamefinder.LeagueGameFinder(
            season_nullable='2025-26',
            league_id_nullable='00',
            date_from_nullable=date_str,
            date_to_nullable=date_str,
        ).get_normalized_dict()

        game_ids = list(set(g["GAME_ID"] for g in games.get("LeagueGameFinderResults", [])))
    except Exception as e:
        print(f"  Error finding games: {e}")
        return {}

    if not game_ids:
        print(f"  No completed games found for {date_str}")
        return {}

    # Get box scores
    player_stats = {}
    for game_id in game_ids:
        time.sleep(0.6)
        try:
            box = boxscoretraditionalv2.BoxScoreTraditionalV2(game_id=game_id)
            data = box.get_normalized_dict()
            players = data.get("PlayerStats", [])
            for p in players:
                name = p.get("PLAYER_NAME", "")
                player_stats[name] = {
                    "pts": p.get("PTS", 0),
                    "reb": p.get("REB", 0),
                    "ast": p.get("AST", 0),
                    "min": p.get("MIN", "0:00"),
                    "pra": p.get("PTS", 0) + p.get("REB", 0) + p.get("AST", 0),
                    "p_r": p.get("PTS", 0) + p.get("REB", 0),
                    "p_a": p.get("PTS", 0) + p.get("AST", 0),
                    "r_a": p.get("REB", 0) + p.get("AST", 0),
                    "game_id": game_id,
                }
        except Exception as e:
            print(f"  Box score error for {game_id}: {e}")

    return player_stats


def grade_picks(picks, actual_stats):
    """Grade each pick: WIN or LOSS."""
    results = []
    stat_map = {
        "Points": "pts",
        "Rebounds": "reb",
        "Assists": "ast",
        "PRA": "pra",
        "Pts+Rebs+Asts": "pra",
        "P+R": "p_r",
        "Pts+Rebs": "p_r",
        "P+A": "p_a",
        "Pts+Asts": "p_a",
        "R+A": "r_a",
        "Rebs+Asts": "r_a",
    }

    for pick in picks:
        player = pick.get("player", "")
        prop = pick.get("prop", "")
        line = pick.get("line", 0)
        direction = pick.get("pick", "").upper()
        confidence = pick.get("confidence", 0)
        projected = pick.get("projected", 0)

        # Find actual stat
        stats = actual_stats.get(player, {})
        if not stats:
            # Try partial name match
            for name, s in actual_stats.items():
                if player.lower() in name.lower() or name.lower() in player.lower():
                    stats = s
                    break

        if not stats:
            results.append({**pick, "result": "NO_DATA", "actual": None})
            continue

        stat_key = stat_map.get(prop, "")
        if not stat_key or stat_key not in stats:
            results.append({**pick, "result": "NO_DATA", "actual": None})
            continue

        actual = stats[stat_key]
        hit = (direction == "OVER" and actual > line) or (direction == "UNDER" and actual < line)
        push = actual == line

        results.append({
            **pick,
            "actual": actual,
            "result": "WIN" if hit else "PUSH" if push else "LOSS",
            "margin": round(actual - line, 1),
        })

    return results


def calculate_stats(all_results):
    """Calculate overall performance stats."""
    graded = [r for r in all_results if r["result"] in ("WIN", "LOSS")]
    if not graded:
        return {}

    wins = sum(1 for r in graded if r["result"] == "WIN")
    losses = sum(1 for r in graded if r["result"] == "LOSS")
    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0

    # By confidence level
    by_conf = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in graded:
        conf = r.get("confidence", 5)
        if r["result"] == "WIN":
            by_conf[conf]["wins"] += 1
        else:
            by_conf[conf]["losses"] += 1

    # By prop type
    by_prop = defaultdict(lambda: {"wins": 0, "losses": 0})
    for r in graded:
        prop = r.get("prop", "unknown")
        if r["result"] == "WIN":
            by_prop[prop]["wins"] += 1
        else:
            by_prop[prop]["losses"] += 1

    # By direction
    overs = [r for r in graded if r.get("pick", "").upper() == "OVER"]
    unders = [r for r in graded if r.get("pick", "").upper() == "UNDER"]
    over_wr = sum(1 for r in overs if r["result"] == "WIN") / len(overs) * 100 if overs else 0
    under_wr = sum(1 for r in unders if r["result"] == "WIN") / len(unders) * 100 if unders else 0

    return {
        "total_picks": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 1),
        "over_win_rate": round(over_wr, 1),
        "under_win_rate": round(under_wr, 1),
        "by_confidence": dict(by_conf),
        "by_prop": dict(by_prop),
    }


def load_lessons():
    """Load accumulated lessons."""
    if LESSONS_FILE.exists():
        with open(LESSONS_FILE) as f:
            return json.load(f)
    return {"lessons": [], "last_updated": None}


def save_lessons(lessons_data):
    """Save lessons."""
    lessons_data["last_updated"] = datetime.now().isoformat()
    with open(LESSONS_FILE, "w") as f:
        json.dump(lessons_data, f, indent=2)


def weekly_reflection(all_results):
    """Analyze patterns and generate lessons for the model."""
    lessons_data = load_lessons()
    stats = calculate_stats(all_results)

    new_lessons = []

    # Lesson 1: Which direction is hitting more?
    if stats.get("over_win_rate", 50) > 60:
        new_lessons.append(f"OVERS are hitting at {stats['over_win_rate']}% — lean over when edge is close")
    elif stats.get("under_win_rate", 50) > 60:
        new_lessons.append(f"UNDERS are hitting at {stats['under_win_rate']}% — lean under when edge is close")

    # Lesson 2: Which confidence levels are calibrated?
    for conf, record in stats.get("by_confidence", {}).items():
        total = record["wins"] + record["losses"]
        if total >= 3:
            wr = record["wins"] / total * 100
            if wr < 50 and conf >= 8:
                new_lessons.append(f"Confidence {conf} picks are only hitting {wr:.0f}% — model is overconfident at this level")
            elif wr > 70 and conf <= 7:
                new_lessons.append(f"Confidence {conf} picks hitting {wr:.0f}% — model is underconfident, can bet bigger")

    # Lesson 3: Which prop types work best?
    for prop, record in stats.get("by_prop", {}).items():
        total = record["wins"] + record["losses"]
        if total >= 3:
            wr = record["wins"] / total * 100
            if wr > 65:
                new_lessons.append(f"{prop} props hitting {wr:.0f}% — prioritize this market")
            elif wr < 40:
                new_lessons.append(f"{prop} props only hitting {wr:.0f}% — reduce exposure or skip")

    # Save lessons — keep only the most impactful ones
    lessons_data["lessons"].extend(new_lessons)
    # Deduplicate similar lessons and keep only MAX_LESSONS
    seen = set()
    unique = []
    for l in reversed(lessons_data["lessons"]):
        key = l[:30]  # rough dedup by prefix
        if key not in seen:
            seen.add(key)
            unique.append(l)
    lessons_data["lessons"] = list(reversed(unique))[-MAX_LESSONS:]
    save_lessons(lessons_data)

    return new_lessons, stats


def grade_yesterday():
    """Grade yesterday's picks with actual results."""
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    print(f"=== Grading picks for {yesterday} ===\n")

    picks_data = load_picks(yesterday)
    if not picks_data:
        print(f"No picks found for {yesterday}")
        return

    picks = picks_data.get("picks", [])
    if not picks:
        print("No picks to grade")
        return

    # Get actual stats
    print("Fetching game results...")
    actual_stats = get_game_results(yesterday)
    if not actual_stats:
        print("No game results available yet")
        return

    print(f"Got stats for {len(actual_stats)} players\n")

    # Grade
    results = grade_picks(picks, actual_stats)

    # Display
    print(f"{'#':<3} {'Player':<25} {'Prop':<8} {'Line':<6} {'Pick':<6} {'Actual':<7} {'Result'}")
    print("-" * 70)
    for i, r in enumerate(results, 1):
        actual = r.get("actual", "—")
        result_icon = "✅" if r["result"] == "WIN" else "❌" if r["result"] == "LOSS" else "⬜"
        print(f"{i:<3} {r['player']:<25} {r['prop']:<8} {r['line']:<6} {r['pick']:<6} {actual:<7} {result_icon} {r['result']}")

    # Stats
    wins = sum(1 for r in results if r["result"] == "WIN")
    losses = sum(1 for r in results if r["result"] == "LOSS")
    total = wins + losses
    print(f"\n  Record: {wins}W - {losses}L ({wins/total*100:.0f}%)" if total else "\n  No graded picks")

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_file = RESULTS_DIR / f"{yesterday}.json"
    with open(result_file, "w") as f:
        json.dump({
            "date": yesterday,
            "results": results,
            "record": {"wins": wins, "losses": losses, "total": total},
        }, f, indent=2)
    print(f"  Saved to {result_file}")

    return results


def show_all_time_stats():
    """Show cumulative performance."""
    all_results = []
    if RESULTS_DIR.exists():
        for f in sorted(RESULTS_DIR.glob("*.json")):
            with open(f) as fh:
                data = json.load(fh)
                all_results.extend(data.get("results", []))

    if not all_results:
        print("No historical results yet.")
        return

    stats = calculate_stats(all_results)
    print(f"\n{'='*50}")
    print(f"  ALL-TIME PERFORMANCE")
    print(f"{'='*50}")
    print(f"  Record: {stats['wins']}W - {stats['losses']}L ({stats['win_rate']}%)")
    print(f"  Overs: {stats['over_win_rate']}% hit rate")
    print(f"  Unders: {stats['under_win_rate']}% hit rate")
    print(f"\n  By Prop Type:")
    for prop, record in stats.get("by_prop", {}).items():
        total = record["wins"] + record["losses"]
        wr = record["wins"] / total * 100 if total else 0
        print(f"    {prop}: {record['wins']}W-{record['losses']}L ({wr:.0f}%)")
    print()


def cleanup_old_files():
    """Remove daily files older than MAX_DAILY_FILES days. Keep results forever."""
    cutoff = datetime.now() - timedelta(days=MAX_DAILY_FILES)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    for pattern in ["*_props.json", "*_analysis.json", "*_picks.json"]:
        for f in DATA_DIR.glob(pattern):
            date_part = f.stem.split("_")[0]
            if date_part < cutoff_str:
                f.unlink()


def main():
    import sys

    # Auto-cleanup on every run
    cleanup_old_files()

    if "--grade" in sys.argv:
        results = grade_yesterday()
        if results:
            # Check if it's Friday — run weekly reflection
            if datetime.now().weekday() == 4:
                all_results = []
                if RESULTS_DIR.exists():
                    for f in sorted(RESULTS_DIR.glob("*.json")):
                        with open(f) as fh:
                            data = json.load(fh)
                            all_results.extend(data.get("results", []))
                if all_results:
                    print("\n=== Weekly Reflection ===")
                    lessons, stats = weekly_reflection(all_results)
                    for l in lessons:
                        print(f"  📝 {l}")

    elif "--stats" in sys.argv:
        show_all_time_stats()

    elif "--lessons" in sys.argv:
        lessons_data = load_lessons()
        print("=== Accumulated Lessons ===")
        for l in lessons_data.get("lessons", []):
            print(f"  • {l}")

    else:
        print("Usage:")
        print("  python3 tracker.py --grade    # Grade yesterday's picks")
        print("  python3 tracker.py --stats    # Show all-time performance")
        print("  python3 tracker.py --lessons  # Show learned lessons")


if __name__ == "__main__":
    main()
