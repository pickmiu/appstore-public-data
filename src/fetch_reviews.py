#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_reviews.py - App Store User Review Multi-channel Scraper and Incremental Ingestion Module

Supports concurrent fetching from Apple official iTunes RSS API and Web landing page SSR sources:
1. Incremental monitoring: latest user reviews (sortBy=mostRecent);
2. Permanent featured pool: landing page featured and most helpful reviews (is_most_helpful=True);
3. Cleaning, deduplication, and 180-day / 10,000-count lifecycle pruning.
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
    Scrape featured and most helpful reviews from App Store official Web endpoints.
    Covers product landing page and see-all reviews endpoints (?see-all=reviews&platform=iphone/web).
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
            print(f"[{country.upper()}|Web] Error scraping featured reviews ({url}): {e}", file=sys.stderr)

    return featured_reviews


def fetch_rss_page_reviews(
    app_id: str,
    app_name: str,
    country: str,
    page: int = 1,
    sort_by: str = "mostRecent",
    max_retries: int = 2
) -> List[Dict[str, Any]]:
    """
    Fetch a single page of reviews from Apple iTunes RSS feed for a specific country (max 50 reviews/page).
    Equipped with exponential backoff retry and URL case tolerance.
    """
    canonical_sort = "mostHelpful" if "helpful" in sort_by.lower() else "mostRecent"
    url_patterns = [
        f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortBy={canonical_sort}/json",
        f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortby={canonical_sort.lower()}/json"
    ]

    for attempt in range(max_retries + 1):
        last_error = None
        for url in url_patterns:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    feed = data.get("feed", {})
                    entries = feed.get("entry", [])
                    if not entries:
                        continue
                    if isinstance(entries, dict):
                        entries = [entries]

                    reviews = []
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        rating_obj = entry.get("im:rating")
                        if not rating_obj:
                            continue  # Skip app metadata entry

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
                last_error = e
            except Exception as e:
                last_error = e

        if attempt < max_retries:
            time.sleep(1.0 * (attempt + 1))
        elif last_error:
            print(f"[{country.upper()}|{sort_by}] Page {page} retries exhausted: {last_error}", file=sys.stderr)

    return []


def check_overflow_risk(
    app_name: str,
    app_id: str,
    added_count: int,
    hit_ceiling: bool,
    threshold: int = 300
) -> Optional[str]:
    """
    Evaluate overflow and missed-review risk:
    When a single polling cycle reaches the 500-review physical API limit (page 10 full with 50 items)
    and valid new additions meet or exceed the warning threshold (default >= 300),
    a risk of missed reviews between polling intervals is detected.
    """
    if hit_ceiling and added_count >= threshold:
        return (
            f"App [{app_name}] (ID: {app_id}) added {added_count} reviews in a single cycle and hit the 500-review ceiling! "
            f"Reviews may have been missed between polling intervals."
        )
    return None


def emit_github_action_warning(title: str, message: str) -> None:
    """
    Emit GitHub Actions official warning annotation and append alert box to job summary.
    1. Output ::warning workflow annotation for yellow warning banner on GitHub Actions run page.
    2. Append alert box to $GITHUB_STEP_SUMMARY.
    """
    print("\n" + "!" * 65, file=sys.stderr)
    print(f"🚨 [Overflow Alert] {title}\n{message}", file=sys.stderr)
    print("!" * 65 + "\n", file=sys.stderr)

    print(f"::warning title={title}::{message}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"\n> [!WARNING]\n> ### ⚠️ {title}\n> {message}\n>\n> *Recommendation: Current polling is set to every 30 minutes. If overflow persists, check for viral surge or major events.*\n\n")
        except Exception as e:
            print(f"[Warning] Failed to write to GITHUB_STEP_SUMMARY: {e}", file=sys.stderr)


def monitor_app_reviews(
    app_id: str,
    app_name: str,
    countries: List[str],
    max_pages_per_country: int = 10,
    return_stats: bool = False
) -> Any:
    """
    Monitor incremental reviews for an app across target countries:
    1. Scrape Web landing page Most Helpful reviews.
    2. Scrape RSS latest reviews (Apple limit: 10 pages, 500 reviews per country).
    3. Overflow detection: track whether page 10 reached full capacity (50 reviews).
    """
    print(f"\n==================================================")
    print(f"🚀 Scraping reviews for: {app_name} (ID: {app_id})")
    print(f"📍 Target countries/regions: {', '.join([c.upper() for c in countries])}")
    print(f"==================================================")

    collected_reviews = []
    overflow_stats = {
        "hit_ceiling": False,
        "ceiling_details": []
    }

    for cc in countries:
        cc_lower = cc.lower()
        # 1. Scrape Web featured reviews
        web_helpful = fetch_web_featured_reviews(app_id, app_name, country=cc_lower)
        if web_helpful:
            print(f"  -> [{cc.upper()}] Successfully fetched Web featured reviews: {len(web_helpful)} items")
            collected_reviews.extend(web_helpful)

        # 2. Scrape RSS reviews (mostRecent and mostHelpful)
        for sort_mode in ("mostRecent", "mostHelpful"):
            page_counts = []
            for p in range(1, max_pages_per_country + 1):
                page_data = fetch_rss_page_reviews(app_id, app_name, country=cc_lower, page=p, sort_by=sort_mode)
                if not page_data:
                    break
                page_counts.append(f"P{p}({len(page_data)})")
                if sort_mode.lower() == "mosthelpful":
                    for item in page_data:
                        item["is_most_helpful"] = True
                collected_reviews.extend(page_data)
                time.sleep(0.2)

                # Ceiling detection
                if p == max_pages_per_country and len(page_data) >= 50:
                    overflow_stats["hit_ceiling"] = True
                    overflow_stats["ceiling_details"].append(f"{cc.upper()}|RSS {sort_mode} reached page {p} ceiling ({len(page_data)} items)")

            status_str = " ".join(page_counts) if page_counts else "No new data"
            print(f"  -> [{cc.upper()}|RSS {sort_mode}]: {status_str}")

    print(f"  ✅ Total candidate reviews collected: {len(collected_reviews)}")
    if return_stats:
        return collected_reviews, overflow_stats
    return collected_reviews


def get_review_filename(app_id: str, app_name: str, countries: Optional[List[str]] = None) -> str:
    """
    Generate standardized review CSV filename: reviews_{app_id}_{safe_name}_{region}.csv
    Example: reviews_6448311069_ChatGPT_us.csv
    """
    safe_name = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", app_name).strip("_") if app_name else app_id
    region_str = "_".join([c.strip().lower() for c in countries]) if countries else "all"
    return f"reviews_{app_id}_{safe_name}_{region_str}.csv"


def build_reviews_commit_message(stats: List[Dict[str, Any]]) -> str:
    """
    Generate clean, concise, traceable Git commit message (in English except for app names):
    1. Sorted descending by number of added reviews.
    2. Concise title (<= 72 chars), folding excess apps (e.g. +110 ChatGPT, +78 DeepSeek (+8 more) [skip ci]).
    3. Body listing full breakdown of updated apps.
    4. If only lifecycle pruning occurred, outputs pruning summary.
    """
    added_apps = [s for s in stats if s.get("added", 0) > 0]
    added_apps.sort(key=lambda x: x["added"], reverse=True)

    if not added_apps:
        pruned_apps = [s for s in stats if s.get("pruned", 0) > 0]
        pruned_apps.sort(key=lambda x: x["pruned"], reverse=True)
        if pruned_apps:
            p_items = [f"-{p['pruned']} {p['name']}" for p in pruned_apps[:2]]
            remaining = len(pruned_apps) - len(p_items)
            suffix = f" and {remaining} more" if remaining > 0 else ""
            title = f"chore(data): prune expired reviews ({', '.join(p_items)}{suffix}) [skip ci]"
            body_lines = ["Review retention cleanup:"]
            for p in pruned_apps:
                body_lines.append(f"- {p['name']}: pruned {p['pruned']} expired review(s)")
            return f"{title}\n\n" + "\n".join(body_lines)
        return "chore(data): auto-update App Store reviews [skip ci]"

    total_apps = len(added_apps)
    shown = []

    for app in added_apps:
        item = f"+{app['added']} {app['name']}"
        test_shown = shown + [item]
        remaining = total_apps - len(test_shown)
        suffix = f" (+{remaining} more) [skip ci]" if remaining > 0 else " [skip ci]"
        test_title = f"chore(data): {', '.join(test_shown)}{suffix}"

        # Stop adding if exceeding 3 apps or title length > 72 chars
        if len(test_shown) > 3 or (len(test_title) > 72 and len(shown) >= 1):
            break
        shown.append(item)

    remaining = total_apps - len(shown)
    if remaining > 0:
        title = f"chore(data): {', '.join(shown)} (+{remaining} more) [skip ci]"
    else:
        title = f"chore(data): {', '.join(shown)} [skip ci]"

    body_lines = ["Review update summary (sorted by new reviews):"]
    for app in added_apps:
        body_lines.append(f"- {app['name']}: +{app['added']}")

    return f"{title}\n\n" + "\n".join(body_lines)


def run_reviews_pipeline(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Execute full pipeline for review monitoring, ingestion, and lifecycle pruning based on config.
    """
    retention_cfg = config.get("retention", {})
    retention_days = int(retention_cfg.get("reviews_days", 180))
    max_count = int(retention_cfg.get("reviews_max_count", 10000))
    keep_all_helpful = bool(retention_cfg.get("keep_all_helpful", True))

    monitoring_cfg = config.get("monitoring", {})
    overflow_threshold = int(monitoring_cfg.get("overflow_alert_threshold", 300))

    monitored_apps = config.get("monitored_apps", [])
    if not monitored_apps:
        print("⚠️ No monitored applications configured. Skipping review pipeline.")
        return []

    data_dir = os.path.join(os.getcwd(), "data")
    os.makedirs(data_dir, exist_ok=True)

    pipeline_stats = []

    for app in monitored_apps:
        app_id = str(app.get("id", "")).strip()
        app_name = str(app.get("name", app_id)).strip()
        countries = app.get("countries", ["us", "cn"])

        if not app_id:
            continue

        filename = get_review_filename(app_id, app_name, countries)
        output_csv = os.path.join(data_dir, filename)

        # Legacy filename migration
        legacy_csv = os.path.join(data_dir, f"reviews_{app_id}.csv")
        if os.path.exists(legacy_csv) and not os.path.exists(output_csv):
            os.rename(legacy_csv, output_csv)

        # 1. Fetch multi-channel reviews and overflow stats
        raw_reviews, overflow_stats = monitor_app_reviews(app_id, app_name, countries, return_stats=True)

        # 2. Incremental save with fingerprint deduplication
        report = save_reviews_to_csv(raw_reviews, output_csv, default_source="itunes_rss")

        # 3. Check overflow and missed reviews risk
        warn_msg = check_overflow_risk(
            app_name=app_name,
            app_id=app_id,
            added_count=report["added_count"],
            hit_ceiling=overflow_stats.get("hit_ceiling", False),
            threshold=overflow_threshold
        )
        if warn_msg:
            details = "; ".join(overflow_stats.get("ceiling_details", []))
            full_msg = f"{warn_msg} (Ceiling details: {details})"
            emit_github_action_warning("App Store Review Overflow Alert (Missed Risk)", full_msg)

        # 4. Data lifecycle pruning (180 days / 10k ceiling / mostHelpful protection)
        prune_report = prune_reviews_data(
            output_csv,
            retention_days=retention_days,
            max_count=max_count,
            keep_all_helpful=keep_all_helpful
        )

        pipeline_stats.append({
            "name": app_name,
            "added": report.get("added_count", 0),
            "pruned": prune_report.get("pruned_count", 0)
        })

        print("\n" + "-" * 50)
        print(f"📊 {app_name} Data Report")
        print("-" * 50)
        print(f"📁 Target file: data/{filename}")
        print(f"📥 Candidate reviews: {report['input_count']}")
        print(f"✨ Valid added: {report['added_count']}")
        print(f"⏭️ Fingerprint deduplicated: {report['skipped_count']}")
        print(f"✂️ Lifecycle pruned: Removed {prune_report.get('pruned_count', 0)} expired reviews")
        print(f"📦 Total retained: {prune_report.get('total_after', 0)} (Featured protected: {prune_report.get('helpful_retained', 0)})")
        if overflow_stats.get("hit_ceiling"):
            print(f"⚠️ Overflow status: Hit 500 ceiling ({'; '.join(overflow_stats['ceiling_details'])})")
        print("-" * 50)

    # Generate commit message and save to .commit_msg for CI
    commit_msg = build_reviews_commit_message(pipeline_stats)
    commit_msg_path = os.path.join(os.getcwd(), ".commit_msg")
    try:
        with open(commit_msg_path, "w", encoding="utf-8") as f:
            f.write(commit_msg)
        print(f"\n📝 Generated Git commit message: {commit_msg.splitlines()[0]}")
    except Exception as e:
        print(f"⚠️ Failed to write .commit_msg: {e}", file=sys.stderr)

    return pipeline_stats


if __name__ == "__main__":
    test_config = {
        "retention": {"reviews_days": 180, "reviews_max_count": 10000, "keep_all_helpful": True},
        "monitored_apps": [
            {"id": "6670324846", "name": "Grok AI", "countries": ["us", "cn"]},
            {"id": "6760173601", "name": "Muse from Meta", "countries": ["us", "cn"]}
        ]
    }
    run_reviews_pipeline(test_config)
