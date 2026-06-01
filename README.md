# Sports Betting — AI Props Predictions

Automated NBA + WNBA player prop predictions using quantitative edge scoring with Claude validation.

## Daily Picks

**Blog**: [kai-ion.github.io/sports-betting](https://kai-ion.github.io/sports-betting/)

Reports are generated daily at 11:00 AM ET and emailed.

## How It Works

```
ESPN Game Logs → Weighted Projection → Edge Score → Claude Filter → Final Picks
```

### 1. Data (ESPN + Underdog Fantasy)
- Player game logs (current + last season)
- L5, L10, season averages for PTS, REB, AST, 3PM
- Team standings for game predictions
- Live lines from Underdog Fantasy (PrizePicks blocked on EC2)

### 2. Projection Model
Weighted average per stat:
- 45% Last 5 games (recency)
- 35% Last 10 games (stability)
- 20% Season average (baseline)

### 3. Edge Scoring (0-100)
| Factor | Weight | What it measures |
|--------|--------|------------------|
| Edge size | 40% | How far projection is from the line |
| Hit rate | 30% | How often player actually clears this line in recent games |
| Consistency | 20% | Standard deviation — lower = more predictable |
| Trend alignment | 10% | Is the player hot/cold in the right direction? |

### 4. Claude Validation
- Receives top 30 edges ranked by score
- Removes picks with clear disqualifiers (injury, blowout, minutes restriction)
- Reorders by confidence
- Keeps most picks — only cuts with a real reason

## Report Columns

| Column | Meaning |
|--------|---------|
| Proj | Model's projected stat value |
| Edge% | How far projection is from line as % |
| L5 HR | Hit rate over last 5 games |
| L10 HR | Hit rate over last 10 games |
| H2H | Hit rate in head-to-head matchups |
| Conf | Claude's confidence (1-10) |

## Architecture

```
SportsAnalysis/
├── nba/
│   ├── props/
│   │   ├── fetch.py         Pulls lines from PrizePicks + Underdog
│   │   ├── model.py         Player profiles from ESPN game logs (cached daily)
│   │   ├── picks.py         Edge scoring + Claude validation
│   │   └── analyze.py       Game odds from ESPN
│   ├── games/
│   │   └── predict.py       Moneyline/spread predictions from ESPN standings
│   ├── data/                JSON (props, predictions, picks)
│   └── reports/             Daily markdown reports
├── wnba/
│   ├── props/
│   │   ├── fetch.py         PrizePicks + Underdog (with WNBA fallback)
│   │   └── picks.py         Edge scoring (ESPN game logs) + Claude
│   ├── games/
│   │   └── predict.py       WNBA game predictions
│   ├── data/
│   └── reports/
├── common/
│   ├── errors.py            Error tracking with enums
│   └── tracker.py           Grading + lessons from past picks
├── blog/                    Jekyll site (GitHub Pages)
│   ├── generate_posts.py    Converts reports to blog posts
│   └── _config.yml
└── run_daily.sh             Two-phase runner (stats + picks)
```

## Schedule

| Time (ET) | What |
|-----------|------|
| 9:00 AM | Phase 1: Grade yesterday's picks, pre-cache player stats |
| 11:00 AM | Phase 2: Fetch lines, compute edges, Claude validates, email report |

## Data Sources

| Source | What | Status |
|--------|------|--------|
| ESPN Scoreboard | Game odds (spread, O/U, ML) | ✅ Works from EC2 |
| ESPN Standings | Team stats (PPG, OPP PPG, win%) | ✅ Works from EC2 |
| ESPN Game Logs | Player stats per game | ✅ Works from EC2 |
| Underdog Fantasy | Player prop lines | ✅ Works from EC2 |
| PrizePicks | Player prop lines | ❌ Blocked from EC2 (403) |
| Claude (Bedrock) | Final pick validation | ✅ Via IAM role |

## Error Handling

Pipeline errors are tracked with enums and reported in two places:
1. **In the report** — "Pipeline Warnings" section at the bottom
2. **Email alert** — sent immediately if any step crashes

Error types: `PRIZEPICKS_BLOCKED`, `UNDERDOG_FAILED`, `ESPN_TIMEOUT`, `PLAYER_NOT_FOUND`, `GAME_LOG_EMPTY`, `TEAM_UNKNOWN`, `BEDROCK_FAILED`, `BEDROCK_EMPTY`

## Setup

### EC2 (production)
```bash
# Cron runs at 9 AM + 11 AM ET daily
# Uses ESPN for all data (nba_api blocked from EC2)
# Sends email via SES after picks generate
```

### Local (development)
```bash
# Requires mise Python 3.12 with: requests boto3 pandas
# run_daily.sh sets correct PATH for mise
```
