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
            print(f"[{country.upper()}|Web] 拉取/解析落地页精选评价异常 ({url}): {e}", file=sys.stderr)

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
    通过 Apple iTunes RSS 抓取指定国家的一页评价 (单页最多 50 条)
    带有自动指数退避重试 (Backoff Retry) 与大小写参数自动容错
    """
    canonical_sort = "mostHelpful" if "helpful" in sort_by.lower() else "mostRecent"
    # 支持 camelCase (sortBy=mostRecent) 与全小写 (sortby=mostrecent) 兜底
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
                last_error = e
            except Exception as e:
                last_error = e

        # 若当轮尝试所有 URL 模式均未获取成功，且未到最大重试次数，执行单次退避休眠
        if attempt < max_retries:
            time.sleep(1.0 * (attempt + 1))
        elif last_error:
            print(f"[{country.upper()}|{sort_by}] 第 {page} 页请求重试耗尽: {last_error}", file=sys.stderr)

    return []


def check_overflow_risk(
    app_name: str,
    app_id: str,
    added_count: int,
    hit_ceiling: bool,
    threshold: int = 300
) -> Optional[str]:
    """
    满载与漏抓风险判定：
    当单次轮询达到接口 500 条物理上限（第 10 页满载 50 条），且有效新增入库量达到预警阈值（默认 >= 300 条），
    判定存在评论在两次轮询间被挤出窗口的漏抓风险。
    """
    if hit_ceiling and added_count >= threshold:
        return (
            f"应用 [{app_name}] (ID: {app_id}) 单次新增评价达 {added_count} 条且触及 500 条物理上限！"
            f"在两次轮询间隔内极可能存在新评论被挤出窗口的漏抓风险，建议关注！"
        )
    return None


def emit_github_action_warning(title: str, message: str) -> None:
    """
    输出 GitHub Actions 官方高亮告警并在 Job 汇总生成 Markdown 预警
    1. 通过 ::warning 工作流注解在 GitHub Actions 页面直接生成黄色/高优先级警告横幅
    2. 追加到 $GITHUB_STEP_SUMMARY 渲染 GitHub 原生 Alert 呼出框
    """
    # 1. 终端与控制台显式警报
    print("\n" + "!" * 65, file=sys.stderr)
    print(f"🚨 [满载告警] {title}\n{message}", file=sys.stderr)
    print("!" * 65 + "\n", file=sys.stderr)

    # 2. GitHub Actions Annotation 语法
    print(f"::warning title={title}::{message}")

    # 3. GitHub Actions Step Summary 渲染
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"\n> [!WARNING]\n> ### ⚠️ {title}\n> {message}\n>\n> *建议：当前调度已设定为 30 分钟轮询。若持续满载，请人工核查是否出现全网舆情暴涨或爆款出圈。*\n\n")
        except Exception as e:
            print(f"[警告] 写入 GITHUB_STEP_SUMMARY 失败: {e}", file=sys.stderr)


def monitor_app_reviews(
    app_id: str,
    app_name: str,
    countries: List[str],
    max_pages_per_country: int = 10,
    return_stats: bool = False
) -> Any:
    """
    对指定应用的所有目标国家进行增量评价监控：
    1. 抓取 Web 落地页最有帮助 (Most Helpful) 评价 (通常为置顶 8 条)
    2. 抓取 RSS 最新评价 (mostrecent，Apple 单个国家公开接口上限 10 页共 500 条)
    3. 满载与溢出检测：记录第 10 页是否满载 (50条)
    """
    print(f"\n==================================================")
    print(f"🚀 开始抓取应用评价: {app_name} (ID: {app_id})")
    print(f"📍 目标国家/地区: {', '.join([c.upper() for c in countries])}")
    print(f"==================================================")

    collected_reviews = []
    overflow_stats = {
        "hit_ceiling": False,
        "ceiling_details": []
    }

    for cc in countries:
        cc_lower = cc.lower()
        # 1. 抓取 Web 精选高赞 (最有帮助)
        web_helpful = fetch_web_featured_reviews(app_id, app_name, country=cc_lower)
        if web_helpful:
            print(f"  -> [{cc.upper()}] 成功拉取 Web 落地页高赞评价: {len(web_helpful)} 条")
            collected_reviews.extend(web_helpful)

        # 2. 抓取 RSS 评价 (同时覆盖 mostrecent 与 mosthelpful 双维度，单排序最多 10 页 500 条)
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

                # 满载检测：如果抓到了第 10 页且第 10 页达到满页 (50 条)，说明触及苹果单次 500 条物理上限
                if p == max_pages_per_country and len(page_data) >= 50:
                    overflow_stats["hit_ceiling"] = True
                    overflow_stats["ceiling_details"].append(f"{cc.upper()}|RSS {sort_mode} 达第 {p} 页满载({len(page_data)}条)")

            status_str = " ".join(page_counts) if page_counts else "无新增数据"
            print(f"  -> [{cc.upper()}|RSS {sort_mode}]: {status_str}")

    print(f"  ✅ 本次抓取候选总量: {len(collected_reviews)} 条")
    if return_stats:
        return collected_reviews, overflow_stats
    return collected_reviews


def get_review_filename(app_id: str, app_name: str, countries: Optional[List[str]] = None) -> str:
    """
    生成规范的评价 CSV 文件名，格式：reviews_{app_id}_{app_name}_{region}.csv
    例如：reviews_6448311069_ChatGPT_us.csv
    """
    safe_name = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", app_name).strip("_") if app_name else app_id
    region_str = "_".join([c.strip().lower() for c in countries]) if countries else "all"
    return f"reviews_{app_id}_{safe_name}_{region_str}.csv"


def build_reviews_commit_message(stats: List[Dict[str, Any]]) -> str:
    """
    生成规范、简洁且可追溯的 Git Commit Message（除应用名外全英文）：
    1. 按新增评价数量降序排列；
    2. 标题简洁（控制在 72 字符以内），若应用过多自动折叠省略（如：+110 ChatGPT, +78 DeepSeek (+8 more) [skip ci]）；
    3. 正文列出所有变动应用的完整更新详情；
    4. 若仅有生命周期裁剪（新增为0），生成清理过期评价摘要。
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

        # 超过 3 个应用或标题长度超过 72 字符时停止追加到标题
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
    根据配置全流程执行应用评价监控、入库与生命周期裁剪，并触发满载告警
    """
    retention_cfg = config.get("retention", {})
    retention_days = int(retention_cfg.get("reviews_days", 180))
    max_count = int(retention_cfg.get("reviews_max_count", 10000))
    keep_all_helpful = bool(retention_cfg.get("keep_all_helpful", True))

    monitoring_cfg = config.get("monitoring", {})
    overflow_threshold = int(monitoring_cfg.get("overflow_alert_threshold", 300))

    monitored_apps = config.get("monitored_apps", [])
    if not monitored_apps:
        print("⚠️ 未配置监控应用，跳过评价监控流程。")
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

        # 兼容性平滑迁移：若存在旧版命名 reviews_{app_id}.csv 且新文件尚不存在，自动重命名继承历史数据
        legacy_csv = os.path.join(data_dir, f"reviews_{app_id}.csv")
        if os.path.exists(legacy_csv) and not os.path.exists(output_csv):
            os.rename(legacy_csv, output_csv)

        # 1. 抓取多渠道评价候选与满载统计
        raw_reviews, overflow_stats = monitor_app_reviews(app_id, app_name, countries, return_stats=True)

        # 2. 增量追加入库并指纹去重
        report = save_reviews_to_csv(raw_reviews, output_csv, default_source="itunes_rss")

        # 3. 满载与漏抓预警检测 (Git Action 告警)
        warn_msg = check_overflow_risk(
            app_name=app_name,
            app_id=app_id,
            added_count=report["added_count"],
            hit_ceiling=overflow_stats.get("hit_ceiling", False),
            threshold=overflow_threshold
        )
        if warn_msg:
            details = "; ".join(overflow_stats.get("ceiling_details", []))
            full_msg = f"{warn_msg} (满载详情: {details})"
            emit_github_action_warning("App Store 评价满载预警 (漏抓风险)", full_msg)

        # 4. 执行数据生命周期裁剪 (180天保留 / 1w上限 / mostHelpful永久保护)
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
        print(f"📊 {app_name} 数据统计报告")
        print("-" * 50)
        print(f"📁 目标存储文件: data/{filename}")
        print(f"📥 本次抓取候选: {report['input_count']} 条")
        print(f"✨ 增量有效入库: {report['added_count']} 条")
        print(f"⏭️ 自动指纹去重: {report['skipped_count']} 条")
        print(f"✂️ 生命周期裁剪: 剔除 {prune_report.get('pruned_count', 0)} 条过期记录")
        print(f"📦 最终沉淀总量: {prune_report.get('total_after', 0)} 条 (高赞保护: {prune_report.get('helpful_retained', 0)} 条)")
        if overflow_stats.get("hit_ceiling"):
            print(f"⚠️ 满载监控状态: 触发 500 条上限 ({'; '.join(overflow_stats['ceiling_details'])})")
        print("-" * 50)

    # 生成规范的 commit message 并写入 .commit_msg 供 CI 自动化提交
    commit_msg = build_reviews_commit_message(pipeline_stats)
    commit_msg_path = os.path.join(os.getcwd(), ".commit_msg")
    try:
        with open(commit_msg_path, "w", encoding="utf-8") as f:
            f.write(commit_msg)
        print(f"\n📝 已生成 Git 提交信息: {commit_msg.splitlines()[0]}")
    except Exception as e:
        print(f"⚠️ 写入 .commit_msg 失败: {e}", file=sys.stderr)

    return pipeline_stats


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
