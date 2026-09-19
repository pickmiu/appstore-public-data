#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_reviews.py - App Store 用户评价多渠道抓取与增量入库模块

支持从 Apple 官方 iTunes RSS API 与 Web 落地页 SSR 数据源并发拉取：
1. 增量监控：最新用户评价 (sortBy=mostRecent)；
2. 永久精选：落地页精选高赞与最有帮助评价 (is_most_helpful=True)；
3. 清洗去重入库与 180 天/1w条生命周期裁剪维护。
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from src.pipeline import save_reviews_to_csv, prune_reviews_data

REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def fetch_web_featured_reviews(app_id: str, app_name: str, country: str = "us") -> List[Dict[str, Any]]:
    """
    从 App Store 官方 Web 端多维度抓取精选评价 (Most Helpful / Featured Reviews)
    覆盖落地页及查看全部评价接口 (?see-all=reviews&platform=iphone/web)
    """
    target_urls = [
        f"https://apps.apple.com/{country}/app/id{app_id}",
        f"https://apps.apple.com/{country}/app/{app_id}?see-all=reviews&platform=iphone",
        f"https://apps.apple.com/{country}/app/{app_id}?see-all=reviews&platform=web"
    ]
    
    seen_review_ids = set()
    featured_reviews = []

    for url in target_urls:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                html = resp.read().decode("utf-8")
                scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)
                for s in scripts:
                    if "allProductReviews" in s:
                        data = json.loads(s)
                        data_items = data.get("data", [{}])
                        for d_block in data_items:
                            items = d_block.get("data", {}).get("shelfMapping", {}).get("allProductReviews", {}).get("items", [])
                            for item in items:
                                rev = item.get("review", {})
                                if not rev:
                                    continue
                                
                                review_id = str(rev.get("id") or rev.get("targetReviewId", ""))
                                if not review_id or review_id in seen_review_ids:
                                    continue
                                
                                seen_review_ids.add(review_id)
                                title = str(rev.get("title", ""))
                                content = str(rev.get("contents") or rev.get("body") or rev.get("text", ""))
                                author = str(rev.get("reviewerName", "Anonymous"))
                                rating = rev.get("rating", 5)
                                review_date = rev.get("date", "")
                                
                                featured_reviews.append({
                                    "review_id": review_id,
                                    "app_id": str(app_id),
                                    "app_name": app_name,
                                    "country": country,
                                    "rating": rating,
                                    "title": title,
                                    "content": content,
                                    "author": author,
                                    "version": "",
                                    "review_date": review_date,
                                    "is_most_helpful": True,
                                    "source": "web_ssr_helpful"
                                })
                        break
        except Exception as e:
            # 忽略非核心视图报错
            pass

    return featured_reviews


def fetch_rss_page_reviews(
    app_id: str,
    app_name: str,
    country: str,
    page: int = 1,
    sort_by: str = "mostrecent",
    max_retries: int = 2
) -> List[Dict[str, Any]]:
    """
    通过 Apple iTunes RSS 抓取指定国家的一页最新评价
    带有自动指数退避重试 (Backoff Retry)
    注：Apple 服务端严格区分大小写，必须使用全小写 `sortby=mostrecent`，且必须显式包含 `page={page}`
    """
    sort_param = sort_by.lower()
    url = f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby={sort_param}/json"

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                feed = data.get("feed", {})
                entries = feed.get("entry", [])
                if not entries:
                    return []
                if isinstance(entries, dict):
                    entries = [entries]

                reviews = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    rating_obj = entry.get("im:rating")
                    if not rating_obj:
                        continue  # 过滤 App 自身元数据

                    review_id = entry.get("id", {}).get("label", "")
                    title = entry.get("title", {}).get("label", "")
                    content = entry.get("content", {}).get("label", "")
                    rating = rating_obj.get("label", "5")
                    version = entry.get("im:version", {}).get("label", "")
                    author = entry.get("author", {}).get("name", {}).get("label", "Anonymous")
                    updated = entry.get("updated", {}).get("label", "")

                    reviews.append({
                        "review_id": review_id,
                        "app_id": str(app_id),
                        "app_name": app_name,
                        "country": country,
                        "rating": rating,
                        "title": title,
                        "content": content,
                        "author": author,
                        "version": version,
                        "review_date": updated,
                        "is_most_helpful": False,
                        "source": f"itunes_rss_{sort_by}"
                    })
                return reviews
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return []
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            print(f"[{country.upper()}|{sort_by}] 第 {page} 页 HTTP 错误: {e.code}", file=sys.stderr)
            return []
        except Exception as e:
            if attempt < max_retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            return []
    return []


def monitor_app_reviews(
    app_id: str,
    app_name: str,
    countries: List[str],
    max_pages_per_country: int = 10
) -> List[Dict[str, Any]]:
    """
    对指定应用的所有目标国家进行增量评价监控：
    1. 抓取 Web 落地页最有帮助 (Most Helpful) 评价 (通常为置顶 8 条)
    2. 抓取 RSS 最新评价 (mostrecent，Apple 单个国家公开接口上限 10 页共 500 条)
    """
    print(f"\n==================================================")
    print(f"🚀 开始抓取应用评价: {app_name} (ID: {app_id})")
    print(f"📍 目标国家/地区: {', '.join([c.upper() for c in countries])}")
    print(f"==================================================")

    collected_reviews = []

    for cc in countries:
        cc_lower = cc.lower()
        # 1. 抓取 Web 精选高赞 (最有帮助)
        web_helpful = fetch_web_featured_reviews(app_id, app_name, country=cc_lower)
        if web_helpful:
            print(f"  -> [{cc.upper()}] 成功拉取 Web 落地页高赞评价: {len(web_helpful)} 条")
            collected_reviews.extend(web_helpful)

        # 2. 抓取 RSS 评价 (同时覆盖 mostrecent 与 mosthelpful 双维度，单排序最多 10 页 500 条)
        for sort_mode in ("mostrecent", "mosthelpful"):
            page_counts = []
            for p in range(1, max_pages_per_country + 1):
                page_data = fetch_rss_page_reviews(app_id, app_name, country=cc_lower, page=p, sort_by=sort_mode)
                if not page_data:
                    break
                page_counts.append(f"P{p}({len(page_data)})")
                if sort_mode == "mosthelpful":
                    for item in page_data:
                        item["is_most_helpful"] = True
                collected_reviews.extend(page_data)
                time.sleep(0.2)

            status_str = " ".join(page_counts) if page_counts else "无新增数据"
            print(f"  -> [{cc.upper()}|RSS {sort_mode}]: {status_str}")

    print(f"  ✅ 本次抓取候选总量: {len(collected_reviews)} 条")
    return collected_reviews


def run_reviews_pipeline(config: Dict[str, Any]) -> None:
    """
    根据配置全流程执行应用评价监控、入库与生命周期裁剪
    """
    retention_cfg = config.get("retention", {})
    retention_days = int(retention_cfg.get("reviews_days", 180))
    max_count = int(retention_cfg.get("reviews_max_count", 10000))
    keep_all_helpful = bool(retention_cfg.get("keep_all_helpful", True))

    monitored_apps = config.get("monitored_apps", [])
    if not monitored_apps:
        print("[提示] config.yaml 中未配置 monitored_apps，跳过评价抓取。")
        return

    data_dir = os.path.join(os.getcwd(), "data")
    os.makedirs(data_dir, exist_ok=True)

    for app in monitored_apps:
        app_id = str(app.get("id", "")).strip()
        app_name = str(app.get("name", app_id)).strip()
        countries = app.get("countries", ["us", "cn"])

        if not app_id:
            continue

        output_csv = os.path.join(data_dir, f"reviews_{app_id}.csv")

        # 1. 抓取多渠道评价候选
        raw_reviews = monitor_app_reviews(app_id, app_name, countries)

        # 2. 增量追加入库并指纹去重
        report = save_reviews_to_csv(raw_reviews, output_csv, default_source="itunes_rss")

        # 3. 执行数据生命周期裁剪 (180天保留 / 1w上限 / mostHelpful永久保护)
        prune_report = prune_reviews_data(
            output_csv,
            retention_days=retention_days,
            max_count=max_count,
            keep_all_helpful=keep_all_helpful
        )

        print("\n" + "-" * 50)
        print(f"📊 {app_name} 数据统计报告")
        print("-" * 50)
        print(f"📁 目标存储文件: data/reviews_{app_id}.csv")
        print(f"📥 本次抓取候选: {report['input_count']} 条")
        print(f"✨ 增量有效入库: {report['added_count']} 条")
        print(f"⏭️ 自动指纹去重: {report['skipped_count']} 条")
        print(f"✂️ 生命周期裁剪: 剔除 {prune_report.get('pruned_count', 0)} 条过期记录")
        print(f"📦 最终沉淀总量: {prune_report.get('total_after', 0)} 条 (高赞保护: {prune_report.get('helpful_retained', 0)} 条)")
        print("-" * 50)


if __name__ == "__main__":
    # 支持单独直接运行测试
    test_config = {
        "retention": {"reviews_days": 180, "reviews_max_count": 10000, "keep_all_helpful": True},
        "monitored_apps": [
            {"id": "6670324846", "name": "Grok AI", "countries": ["us", "cn"]},
            {"id": "6760173601", "name": "Muse from Meta", "countries": ["us", "cn"]}
        ]
    }
    run_reviews_pipeline(test_config)
