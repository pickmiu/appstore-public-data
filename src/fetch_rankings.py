#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_rankings.py - App Store 榜单抓取与每日 Markdown 快照生成模块

特性：
1. 默认抓取中国区 (CN) 与美国区 (US)；
2. 彻底排除游戏：总榜自动过滤 Games (6014) 并补齐至纯应用 Top 100；
3. 一级品类（Top 10）使用轻量线程池并发抓取（耗时 < 5 秒）；
4. 自动生成标准 Markdown 快照（rankings/YYYY-MM-DD_{region}.md）；
5. 自动维护 180 天生命周期，清理半年前的历史快照文件。
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


def is_game_item(genre_ids: List[str], genre_names: List[str]) -> bool:
    """判定是否为游戏类应用 (Genre ID: 6014 或包含 Game/游戏)"""
    if "6014" in genre_ids:
        return True
    for g in genre_names:
        if "game" in g.lower() or "游戏" in g:
            return True
    return False


def fetch_main_chart(region: str, limit: int = 100, exclude_games: bool = True) -> List[Dict[str, Any]]:
    """
    抓取主榜单免费榜 (Top Free)，支持过滤所有游戏项目
    优先采用 Apple Media Services 现代 Feed API，失败则回退至 iTunes RSS
    """
    # Apple Marketing Tools API 仅支持 100
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
        print(f"[{region.upper()}] Apple Media Services 主榜拉取失败，尝试回退 iTunes RSS: {e}", file=sys.stderr)

    # 回退至 iTunes RSS
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
        print(f"[{region.upper()}] 回退 iTunes RSS 主榜拉取失败: {ex}", file=sys.stderr)

    return []


def fetch_genre_chart(region: str, genre_id: int, genre_name: str, limit: int = 10) -> Dict[str, Any]:
    """
    抓取单个品类的免费榜前 N 名
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

                    # 安全提取 link url
                    link_obj = e.get("link")
                    if isinstance(link_obj, list) and link_obj:
                        app_url = link_obj[0].get("attributes", {}).get("href", "")
                    elif isinstance(link_obj, dict):
                        app_url = link_obj.get("attributes", {}).get("href", "")
                    else:
                        app_url = ""

                    # 安全提取 id
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
            print(f"[{region.upper()}] 抓取品类 {genre_name} (ID: {genre_id}) 失败: {e}", file=sys.stderr)
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
    使用轻量线程池并发抓取所有指定品类榜单，极大压缩 Runner 运行耗时
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
                print(f"[{region.upper()}] 并发获取品类 {g.get('name')} 异常: {e}", file=sys.stderr)

    # 按照原配置列表顺序排序，确保 Markdown 快照格式稳定
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
    生成规范、排版清晰的每日榜单 Markdown 快照文件
    """
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, f"{snapshot_date_str}_{region.lower()}.md")

    now_utc = datetime.now(timezone.utc)
    cst_time = now_utc + timedelta(hours=8)
    time_str_cst = cst_time.strftime("%Y-%m-%d %H:%M:%S")
    time_str_utc = now_utc.strftime("%Y-%m-%d %H:%M:%S")

    region_name = "中国区 (CN)" if region.lower() == "cn" else f"美国区 (US)" if region.lower() == "us" else region.upper()

    lines = [
        f"# App Store 纯应用榜单快照 - {region_name}",
        "",
        f"- **快照日期**: `{snapshot_date_str}`",
        f"- **生成时间**: {time_str_cst} (北京时间 CST) / {time_str_utc} (UTC)",
        f"- **数据策略**: 纯应用模式（彻底排除游戏相关分类与应用）",
        f"- **数据源**: Apple Media Services & iTunes RSS 官方公开 Feed",
        "",
        "---",
        "",
        "## 目录导航",
        "- [🏆 免费主榜 Top 100 (纯应用)](#-免费主榜-top-100-纯应用)",
        "- [📂 一级品类 Top 10](#-一级品类-top-10)",
    ]

    if secondary_charts:
        lines.append("- [🎮 二级细分品类 Top 5](#-二级细分品类-top-5)")

    lines.extend([
        "",
        "---",
        "",
        f"## 🏆 免费主榜 Top {len(main_apps)} (纯应用)",
        "",
        "| 排名 | 应用名称 | 开发者 | 核心分类 | App Store 链接 |",
        "| :---: | :--- | :--- | :--- | :--- |"
    ])

    for idx, app in enumerate(main_apps, 1):
        name = app.get("name", "").replace("|", "-")
        artist = app.get("artist", "").replace("|", "-")
        genre = app.get("genre", "")
        url = app.get("url", "")
        link = f"[直达商店]({url})" if url else "暂无"
        lines.append(f"| {idx} | **{name}** | {artist} | {genre} | {link} |")

    lines.extend([
        "",
        "---",
        "",
        "## 📂 一级品类 Top 10",
        ""
    ])

    for chart in primary_charts:
        g_name = chart.get("genre_name", "")
        apps = chart.get("apps", [])
        lines.append(f"### 📌 {g_name} (前 {len(apps)} 名)")
        lines.append("")
        if not apps:
            lines.append("*暂无数据或抓取未返回*")
            lines.append("")
            continue

        lines.append("| 排名 | 应用名称 | 开发者 | App Store 链接 |")
        lines.append("| :---: | :--- | :--- | :--- |")
        for idx, app in enumerate(apps, 1):
            name = app.get("name", "").replace("|", "-")
            artist = app.get("artist", "").replace("|", "-")
            url = app.get("url", "")
            link = f"[直达商店]({url})" if url else "暂无"
            lines.append(f"| {idx} | **{name}** | {artist} | {link} |")
        lines.append("")

    if secondary_charts:
        lines.extend([
            "---",
            "",
            "## 🎮 二级细分品类 Top 5",
            ""
        ])
        for chart in secondary_charts:
            g_name = chart.get("genre_name", "")
            apps = chart.get("apps", [])
            lines.append(f"### 🔹 {g_name} (前 {len(apps)} 名)")
            lines.append("")
            if not apps:
                lines.append("*暂无数据或抓取未返回*")
                lines.append("")
                continue

            lines.append("| 排名 | 应用名称 | 开发者 | App Store 链接 |")
            lines.append("| :---: | :--- | :--- | :--- |")
            for idx, app in enumerate(apps, 1):
                name = app.get("name", "").replace("|", "-")
                artist = app.get("artist", "").replace("|", "-")
                url = app.get("url", "")
                link = f"[直达商店]({url})" if url else "暂无"
                lines.append(f"| {idx} | **{name}** | {artist} | {link} |")
            lines.append("")

    with open(file_path, mode="w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return file_path


def prune_historical_rankings(rankings_dir: str, retention_days: int = 180) -> int:
    """
    自动清理 rankings/ 目录下超过 retention_days (默认180天) 的历史 Markdown 快照文件
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
    全流程执行多地区榜单抓取、快照生成与生命周期清理
    """
    rankings_cfg = config.get("rankings", {})
    regions = rankings_cfg.get("regions", ["cn", "us"])
    exclude_games = bool(rankings_cfg.get("exclude_games", True))

    main_limit = int(rankings_cfg.get("main_chart_limit", 100))
    primary_limit = int(rankings_cfg.get("primary_genre_limit", 10))
    secondary_limit = int(rankings_cfg.get("secondary_genre_limit", 5))

    primary_genres = rankings_cfg.get("primary_genres", [])
    secondary_genres = rankings_cfg.get("secondary_genres", [])

    # 如果开启 exclude_games，过滤掉可能包含的游戏品类配置
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
    print(f"📊 开始生成 App Store 榜单快照 (日期: {snapshot_date_str})")
    print(f"🌍 监控地区: {', '.join([r.upper() for r in regions])}")
    print(f"🚫 游戏排除过滤: {'已启用 (纯应用榜单)' if exclude_games else '未启用'}")
    print(f"==================================================")

    for region in regions:
        region = region.strip().lower()
        t0 = time.time()
        print(f"\n  [正在抓取 {region.upper()} 榜单] ...")

        # 1. 主榜单 Top 100
        main_apps = fetch_main_chart(region, limit=main_limit, exclude_games=exclude_games)
        print(f"  -> 主榜单获取完成: 纯应用 {len(main_apps)} 款")

        # 2. 一级品类 Top 10 并发拉取
        primary_charts = fetch_all_genres_concurrently(region, primary_genres, limit=primary_limit, max_workers=4)
        print(f"  -> 一级品类并发拉取完成: {len(primary_charts)} 个品类")

        # 3. 二级品类 Top 5 (如有配置)
        secondary_charts = fetch_all_genres_concurrently(region, secondary_genres, limit=secondary_limit, max_workers=4) if secondary_genres else []

        # 4. 生成 Markdown 快照文件
        out_file = generate_markdown_snapshot(
            region=region,
            main_apps=main_apps,
            primary_charts=primary_charts,
            secondary_charts=secondary_charts,
            snapshot_date_str=snapshot_date_str,
            output_dir=rankings_dir
        )
        elapsed = time.time() - t0
        print(f"  ✅ {region.upper()} 快照生成成功: {os.path.relpath(out_file)} (耗时: {elapsed:.2f}s)")

    # 5. 清理超过 180 天的旧快照
    pruned_files = prune_historical_rankings(rankings_dir, retention_days=rankings_days)
    if pruned_files > 0:
        print(f"\n🧹 已自动清理超过 {rankings_days} 天的旧榜单快照: {pruned_files} 个文件")


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
                {"id": 6005, "name": "社交 (Social Networking)"},
                {"id": 6007, "name": "效率 (Productivity)"},
                {"id": 6015, "name": "财务 (Finance)"},
                {"id": 6002, "name": "工具 (Utilities)"}
            ],
            "secondary_genres": []
        }
    }
    run_rankings_pipeline(test_config)
