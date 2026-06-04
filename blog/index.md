---
layout: default
title: Home
---

AI-powered NBA + WNBA + MLB prop predictions using quantitative edge scoring with Claude validation.

## How It Works

1. **Data**: Player stats from ESPN + MLB Stats API, lines from BettingPros (DraftKings/FanDuel consensus)
2. **Projection**: Weighted model (45% L5, 35% L10, 20% season)
3. **Edge Score**: Composite of edge size, hit rate, consistency, and trend alignment
4. **Validation**: Claude reviews top edges and assigns confidence

## Today's Picks

{% assign nba_sorted = site.nba | sort: "date" | reverse %}
{% assign wnba_sorted = site.wnba | sort: "date" | reverse %}
{% assign mlb_sorted = site.mlb | sort: "date" | reverse %}

### NBA
{% for post in nba_sorted limit:1 %}
[{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

### WNBA
{% for post in wnba_sorted limit:1 %}
[{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

### MLB
{% for post in mlb_sorted limit:1 %}
[{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

## Recent Reports

### NBA
{% for post in nba_sorted limit:5 %}
- [{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

### WNBA
{% for post in wnba_sorted limit:5 %}
- [{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

### MLB
{% for post in mlb_sorted limit:5 %}
- [{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

---

*Not financial advice. AI predictions for entertainment and research purposes.*
