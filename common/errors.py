"""Shared error tracking for sports pipeline. Uses enums for error classification."""

from enum import Enum
from datetime import datetime


class ErrorType(Enum):
    PRIZEPICKS_BLOCKED = "PrizePicks API blocked (403)"
    UNDERDOG_FAILED = "Underdog Fantasy API failed"
    ESPN_TIMEOUT = "ESPN API timeout"
    ESPN_NO_DATA = "ESPN returned no data"
    NBA_API_BLOCKED = "stats.nba.com blocked/timeout"
    PLAYER_NOT_FOUND = "Player not found in ESPN search"
    GAME_LOG_EMPTY = "Player has no game log data"
    TEAM_UNKNOWN = "Team abbreviation not mapped to any game"
    BEDROCK_FAILED = "Claude/Bedrock API call failed"
    BEDROCK_EMPTY = "Claude returned empty response"
    PREDICTIONS_EMPTY = "Game predictions unavailable (no team stats)"
    PROPS_EMPTY = "No props available from any source"


class PipelineErrors:
    """Collects errors during a pipeline run and formats them for output."""

    def __init__(self):
        self.errors = []

    def add(self, error_type: ErrorType, detail: str = ""):
        self.errors.append({
            "type": error_type,
            "detail": detail,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    def has_errors(self):
        return len(self.errors) > 0

    def summary(self):
        """One-line summary for terminal output."""
        if not self.errors:
            return ""
        counts = {}
        for e in self.errors:
            name = e["type"].value
            counts[name] = counts.get(name, 0) + 1
        parts = [f"{v}x {k}" for k, v in counts.items()]
        return f"  Warnings: {', '.join(parts)}"

    def to_markdown(self):
        """Markdown section for the report."""
        if not self.errors:
            return ""
        md = "## Pipeline Warnings\n\n"
        md += "| Type | Detail |\n|------|--------|\n"
        seen = set()
        for e in self.errors:
            key = (e["type"].value, e["detail"])
            if key in seen:
                continue
            seen.add(key)
            md += f"| {e['type'].value} | {e['detail']} |\n"
        md += "\n"
        return md

    def to_log(self):
        """Plain text for log files."""
        if not self.errors:
            return ""
        lines = ["PIPELINE WARNINGS:"]
        for e in self.errors:
            lines.append(f"  [{e['time']}] {e['type'].value}: {e['detail']}")
        return "\n".join(lines)
