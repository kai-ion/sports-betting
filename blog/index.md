---
layout: default
title: Home
---

AI-powered NBA + WNBA prop predictions using quantitative edge scoring with Claude validation.

## How It Works

1. **Data**: Player game logs from ESPN (last 5, last 10, season averages)
2. **Projection**: Weighted model (45% L5, 35% L10, 20% season)
3. **Edge Score**: Composite of edge size, hit rate, consistency, and trend alignment
4. **Validation**: Claude reviews top edges and removes picks with hidden risk (blowouts, injuries)

## Today's Picks

{% assign nba_sorted = site.nba | sort: "date" | reverse %}
{% assign wnba_sorted = site.wnba | sort: "date" | reverse %}

### NBA
{% for post in nba_sorted limit:1 %}
[{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

### WNBA
{% for post in wnba_sorted limit:1 %}
[{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

## Recent Reports

### NBA
{% for post in nba_sorted limit:7 %}
- [{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

### WNBA
{% for post in wnba_sorted limit:7 %}
- [{{ post.title }}]({{ post.url | relative_url }})
{% endfor %}

---

*Not financial advice. AI predictions for entertainment and research purposes.*
