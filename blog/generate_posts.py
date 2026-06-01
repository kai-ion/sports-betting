#!/usr/bin/env python3
"""Convert daily NBA/WNBA reports into Jekyll blog posts."""

from pathlib import Path
from datetime import datetime

REPO_ROOT = Path(__file__).parent.parent
NBA_REPORTS = REPO_ROOT / "nba" / "reports"
WNBA_REPORTS = REPO_ROOT / "wnba" / "reports"
BLOG_DIR = Path(__file__).parent
NBA_DIR = BLOG_DIR / "_nba"
WNBA_DIR = BLOG_DIR / "_wnba"


def generate_posts(reports_dir, output_dir, sport):
    """Convert report .md files into Jekyll posts with front matter."""
    output_dir.mkdir(exist_ok=True)

    if not reports_dir.exists():
        print(f"No {sport} reports directory")
        return

    count = 0
    for report in sorted(reports_dir.glob("*.md")):
        date_str = report.stem
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        post_path = output_dir / f"{date_str}.md"
        content = report.read_text()

        front_matter = f"""---
layout: post
title: "{sport} Picks — {date.strftime('%B %d, %Y')}"
date: {date_str}
categories: {sport.lower()}
---

"""
        post_path.write_text(front_matter + content)
        count += 1

    print(f"Generated {count} {sport} posts")


def main():
    print("Generating blog posts...")
    generate_posts(NBA_REPORTS, NBA_DIR, "NBA")
    generate_posts(WNBA_REPORTS, WNBA_DIR, "WNBA")
    print("Done!")


if __name__ == "__main__":
    main()
