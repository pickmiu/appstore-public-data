#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_rankings.py - App Store Rankings Scraper and Daily Markdown Snapshot Generator Module

Features:
1. Monitored regions default to China (CN) and United States (US);
2. Strict game exclusion: automatically filters Games (6014) from main chart and pads to Top 100 non-game apps;
3. Concurrent category fetching (Top 10) via lightweight thread pool (< 5s execution time);
4. Automated standard Markdown snapshot generation (rankings/YYYY-MM-DD_{region}.md);
5. Automated 180-day lifecycle retention and cleanup of older snapshots.
"""

import os
import re
import sys
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

REQUEST_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


# Standard genre translation mapping from localized Apple category names to English
GENRE_TRANSLATION_MAP = {
    "购物": "Shopping",
    "娱乐": "Entertainment",
    "效率": "Productivity",
    "日常事务": "Productivity",
    "音乐": "Music",
    "摄影与录像": "Photo & Video",
    "社交": "Social Networking",
    "工具": "Utilities",
    "实用工具": "Utilities",
    "生活": "Lifestyle",
    "财务": "Finance",
    "新闻": "News",
    "教育": "Education",
    "商务": "Business",
    "健康健美": "Health & Fitness",
    "图书": "Books",
    "美食佳饮": "Food & Drink",
    "旅游": "Travel",
    "旅行": "Travel",
    "体育": "Sports",
    "医疗": "Medical",
    "参考": "Reference",
    "天气": "Weather",
    "导航": "Navigation",
    "儿童": "Kids",
    "报刊杂志": "Magazines & Newspapers",
    "杂志与报刊": "Magazines & Newspapers",
    "贴纸": "Stickers",
    "游戏": "Games",
}


def translate_genre_to_english(genre: str) -> str:
    """Normalize and translate category names to English while preserving original if unmapped."""
    if not genre:
        return ""
    g = genre.strip()
    if g in GENRE_TRANSLATION_MAP:
        return GENRE_TRANSLATION_MAP[g]
    m = re.search(r"\(([^)]+)\)", g)
    if m:
        return m.group(1).strip()
    for cn, en in GENRE_TRANSLATION_MAP.items():
        if cn in g:
            return en
    return g


def is_game_item(genre_ids: List[str], genre_names: List[str]) -> bool:
    """Determine whether an application belongs to Games (Genre ID: 6014 or containing Game/游戏)."""
    if "6014" in genre_ids:
        return True
    for g in genre_names:
        if "game" in g.lower() or "游戏" in g:
            return True
    return False


def fetch_main_chart(region: str, limit: int = 100, exclude_games: bool = True) -> List[Dict[str, Any]]:
    """
    Fetch the Top Free main chart, supporting non-game filtering.
    Prioritizes modern Apple Media Services Feed API, falling back to iTunes RSS.
    """
    url = f"https://rss.applemarketingtools.com/api/v2/{region}/apps/top-free/100/apps.json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results = data.get("feed", {}).get("results", [])
            apps = []
            for item in results:
                genres = [g.get("name", "") for g in item.get("genres", [])]
                genre_ids = [str(g.get("genreId", "")) for g in item.get("genres", [])]

                if exclude_games and is_game_item(genre_ids, genres):
                    continue

                primary_genre = genres[0] if genres else ""
                apps.append({
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "artist": item.get("artistName", ""),
                    "genre": primary_genre,
                    "url": item.get("url", f"https://apps.apple.com/{region}/app/id{item.get('id')}")
                })
                if len(apps) >= limit:
                    break
            return apps
    except Exception as e:
        print(f"[{region.upper()}] Apple Media Services main chart request failed, falling back to iTunes RSS: {e}", file=sys.stderr)

    # Fallback to iTunes RSS
    fallback_url = f"https://itunes.apple.com/{region}/rss/topfreeapplications/limit=100/json"
    fallback_req = urllib.request.Request(fallback_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(fallback_req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            entries = data.get("feed", {}).get("entry", [])
            apps = []
            for e in entries:
                if not isinstance(e, dict):
                    continue
                cat_label = e.get("category", {}).get("attributes", {}).get("label", "")
                cat_id = str(e.get("category", {}).get("attributes", {}).get("im:id", ""))

                if exclude_games and is_game_item([cat_id], [cat_label]):
                    continue

                link_obj = e.get("link")
                if isinstance(link_obj, list) and link_obj:
                    app_url = link_obj[0].get("attributes", {}).get("href", "")
                elif isinstance(link_obj, dict):
                    app_url = link_obj.get("attributes", {}).get("href", "")
                else:
                    app_url = ""

                id_obj = e.get("id")
                if isinstance(id_obj, dict):
                    app_id = id_obj.get("attributes", {}).get("im:id", "")
                else:
                    app_id = str(id_obj or "")

                apps.append({
                    "id": app_id,
                    "name": e.get("im:name", {}).get("label", ""),
                    "artist": e.get("im:artist", {}).get("label", ""),
                    "genre": cat_label,
                    "url": app_url
                })
                if len(apps) >= limit:
                    break
            return apps
    except Exception as ex:
        print(f"[{region.upper()}] iTunes RSS fallback main chart failed: {ex}", file=sys.stderr)

    return []


def fetch_genre_chart(region: str, genre_id: int, genre_name: str, limit: int = 10) -> Dict[str, Any]:
    """
    Fetch top N free apps for a specific category.
    """
    url = f"https://itunes.apple.com/{region}/rss/topfreeapplications/limit={limit}/genre={genre_id}/json"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    apps = []
    max_retries = 2

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                entries = data.get("feed", {}).get("entry", [])
                if isinstance(entries, dict):
                    entries = [entries]

                for e in entries:
                    if not isinstance(e, dict):
                        continue

                    name = e.get("im:name", {}).get("label", "")
                    artist = e.get("im:artist", {}).get("label", "")
                    cat = e.get("category", {}).get("attributes", {}).get("label", "")

                    link_obj = e.get("link")
                    if isinstance(link_obj, list) and link_obj:
                        app_url = link_obj[0].get("attributes", {}).get("href", "")
                    elif isinstance(link_obj, dict):
                        app_url = link_obj.get("attributes", {}).get("href", "")
                    else:
                        app_url = ""

                    id_obj = e.get("id")
                    if isinstance(id_obj, dict):
                        app_id = id_obj.get("attributes", {}).get("im:id", "")
                    else:
                        app_id = str(id_obj or "")

                    apps.append({
                        "id": app_id,
                        "name": name,
                        "artist": artist,
                        "genre": cat,
                        "url": app_url
                    })
                return {
                    "genre_id": genre_id,
                    "genre_name": genre_name,
                    "apps": apps
                }
        except Exception as e:
            if attempt < max_retries:
                time.sleep(0.5 * (attempt + 1))
                continue
            print(f"[{region.upper()}] Failed to fetch category {genre_name} (ID: {genre_id}): {e}", file=sys.stderr)
            return {
                "genre_id": genre_id,
                "genre_name": genre_name,
                "apps": []
            }


def fetch_all_genres_concurrently(
    region: str,
    genres: List[Dict[str, Any]],
    limit: int = 10,
    max_workers: int = 4
) -> List[Dict[str, Any]]:
    """
    Fetch specified category charts concurrently with a lightweight thread pool to minimize Runner execution time.
    """
    results = []
    if not genres:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch_genre_chart, region, g["id"], g["name"], limit): g
            for g in genres
        }
        for future in as_completed(future_map):
            try:
                res = future.result()
                results.append(res)
            except Exception as e:
                g = future_map[future]
                print(f"[{region.upper()}] Exception fetching category {g.get('name')}: {e}", file=sys.stderr)

    genre_order = {g["id"]: idx for idx, g in enumerate(genres)}
    results.sort(key=lambda x: genre_order.get(x["genre_id"], 999))
    return results


def generate_markdown_snapshot(
    region: str,
    main_apps: List[Dict[str, Any]],
    primary_charts: List[Dict[str, Any]],
    secondary_charts: List[Dict[str, Any]],
    snapshot_date_str: str,
    output_dir: str
) -> str:
    """
    Generate clean, standardized daily rankings Markdown snapshot file.
    """
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, f"{snapshot_date_str}_{region.lower()}.md")

    now_utc = datetime.now(timezone.utc)
    cst_time = now_utc + timedelta(hours=8)
    time_str_cst = cst_time.strftime("%Y-%m-%d %H:%M:%S")
    time_str_utc = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    region_name = "China (CN)" if region.lower() == "cn" else "United States (US)" if region.lower() == "us" else region.upper()

    lines = [
        f"# App Store Non-Game App Rankings - {region_name}",
        "",
        f"- **Snapshot Date**: `{snapshot_date_str}`",
        f"- **Generated At**: {time_str_cst} (CST) / {time_str_utc} (UTC)",
        f"- **Data Policy**: Non-game applications (games completely excluded)",
        f"- **Data Sources**: Apple Media Services & iTunes RSS Official Public Feeds",
        "",
        "---",
        "",
        "## Table of Contents",
        f"- [🏆 Top Free Apps Top {len(main_apps)} (Non-Game)](#-top-free-apps-top-{len(main_apps)}-non-game)",
        "- [📂 Primary Categories Top 10](#-primary-categories-top-10)",
    ]

    if secondary_charts:
        lines.append("- [🎮 Secondary Categories Top 5](#-secondary-categories-top-5)")

    lines.extend([
        "",
        "---",
        "",
        f"## 🏆 Top Free Apps Top {len(main_apps)} (Non-Game)",
        "",
        "| Rank | App Name | Developer | Primary Category |",
        "| :---: | :--- | :--- | :--- |"
    ])

    for idx, app in enumerate(main_apps, 1):
        name = app.get("name", "").replace("|", "-")
        artist = app.get("artist", "").replace("|", "-")
        genre = translate_genre_to_english(app.get("genre", ""))
        lines.append(f"| {idx} | **{name}** | {artist} | {genre} |")

    lines.extend([
        "",
        "---",
        "",
        "## 📂 Primary Categories Top 10",
        ""
    ])

    for chart in primary_charts:
        g_name = translate_genre_to_english(chart.get("genre_name", ""))
        apps = chart.get("apps", [])
        lines.append(f"### 📌 {g_name} (Top {len(apps)})")
        lines.append("")
        if not apps:
            lines.append("*No data or empty response from store*")
            lines.append("")
            continue

        lines.append("| Rank | App Name | Developer |")
        lines.append("| :---: | :--- | :--- |")
        for idx, app in enumerate(apps, 1):
            name = app.get("name", "").replace("|", "-")
            artist = app.get("artist", "").replace("|", "-")
            lines.append(f"| {idx} | **{name}** | {artist} |")
        lines.append("")

    if secondary_charts:
        lines.extend([
            "---",
            "",
            "## 🎮 Secondary Categories Top 5",
            ""
        ])
        for chart in secondary_charts:
            g_name = translate_genre_to_english(chart.get("genre_name", ""))
            apps = chart.get("apps", [])
            lines.append(f"### 🔹 {g_name} (Top {len(apps)})")
            lines.append("")
            if not apps:
                lines.append("*No data or empty response from store*")
                lines.append("")
                continue

            lines.append("| Rank | App Name | Developer |")
            lines.append("| :---: | :--- | :--- |")
            for idx, app in enumerate(apps, 1):
                name = app.get("name", "").replace("|", "-")
                artist = app.get("artist", "").replace("|", "-")
                lines.append(f"| {idx} | **{name}** | {artist} |")
            lines.append("")

    with open(file_path, mode="w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return file_path


def prune_historical_rankings(rankings_dir: str, retention_days: int = 180) -> int:
    """
    Clean up historical ranking snapshot files in rankings/ older than retention_days (default 180 days).
    """
    if not os.path.exists(rankings_dir):
        return 0

    now_utc = datetime.now(timezone.utc)
    cutoff_date = (now_utc - timedelta(days=retention_days)).date()

    deleted_count = 0
    date_regex = re.compile(r"^(\d{4}-\d{2}-\d{2})_.*\.md$")

    for fname in os.listdir(rankings_dir):
        m = date_regex.match(fname)
        if m:
            date_str = m.group(1)
            try:
                f_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                if f_date < cutoff_date:
                    fpath = os.path.join(rankings_dir, fname)
                    os.remove(fpath)
                    deleted_count += 1
            except ValueError:
                continue

    return deleted_count


def run_rankings_pipeline(config: Dict[str, Any]) -> None:
    """
    Execute full pipeline for rankings scraping, snapshot generation, and retention pruning.
    """
    rankings_cfg = config.get("rankings", {})
    regions = rankings_cfg.get("regions", ["cn", "us"])
    exclude_games = bool(rankings_cfg.get("exclude_games", True))

    main_limit = int(rankings_cfg.get("main_chart_limit", 100))
    primary_limit = int(rankings_cfg.get("primary_genre_limit", 10))
    secondary_limit = int(rankings_cfg.get("secondary_genre_limit", 5))

    primary_genres = rankings_cfg.get("primary_genres", [])
    secondary_genres = rankings_cfg.get("secondary_genres", [])

    if exclude_games:
        primary_genres = [
            g for g in primary_genres
            if str(g.get("id")) != "6014" and not ("game" in g.get("name", "").lower() or "游戏" in g.get("name", ""))
        ]
        secondary_genres = [
            g for g in secondary_genres
            if str(g.get("id")) not in [str(x) for x in range(7001, 7020)] and not ("game" in g.get("name", "").lower() or "游戏" in g.get("name", ""))
        ]

    retention_cfg = config.get("retention", {})
    rankings_days = int(retention_cfg.get("rankings_days", 180))

    rankings_dir = os.path.join(os.getcwd(), "rankings")
    os.makedirs(rankings_dir, exist_ok=True)

    now_utc = datetime.now(timezone.utc)
    cst_time = now_utc + timedelta(hours=8)
    snapshot_date_str = cst_time.strftime("%Y-%m-%d")

    print(f"\n==================================================")
    print(f"📊 Starting App Store Rankings Snapshot (Date: {snapshot_date_str})")
    print(f"🌍 Monitored regions: {', '.join([r.upper() for r in regions])}")
    print(f"🚫 Game filtering: {'Enabled (Non-game apps only)' if exclude_games else 'Disabled'}")
    print(f"==================================================")

    for region in regions:
        region = region.strip().lower()
        t0 = time.time()
        print(f"\n  [Fetching {region.upper()} rankings] ...")

        # 1. Main chart
        main_apps = fetch_main_chart(region, limit=main_limit, exclude_games=exclude_games)
        print(f"  -> Main chart completed: {len(main_apps)} non-game apps")

        # 2. Primary categories
        primary_charts = fetch_all_genres_concurrently(region, primary_genres, limit=primary_limit, max_workers=4)
        print(f"  -> Primary categories completed: {len(primary_charts)} categories")

        # 3. Secondary categories (if configured)
        secondary_charts = fetch_all_genres_concurrently(region, secondary_genres, limit=secondary_limit, max_workers=4) if secondary_genres else []

        # 4. Generate Markdown snapshot
        out_file = generate_markdown_snapshot(
            region=region,
            main_apps=main_apps,
            primary_charts=primary_charts,
            secondary_charts=secondary_charts,
            snapshot_date_str=snapshot_date_str,
            output_dir=rankings_dir
        )
        elapsed = time.time() - t0
        print(f"  ✅ {region.upper()} snapshot generated: {os.path.relpath(out_file)} (Duration: {elapsed:.2f}s)")

    # 5. Prune old snapshots older than 180 days
    pruned_files = prune_historical_rankings(rankings_dir, retention_days=rankings_days)
    if pruned_files > 0:
        print(f"\n🧹 Automatically pruned {pruned_files} old ranking snapshot files (> {rankings_days} days)")


if __name__ == "__main__":
    test_config = {
        "retention": {"rankings_days": 180},
        "rankings": {
            "regions": ["cn", "us"],
            "exclude_games": True,
            "main_chart_limit": 100,
            "primary_genre_limit": 10,
            "secondary_genre_limit": 5,
            "primary_genres": [
                {"id": 6005, "name": "Social Networking"},
                {"id": 6007, "name": "Productivity"},
                {"id": 6015, "name": "Finance"},
                {"id": 6002, "name": "Utilities"}
            ],
            "secondary_genres": []
        }
    }
    run_rankings_pipeline(test_config)
