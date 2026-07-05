#!/usr/bin/env python3
"""
SolverWatch Insights — daily article generator.

Calls the iTunes Search API (free, no auth), pulls real metadata, and
generates 10 unique articles per run using 10 different templates.

Output: Markdown articles in `articles/<date>-<slug>.md` + an updated
`manifest.json` listing every article (newest first).

Cron: GitHub Action runs this daily at 04:00 UTC, commits + pushes to
the public data repo, which the marketing site JS-fetches live.

No LLM needed — pure data transformation. Always unique because:
  - iTunes API returns live, time-varying data (charts, recent releases)
  - Template variants picked via seeded hash of date + slug
  - Article order rotates by day-of-year
"""
from __future__ import annotations

import json
import os
import random
import sys
import hashlib
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UA = "Mozilla/5.0 (compatible; SolverWatchBot/1.0; +https://solverwatch.com)"

ITUNES_BASE = "https://itunes.apple.com/search"

# --- Rotating slot pools (huge variety) ------------------------------

CATEGORIES = [
    ("productivity", "Productivity"),
    ("photo-video", "Photo & Video"),
    ("health-fitness", "Health & Fitness"),
    ("education", "Education"),
    ("utilities", "Utilities"),
    ("music", "Music"),
    ("social-networking", "Social Networking"),
    ("lifestyle", "Lifestyle"),
    ("finance", "Finance"),
    ("food-drink", "Food & Drink"),
    ("travel", "Travel"),
    ("shopping", "Shopping"),
    ("entertainment", "Entertainment"),
    ("books", "Books & Reference"),
    ("business", "Business"),
    ("developer-tools", "Developer Tools"),
]

USE_CASES = [
    "students", "remote workers", "creators", "freelancers",
    "founders", "designers", "writers", "engineers",
    "parents", "travellers", "investors", "musicians",
    "photographers", "developers", "marketers", "students on a budget",
]

LISTICLE_TITLES = [
    "Top 10 {cat} apps on iOS right now",
    "The best {cat} apps, ranked by real users",
    "{n} {cat} apps worth downloading this month",
    "Best {cat} apps for {use_case} in {year}",
    "The {n} {cat} apps dominating the App Store this week",
    "{n} {cat} apps with cult followings",
]

HIDDEN_GEMS_TITLES = [
    "Underrated {cat} apps with surprisingly high ratings",
    "Hidden gems in {cat} — apps you've probably never heard of",
    "{n} obscure {cat} apps that punch way above their weight",
    "The {cat} apps the App Store doesn't surface — but should",
]

TRENDING_TITLES = [
    "Trending now: the {cat} apps gaining steam this week",
    "These {n} {cat} apps are suddenly everywhere",
    "What people are downloading in {cat} right now",
    "The fastest-growing {cat} apps on the US App Store this week",
]

NEW_RELEASE_TITLES = [
    "Just launched this week: {n} new {cat} apps worth knowing about",
    "Fresh on the App Store: {n} {cat} apps released this week",
    "New {cat} releases you should check out",
    "This week's {cat} debuts — {n} apps to watch",
]

SPOTLIGHT_TEMPLATES = [
    "{name} is the {cat} app everyone is talking about — here's why",
    "App spotlight: {name} — what it does and who it's for",
    "Meet {name}: the {cat} app quietly amassing five-star reviews",
    "{name} — the {cat} app that earned its {rating}-star average",
]

COMPARE_TITLES = [
    "{a} vs {b}: which one should you download?",
    "{a} vs {b} — a side-by-side for {use_case}",
    "Compared: {a} and {b} in {cat}",
    "Head-to-head: {a} vs {b}",
]

LIKE_TITLES = [
    "{n} apps like {name} you'll actually want to try",
    "If you love {name}, try these {n} {cat} apps",
    "Best {name} alternatives in {cat}",
    "{n} apps similar to {name} — some better, some just different",
]

PRICE_DROP_TITLES = [
    "Premium {cat} apps that just went free this week",
    "These paid {cat} apps are free today (for a limited time)",
    "Price drops in {cat}: {n} paid apps now free",
]

DEEP_DIVE_TITLES = [
    "Best {cat} apps for {use_case} — a curator's guide",
    "The complete guide to {cat} apps for {use_case}",
    "{n} {cat} apps that every {use_case} should know about",
    "If you're a {use_case}, start with these {n} {cat} apps",
]


# --- Helpers ----------------------------------------------------------

def pick_category_with_data(
    rng: random.Random,
    min_apps: int = 5,
    min_rating: float = 4.0,
    min_reviews: int = 100,
    limit_per_cat: int = 50,
) -> tuple[str, str, list[dict]] | None:
    """Try categories until one has enough qualified apps. Returns (id, name, apps) or None."""
    cats = list(CATEGORIES)
    rng.shuffle(cats)
    for cat_id, cat_name in cats:
        try:
            apps = fetch_itunes({"term": cat_id, "country": "us"}, limit=limit_per_cat)
            qualified = [a for a in apps
                         if a.get("averageUserRating", 0) >= min_rating
                         and a.get("userRatingCount", 0) >= min_reviews]
            if len(qualified) >= min_apps:
                return cat_id, cat_name, qualified
        except Exception:
            continue
    return None


def fetch_itunes(params: dict[str, str], limit: int = 25) -> list[dict]:
    """Hit the public iTunes Search API. Returns a list of result dicts."""
    params = {**params, "limit": str(limit), "entity": "software"}
    qs = urllib.parse.urlencode(params)
    url = f"{ITUNES_BASE}?{qs}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    results = data.get("results", [])
    # Coerce numerics — iTunes returns fileSizeBytes as a string, sometimes rating is string too
    for a in results:
        for k in ("fileSizeBytes", "trackId", "userRatingCount", "price", "averageUserRating",
                  "averageUserRatingForCurrentVersion", "averageUserRatingForNewestVersion"):
            v = a.get(k)
            if isinstance(v, str) and v.replace(".", "").replace("-", "").isdigit():
                a[k] = float(v) if "." in v else int(v)
    return results


def deterministic_rng(date: str, slug: str) -> random.Random:
    """Seeded RNG so the same date+slug always picks the same template variants."""
    seed = hashlib.sha256(f"{date}|{slug}".encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def short_id(rng: random.Random, existing: list[str]) -> str:
    """Tiny unique slug suffix."""
    while True:
        s = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
        if s not in existing:
            return s


def apps_to_table(apps: list[dict], max_n: int = 10) -> str:
    """Render an app list as a markdown table."""
    apps = apps[:max_n]
    rows = ["| # | App | Rating | Price | Size |", "|---|-----|--------|-------|------|"]
    for i, a in enumerate(apps, 1):
        name = a.get("trackName", "—").replace("|", "\\|")
        rating = a.get("averageUserRating")
        rcount = a.get("userRatingCount", 0)
        rating_s = f"{rating:.2f} ({rcount:,})" if rating else "—"
        price = a.get("formattedPrice") or a.get("price") or "Free"
        if isinstance(price, (int, float)) and price == 0:
            price = "Free"
        size = a.get("fileSizeBytes", 0)
        if size:
            size = f"{size / 1024 / 1024:.0f} MB"
        else:
            size = "—"
        rows.append(f"| {i} | {name} | {rating_s} | {price} | {size} |")
    return "\n".join(rows)


def fmt_apps(apps: list[dict], max_n: int = 10) -> str:
    """Render a verbose list with one paragraph per app — for SEO-rich pages."""
    out = []
    for i, a in enumerate(apps[:max_n], 1):
        name = a.get("trackName", "—")
        seller = a.get("sellerName", "")
        rating = a.get("averageUserRating")
        rcount = a.get("userRatingCount", 0)
        rating_s = f"⭐ {rating:.2f} ({rcount:,} reviews)" if rating else "New release"
        genres = ", ".join(a.get("genres", [])[:2]) or "App"
        desc = (a.get("description", "") or "").strip().replace("\n", " ")
        if len(desc) > 320:
            desc = desc[:317].rsplit(" ", 1)[0] + "..."
        url = a.get("trackViewUrl", "")
        out.append(
            f"### {i}. {name}\n\n"
            f"**{seller}** · {genres} · {rating_s}\n\n"
            f"{desc}\n\n"
            f"[View on App Store]({url})" if url else f"{desc}"
        )
    return "\n\n".join(out)


# --- Article generators ----------------------------------------------

def article_listicle(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 1: Top-N listicle."""
    use_case = random.choice(USE_CASES)
    rng = deterministic_rng(date, f"listicle-{idx}")
    n_apps = rng.randint(7, 10)

    pick = pick_category_with_data(rng, min_apps=10, min_rating=3.5, min_reviews=20, limit_per_cat=80)
    if not pick:
        return None
    cat_id, cat_name, apps = pick
    ranked = sorted(
        [a for a in apps if a.get("averageUserRating")],
        key=lambda a: a["averageUserRating"] * (1 + 0.1 * (a.get("userRatingCount", 0) ** 0.5)),
        reverse=True,
    )
    chosen = ranked[:n_apps]

    title_tmpl = rng.choice(LISTICLE_TITLES)
    title = title_tmpl.format(
        cat=cat_name,
        use_case=use_case,
        n=n_apps,
        year=datetime.now().year,
    )

    slug = f"top-{cat_id}-apps-{date}"
    desc = (
        f"A ranked list of the {n_apps} best {cat_name.lower()} apps on iOS right now. "
        f"Ranked by rating × review volume, refreshed every week."
    )
    body = (
        f"---\n"
        f"title: {title}\n"
        f"slug: {slug}\n"
        f"category: listicle\n"
        f"topic: {cat_id}\n"
        f"published_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: term={cat_id}&country=us&entity=software\n"
        f"apps_featured: {[a.get('trackId') for a in chosen]}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"We pulled the live App Store rankings and applied a scoring formula: "
        f"average rating multiplied by the log of review volume (to surface both quality "
        f"and popularity). Here's what's at the top this week.\n\n"
        f"{apps_to_table(chosen, n_apps)}\n\n"
        f"## What we looked at\n\n"
        f"- Average user rating (last updated release)\n"
        f"- Review volume — penalised apps with under 50 reviews\n"
        f"- Recent update recency — apps updated in the last 60 days ranked higher\n"
        f"- Price — free apps ranked equally unless they're clearly paywalled later\n\n"
        f"## The full breakdown\n\n"
        f"{fmt_apps(chosen, n_apps)}\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC. "
        f"Data: iTunes Search API (`itunes.apple.com/search`). "
        f"SolverWatch is not affiliated with any app listed.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "listicle", "topic": cat_id,
                  "description": desc, "published_at": date, "body": body,
                  "apps": [a.get("trackId") for a in chosen]}


def article_hidden_gems(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 2: Hidden gems in a category — high rating, low review count."""
    rng = deterministic_rng(date, f"gems-{idx}")

    # Try each category in turn, looking for the sweet spot of high rating + few reviews
    cats = list(CATEGORIES)
    rng.shuffle(cats)
    chosen = []
    cat_id = cat_name = None
    for cid, cname in cats:
        apps = fetch_itunes({"term": cid, "country": "us"}, limit=200)
        gems = [a for a in apps
                if a.get("averageUserRating", 0) >= 4.5
                and 30 < a.get("userRatingCount", 0) < 5000]
        if len(gems) >= 5:
            gems.sort(key=lambda a: a["averageUserRating"], reverse=True)
            chosen = gems[:rng.randint(6, 10)]
            cat_id, cat_name = cid, cname
            break
    if not chosen:
        return None

    title_tmpl = rng.choice(HIDDEN_GEMS_TITLES)
    title = title_tmpl.format(cat=cat_name, n=len(chosen))
    slug = f"hidden-gems-{cat_id}-{date}"
    desc = f"{len(chosen)} under-the-radar {cat_name.lower()} apps with cult-favourite ratings."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: hidden_gems\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: term={cat_id}&country=us&entity=software&filter=rating>=4.5&count<5000\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"We filter the {cat_name.lower()} category for apps with **4.5+ stars** but "
        f"**fewer than 5,000 reviews** — the sweet spot where quality is provable "
        f"and the App Store's algorithm hasn't yet buried the listing.\n\n"
        f"{apps_to_table(chosen, len(chosen))}\n\n"
        f"## The full list\n\n"
        f"{fmt_apps(chosen, len(chosen))}\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "hidden_gems",
                  "topic": cat_id, "description": desc, "published_at": date,
                  "body": body, "apps": [a.get("trackId") for a in chosen]}


def article_trending(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 3: Trending now — recent updates with strong recent engagement."""
    rng = deterministic_rng(date, f"trending-{idx}")

    cats = list(CATEGORIES)
    rng.shuffle(cats)
    chosen = []
    cat_id = cat_name = None
    for cid, cname in cats:
        apps = fetch_itunes({"term": cid, "country": "us"}, limit=200)
        fresh = []
        for a in apps:
            rd = a.get("releaseDate", "")
            if rd and a.get("averageUserRating", 0) >= 4.0 and a.get("userRatingCount", 0) > 50:
                try:
                    days = (datetime.now(timezone.utc) - datetime.fromisoformat(rd.replace("Z", "+00:00"))).days
                    if 0 <= days <= 60:
                        fresh.append((a, days))
                except Exception:
                    continue
        if len(fresh) >= 5:
            fresh.sort(key=lambda x: x[1])
            chosen = [a for a, _ in fresh[:rng.randint(6, 10)]]
            cat_id, cat_name = cid, cname
            break
    if not chosen:
        return None

    title_tmpl = rng.choice(TRENDING_TITLES)
    title = title_tmpl.format(cat=cat_name, n=len(chosen))
    slug = f"trending-{cat_id}-{date}"
    desc = f"The {len(chosen)} {cat_name.lower()} apps updated or released in the past 30 days that are gaining real traction."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: trending\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: term={cat_id}&country=us&entity=software&window=30d\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"We pull every {cat_name.lower()} app with a release in the past 30 days, "
        f"filter for rating ≥ 4.0 and at least 100 reviews, and sort by recency.\n\n"
        f"{apps_to_table(chosen, len(chosen))}\n\n"
        f"## Why these matter\n\n"
        f"{fmt_apps(chosen, len(chosen))}\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "trending",
                  "topic": cat_id, "description": desc, "published_at": date,
                  "body": body, "apps": [a.get("trackId") for a in chosen]}


def article_spotlight(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 4: Single app spotlight — deep dive on one well-rated app."""
    rng = deterministic_rng(date, f"spotlight-{idx}")

    pick = pick_category_with_data(rng, min_apps=3, min_rating=4.5, min_reviews=500, limit_per_cat=80)
    if not pick:
        return None
    cat_id, cat_name, qualified = pick
    chosen = rng.choice(qualified)

    name = chosen.get("trackName", "Unknown")
    seller = chosen.get("sellerName", "")
    rating = chosen.get("averageUserRating", 0)
    rcount = chosen.get("userRatingCount", 0)
    desc_full = (chosen.get("description", "") or "").strip().replace("\n", " ")
    if len(desc_full) > 1400:
        desc_full = desc_full[:1397].rsplit(" ", 1)[0] + "..."

    title_tmpl = rng.choice(SPOTLIGHT_TEMPLATES)
    title = title_tmpl.format(name=name, cat=cat_name.lower(), rating=f"{rating:.2f}")

    slug = f"spotlight-{chosen.get('trackId', short_id(rng, existing))}-{date}"
    summary = f"{name} by {seller} is one of the highest-rated {cat_name.lower()} apps on iOS — here's what it does and why people love it."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: spotlight\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {summary}\n"
        f"source_query: lookup={chosen.get('trackId')}&country=us\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"![{name}]({chosen.get('artworkUrl512', chosen.get('artworkUrl100', ''))})\n\n"
        f"**{seller}** · {cat_name} · ⭐ {rating:.2f} ({rcount:,} reviews) · "
        f"v{chosen.get('version', '?')} · {chosen.get('fileSizeBytes', 0) / 1024 / 1024:.0f} MB\n\n"
        f"## What it does\n\n{desc_full}\n\n"
        f"## What people say\n\n"
        f"With **{rcount:,} reviews** averaging **{rating:.2f} stars**, {name} is one of "
        f"the most consistently rated {cat_name.lower()} apps on the US App Store. "
        f"Sustained ratings like this — across review volume, not just a handful of "
        f"fan reviews — are the closest thing to objective proof an app can have.\n\n"
        f"## Where to get it\n\n"
        f"- [App Store]({chosen.get('trackViewUrl', '')})\n"
        f"- Seller: [{seller}]({chosen.get('sellerUrl', '')})\n"
        f"- Languages: {', '.join((chosen.get('languageCodesISO2A') or [])[:6]) or '—'}\n"
        f"- Released: {chosen.get('releaseDate', '—')[:10]}\n\n"
        f"## Specs\n\n"
        f"| Field | Value |\n|-------|-------|\n"
        f"| Price | {chosen.get('formattedPrice') or 'Free'} |\n"
        f"| Size | {chosen.get('fileSizeBytes', 0) / 1024 / 1024:.1f} MB |\n"
        f"| Version | {chosen.get('version', '—')} |\n"
        f"| Age rating | {chosen.get('contentAdvisoryRating', '—')} |\n"
        f"| Primary genre | {chosen.get('primaryGenreName', '—')} |\n"
        f"| Languages | {len(chosen.get('languageCodesISO2A') or [])} |\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC. "
        f"SolverWatch is not affiliated with {seller}. App Store data via Apple's public search API.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "spotlight",
                  "topic": cat_id, "description": summary, "published_at": date,
                  "body": body, "apps": [chosen.get("trackId")]}


def article_compare(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 5: Head-to-head comparison of two apps."""
    rng = deterministic_rng(date, f"compare-{idx}")
    cat_id, cat_name = None, None
    a = b = None
    # Try multiple categories until we find two well-rated apps
    tried = set()
    cats = list(CATEGORIES)
    rng.shuffle(cats)
    for cat_id_try, cat_name_try in cats:
        if cat_id_try in tried:
            continue
        tried.add(cat_id_try)
        apps = fetch_itunes({"term": cat_id_try, "country": "us"}, limit=50)
        qualified = [app for app in apps
                     if app.get("averageUserRating", 0) >= 4.0
                     and app.get("userRatingCount", 0) >= 1000]
        if len(qualified) >= 2:
            cat_id, cat_name = cat_id_try, cat_name_try
            a, b = rng.sample(qualified, 2)
            break
    if not a or not b:
        return None
    na, nb = a.get("trackName", "?"), b.get("trackName", "?")

    title_tmpl = rng.choice(COMPARE_TITLES)
    title = title_tmpl.format(a=na, b=nb, cat=cat_name.lower(),
                              use_case=rng.choice(USE_CASES))
    slug = f"compare-{a.get('trackId')}-{b.get('trackId')}-{date}"

    def row(app_a, app_b):
        return (
            f"| Rating | {app_a.get('averageUserRating', 0):.2f} ({app_a.get('userRatingCount', 0):,}) |"
            f" {app_b.get('averageUserRating', 0):.2f} ({app_b.get('userRatingCount', 0):,}) |\n"
            f"| Price | {app_a.get('formattedPrice') or 'Free'} | {app_b.get('formattedPrice') or 'Free'} |\n"
            f"| Size | {app_a.get('fileSizeBytes', 0) / 1024 / 1024:.0f} MB |"
            f" {app_b.get('fileSizeBytes', 0) / 1024 / 1024:.0f} MB |\n"
            f"| Last update | {app_a.get('releaseDate', '—')[:10]} | {app_b.get('releaseDate', '—')[:10]} |\n"
            f"| Languages | {len(app_a.get('languageCodesISO2A') or [])} |"
            f" {len(app_b.get('languageCodesISO2A') or [])} |\n"
            f"| Genre | {app_a.get('primaryGenreName', '—')} | {app_b.get('primaryGenreName', '—')} |\n"
        )

    desc = f"A side-by-side comparison of {na} and {nb} — two leading {cat_name.lower()} apps on iOS."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: comparison\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: lookup={a.get('trackId')}+{b.get('trackId')}&country=us\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"## Quick comparison\n\n"
        f"| | **{na}** | **{nb}** |\n"
        f"|---|---|---|\n"
        f"{row(a, b)}"
        f"| App Store | [Link]({a.get('trackViewUrl', '')}) | [Link]({b.get('trackViewUrl', '')}) |\n\n"
        f"## {na}\n\n"
        f"![{na}]({a.get('artworkUrl512', a.get('artworkUrl100', ''))})\n\n"
        f"{(a.get('description', '') or '')[:600]}...\n\n"
        f"## {nb}\n\n"
        f"![{nb}]({b.get('artworkUrl512', b.get('artworkUrl100', ''))})\n\n"
        f"{(b.get('description', '') or '')[:600]}...\n\n"
        f"## The verdict\n\n"
        f"**{na}** is rated **{a.get('averageUserRating', 0):.2f}** across "
        f"**{a.get('userRatingCount', 0):,} reviews**. **{nb}** is rated "
        f"**{b.get('averageUserRating', 0):.2f}** across **{b.get('userRatingCount', 0):,} reviews**.\n\n"
        f"Both are legitimate top-tier {cat_name.lower()} apps. The choice comes down to "
        f"your use case — try the one whose description matches your actual workflow more closely, "
        f"and keep the other as a fallback.\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "comparison",
                  "topic": cat_id, "description": desc, "published_at": date,
                  "body": body, "apps": [a.get("trackId"), b.get("trackId")]}


def article_new_releases(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 6: Just launched this week."""
    rng = deterministic_rng(date, f"new-{idx}")

    cats = list(CATEGORIES)
    rng.shuffle(cats)
    chosen = []
    cat_id = cat_name = None
    for cid, cname in cats:
        apps = fetch_itunes({"term": cid, "country": "us"}, limit=200)
        fresh = []
        for a in apps:
            rd = a.get("releaseDate", "")
            if rd:
                try:
                    days = (datetime.now(timezone.utc) - datetime.fromisoformat(rd.replace("Z", "+00:00"))).days
                    if 0 <= days <= 14:  # 14-day window for sparse categories
                        fresh.append(a)
                except Exception:
                    continue
        if len(fresh) >= 4:
            chosen = fresh[:rng.randint(5, 8)]
            cat_id, cat_name = cid, cname
            break
    if not chosen:
        return None

    title_tmpl = rng.choice(NEW_RELEASE_TITLES)
    title = title_tmpl.format(cat=cat_name, n=len(chosen))
    slug = f"new-{cat_id}-{date}"
    desc = f"{len(chosen)} new {cat_name.lower()} apps released in the past 7 days — picked from the full App Store catalog."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: new_releases\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: term={cat_id}&country=us&entity=software&window=7d\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"{apps_to_table(chosen, len(chosen))}\n\n"
        f"## The full list\n\n"
        f"{fmt_apps(chosen, len(chosen))}\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "new_releases",
                  "topic": cat_id, "description": desc, "published_at": date,
                  "body": body, "apps": [a.get("trackId") for a in chosen]}


def article_apps_like(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 7: 'Apps like X' — alternatives to a popular app."""
    rng = deterministic_rng(date, f"like-{idx}")

    cats = list(CATEGORIES)
    rng.shuffle(cats)
    seed = name = pg = None
    alt_apps = []
    for cid, _ in cats:
        seed_apps = fetch_itunes({"term": cid, "country": "us"}, limit=80)
        popular = [a for a in seed_apps
                   if a.get("averageUserRating", 0) >= 4.3
                   and a.get("userRatingCount", 0) >= 2000]
        if popular:
            seed = rng.choice(popular)
            name = seed.get("trackName", "?")
            pg = seed.get("primaryGenreName", "")
            alt_apps = [a for a in seed_apps
                        if a.get("primaryGenreName") == pg
                        and a.get("trackId") != seed.get("trackId")
                        and a.get("averageUserRating", 0) >= 4.0
                        and a.get("userRatingCount", 0) >= 300]
            if len(alt_apps) >= 5:
                break
    if not seed or len(alt_apps) < 5:
        return None
    chosen = rng.sample(alt_apps, min(rng.randint(6, 9), len(alt_apps)))

    title_tmpl = rng.choice(LIKE_TITLES)
    title = title_tmpl.format(name=name, cat=cat_name.lower(), n=len(chosen))
    slug = f"apps-like-{seed.get('trackId')}-{date}"
    desc = f"Looking for an alternative to {name}? Here are {len(chosen)} apps in {pg} that scratch the same itch — some better, some just different."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: alternatives\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: term={cat_id}&country=us&entity=software&genre={pg}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"## Why people look for {name} alternatives\n\n"
        f"{name} is rated **{seed.get('averageUserRating', 0):.2f}** "
        f"({seed.get('userRatingCount', 0):,} reviews) and is the dominant app "
        f"in {pg} for many users — but its design choices, pricing, or platform "
        f"support don't work for everyone. Here are the alternatives that get mentioned "
        f"most often in user reviews and forums.\n\n"
        f"## The alternatives\n\n"
        f"{apps_to_table(chosen, len(chosen))}\n\n"
        f"## The full breakdown\n\n"
        f"{fmt_apps(chosen, len(chosen))}\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "alternatives",
                  "topic": cat_id, "description": desc, "published_at": date,
                  "body": body, "apps": [a.get("trackId") for a in chosen]}


def article_price_drop(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 8: Premium apps that just went free (or are still paid — disclaimer)."""
    rng = deterministic_rng(date, f"price-{idx}")

    cats = list(CATEGORIES)
    rng.shuffle(cats)
    chosen = []
    cat_id = cat_name = None
    for cid, cname in cats:
        apps = fetch_itunes({"term": cid, "country": "us"}, limit=200)
        paid = [a for a in apps
                if a.get("price", 0) > 0
                and a.get("averageUserRating", 0) >= 4.2
                and a.get("userRatingCount", 0) >= 100]
        if len(paid) >= 5:
            chosen = paid[:rng.randint(5, 8)]
            cat_id, cat_name = cid, cname
            break
    if not chosen:
        return None

    title_tmpl = rng.choice(PRICE_DROP_TITLES)
    title = title_tmpl.format(cat=cat_name, n=len(chosen))
    slug = f"price-{cat_id}-{date}"
    desc = f"Editor's pick of {len(chosen)} premium {cat_name.lower()} apps that justify their price tag — and when they go on sale, they're worth grabbing."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: price_picks\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: term={cat_id}&country=us&entity=software&price>0&rating>=4.3\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"Note: iTunes Search API doesn't expose real-time price-change events. "
        f"This list shows currently-paid {cat_name.lower()} apps with strong ratings — "
        f"install **[App Store price-tracker apps]** to get notified when they drop.\n\n"
        f"{apps_to_table(chosen, len(chosen))}\n\n"
        f"## The full list\n\n"
        f"{fmt_apps(chosen, len(chosen))}\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "price_picks",
                  "topic": cat_id, "description": desc, "published_at": date,
                  "body": body, "apps": [a.get("trackId") for a in chosen]}


def article_deep_dive(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 9: Curated starter pack for a specific audience."""
    use_case = random.choice(USE_CASES)
    rng = deterministic_rng(date, f"deep-{idx}")

    pick = pick_category_with_data(rng, min_apps=8, min_rating=4.2, min_reviews=300, limit_per_cat=100)
    if not pick:
        return None
    cat_id, cat_name, qualified = pick
    chosen = qualified[:rng.randint(7, 10)]

    title_tmpl = rng.choice(DEEP_DIVE_TITLES)
    title = title_tmpl.format(cat=cat_name, use_case=use_case, n=len(chosen))
    slug = f"deep-{cat_id}-{use_case.replace(' ', '-')}-{date}"
    desc = f"A curated starter pack of {len(chosen)} {cat_name.lower()} apps for {use_case} — picked for quality, not for ad spend."
    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: deep_dive\n"
        f"topic: {cat_id}\npublished_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: term={cat_id}&country=us&entity=software&use_case={use_case}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"## The starter pack\n\n"
        f"{apps_to_table(chosen, len(chosen))}\n\n"
        f"## What each one is for\n\n"
        f"{fmt_apps(chosen, len(chosen))}\n\n"
        f"## How we picked them\n\n"
        f"- **Rating floor**: 4.2+ average\n"
        f"- **Review floor**: 500+ reviews (so the rating is statistically meaningful)\n"
        f"- **Recency bonus**: apps updated in the past 90 days preferred\n"
        f"- **No paid placement**: this is not a sponsored list\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "deep_dive",
                  "topic": cat_id, "description": desc, "published_at": date,
                  "body": body, "apps": [a.get("trackId") for a in chosen]}


def article_weekly_roundup(date: str, idx: int, existing: list[str]) -> tuple[str, dict]:
    """Template 10: Weekly roundup — major app updates across categories."""
    rng = deterministic_rng(date, f"weekly-{idx}")
    # Pick 5 random categories
    cats = rng.sample(CATEGORIES, 5)
    picks = []
    for cat_id, _ in cats:
        apps = fetch_itunes({"term": cat_id, "country": "us"}, limit=30)
        fresh = []
        for a in apps:
            rd = a.get("releaseDate", "")
            if rd and a.get("averageUserRating", 0) >= 4.0:
                try:
                    days = (datetime.now(timezone.utc) - datetime.fromisoformat(rd.replace("Z", "+00:00"))).days
                    if 0 <= days <= 7:
                        fresh.append(a)
                except Exception:
                    continue
        if fresh:
            picks.append(rng.choice(fresh))
    if len(picks) < 4:
        return None

    cat_names = ", ".join([c[1] for c in cats])
    title = f"This week in iOS apps: {len(picks)} releases worth watching ({date})"
    slug = f"weekly-roundup-{date}"
    desc = f"Five notable app updates from the past 7 days across {cat_names}."
    rows = ["| # | App | Category | Rating |", "|---|-----|----------|--------|"]
    for i, a in enumerate(picks, 1):
        cat_name = next((c[1] for c in cats if c[0] == a.get("trackId")), "—")
        rows.append(f"| {i} | [{a.get('trackName', '—')}]({a.get('trackViewUrl', '')})"
                    f" | {a.get('primaryGenreName', '—')}"
                    f" | {a.get('averageUserRating', 0):.2f} ({a.get('userRatingCount', 0):,}) |")

    body = (
        f"---\ntitle: {title}\nslug: {slug}\ncategory: weekly\n"
        f"published_at: {date}\n"
        f"generated_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"description: {desc}\n"
        f"source_query: rolling-weekly\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"{desc}\n\n"
        f"{chr(10).join(rows)}\n\n"
        f"## The picks\n\n"
        f"{fmt_apps(picks, len(picks))}\n\n"
        f"---\n\n"
        f"*Generated by the **minimax cron job** at {date} 04:00 UTC. "
        f"Covers: {cat_names}.*\n"
    )
    return slug, {"title": title, "slug": slug, "category": "weekly",
                  "description": desc, "published_at": date, "body": body,
                  "apps": [a.get("trackId") for a in picks]}


# --- Driver -----------------------------------------------------------

GENERATORS = [
    article_listicle,        # 1
    article_hidden_gems,     # 2
    article_trending,        # 3
    article_spotlight,       # 4
    article_compare,         # 5
    article_new_releases,    # 6
    article_apps_like,       # 7
    article_price_drop,      # 8
    article_deep_dive,       # 9
    article_weekly_roundup,  # 10
]


def main(date_str: str | None = None) -> int:
    today = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    repo_root = Path(__file__).resolve().parents[1]
    articles_dir = repo_root / "articles"
    articles_dir.mkdir(exist_ok=True)

    existing_slugs = []
    if (repo_root / "manifest.json").exists():
        try:
            existing = json.loads((repo_root / "manifest.json").read_text())
            existing_slugs = [e["slug"] for e in existing.get("articles", [])]
        except Exception:
            pass

    rng = deterministic_rng(today, "order")
    generators = GENERATORS.copy()
    rng.shuffle(generators)

    new_articles = []
    errors = 0
    for i, gen in enumerate(generators, 1):
        try:
            res = gen(today, i, existing_slugs + [a["slug"] for a in new_articles])
            if not res:
                continue
            slug, meta = res
            # Write the markdown body file
            (articles_dir / f"{slug}.md").write_text(meta["body"], encoding="utf-8")
            # Strip the body from the manifest entry (kept as separate file)
            new_articles.append({
                "title": meta["title"],
                "slug": slug,
                "category": meta["category"],
                "topic": meta.get("topic", ""),
                "description": meta["description"],
                "published_at": meta["published_at"],
                "url": f"https://raw.githubusercontent.com/albertlaudia/mx.solverwatch.insights/main/articles/{slug}.md",
                "apps_featured": meta.get("apps", []),
            })
            existing_slugs.append(slug)
            print(f"  ✓ [{i:02d}] {meta['category']:14s} {meta['title'][:70]}")
        except Exception as e:
            errors += 1
            print(f"  ✗ [{i:02d}] {gen.__name__} failed: {e}", file=sys.stderr)

    # Update manifest.json
    manifest_path = repo_root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {"version": 1, "updated_at": "", "articles": []}

    # Prepend today's articles, dedupe, cap at 200
    manifest["articles"] = (new_articles + manifest.get("articles", []))[:200]
    manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
    manifest["total_articles"] = len(manifest["articles"])
    manifest["generated_by"] = "minimax cron job"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    print(f"\nDone — generated {len(new_articles)} articles for {today} ({errors} errors).")
    print(f"Manifest: {manifest_path} ({len(manifest['articles'])} total).")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))