#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py - 数据清洗、指纹生成、去重存储与生命周期管理核心模块

遵循大道至简与零冗余依赖原则，通过 SHA256 内容指纹保障增量评价绝不重复，
并严格执行半年（180天）保留、1w条上限及“最有帮助（Most Helpful）”永久留存规则。
"""

import os
import re
import csv
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Set, Tuple

# 统一 CSV 表头规范
CSV_COLUMNS = [
    "fingerprint",       # 核心主键：SHA-256 (app_id + author + title + content + date_day)
    "review_id",         # 官方 App Store 评价唯一 ID（如有）
    "app_id",            # 应用 ID
    "app_name",          # 应用名称
    "country",           # 商店地区代码 (如 us, cn)
    "rating",            # 评分 (1-5 整数)
    "title",             # 清洗后的标题
    "content",           # 清洗后的正文
    "original_content",  # 原始正文（备份未过滤原貌，便于追溯）
    "author",            # 评论者用户名
    "version",           # 评价对应的 App 版本号
    "review_date",       # 标准 ISO-8601 UTC 时间 (YYYY-MM-DDTHH:MM:SSZ)
    "is_most_helpful",   # 是否为官方/落地页推荐的高赞或最有帮助评价 (永久保护)
    "source",            # 数据源标识 (itunes_rss / web_ssr / manual)
    "is_short",          # 质量标记：是否为极短无意义内容
    "is_spam"            # 质量标记：是否疑似广告/刷榜引流
]

# 垃圾/引流评论特征正则 (网址、短链、微信号、手机号等)
SPAM_PATTERNS = re.compile(
    r"(https?://|www\.|t\.me/|bit\.ly/|weixin|vx:|微信|\+?\d{7,15})",
    re.IGNORECASE
)

# 仅含标点和空白正则
PUNCT_ONLY_PATTERN = re.compile(r"^[\s\W_]+$")


def clean_text(text: Any) -> str:
    """
    文本清洗：
    1. 移除 ASCII 不见控制字符 (保留换行 \n 和制表符 \t)
    2. 统一换行符为 Unix 格式 (\n)
    3. 压缩多余连续换行 (最多保留 2 个)
    4. 清除每行首尾多余空格
    """
    if text is None:
        return ""
    s = str(text)
    # 移除 ASCII 控制字符
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    # 统一换行符
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # 折叠 3 个及以上换行为 2 个
    s = re.sub(r"\n{3,}", "\n\n", s)
    # 去除每行首尾空格
    lines = [line.strip() for line in s.split("\n")]
    return "\n".join(lines).strip()


def parse_standard_datetime(date_val: Any) -> str:
    """将各类格式的时间字符串转换为标准 ISO-8601 UTC 字符串 (YYYY-MM-DDTHH:MM:SSZ)"""
    if not date_val:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    
    s = str(date_val).strip()
    # 尝试解析常见时间格式
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d"
    ):
        try:
            clean_s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)
            dt = datetime.strptime(clean_s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
            
    return s


def compute_fingerprint(app_id: str, author: str, title: str, content: str, date_iso: str) -> str:
    """
    计算全局内容指纹 (SHA-256)
    
    提取 app_id + author + title + content + date_day 进行哈希。
    使用日期前 10 位（YYYY-MM-DD）可消除毫秒级抓取偏差，只要同一天同一用户发布相同内容即判定唯一。
    """
    date_day = date_iso[:10] if len(date_iso) >= 10 else date_iso
    raw_str = f"{app_id}|{author.strip()}|{title.strip()}|{content.strip()}|{date_day}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


def clean_review_record(raw: Dict[str, Any], default_source: str = "itunes_rss") -> Dict[str, Any]:
    """
    标准化单条评价字典，执行字段对齐、质量打标与指纹生成
    """
    app_id = str(raw.get("app_id", "")).strip()
    app_name = str(raw.get("app_name", "")).strip()
    country = str(raw.get("country", "")).strip().lower()
    review_id = str(raw.get("review_id", "")).strip()
    author = str(raw.get("author", "Anonymous")).strip()
    version = str(raw.get("version", "")).strip()
    source = str(raw.get("source", default_source)).strip()

    # 评分边界规范化 (1-5 整数)
    try:
        rating = int(raw.get("rating", 5))
        rating = max(1, min(5, rating))
    except (ValueError, TypeError):
        rating = 5

    # 文本清洗
    orig_content = str(raw.get("content", ""))
    cleaned_title = clean_text(raw.get("title", ""))
    cleaned_content = clean_text(orig_content)

    # 时间标准化
    date_iso = parse_standard_datetime(raw.get("review_date") or raw.get("updated") or raw.get("date"))

    # 最有帮助标记
    is_most_helpful = bool(raw.get("is_most_helpful", False) or "helpful" in source.lower())

    # 质量打标
    # 中文若 <= 1 个字符，或非中文 <= 2 个字符，或全标点空白，标记为短评
    has_cjk = bool(re.search(r"[\u4e00-\u9fa5]", cleaned_content))
    min_len = 1 if has_cjk else 2
    is_short = len(cleaned_content) <= min_len or bool(PUNCT_ONLY_PATTERN.match(cleaned_content))
    is_spam = bool(SPAM_PATTERNS.search(cleaned_content) or SPAM_PATTERNS.search(cleaned_title))

    # 指纹生成
    fingerprint = raw.get("fingerprint")
    if not fingerprint:
        fingerprint = compute_fingerprint(app_id, author, cleaned_title, cleaned_content, date_iso)

    return {
        "fingerprint": fingerprint,
        "review_id": review_id,
        "app_id": app_id,
        "app_name": app_name,
        "country": country,
        "rating": rating,
        "title": cleaned_title,
        "content": cleaned_content,
        "original_content": orig_content,
        "author": author,
        "version": version,
        "review_date": date_iso,
        "is_most_helpful": is_most_helpful,
        "source": source,
        "is_short": is_short,
        "is_spam": is_spam
    }


def load_existing_fingerprints(csv_path: str) -> Set[str]:
    """快速读取已存在 CSV 文件的 fingerprint 集合（O(1) 查重）"""
    if not os.path.exists(csv_path):
        return set()

    fingerprints = set()
    try:
        with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            if not headers:
                return set()
            try:
                fp_idx = headers.index("fingerprint")
            except ValueError:
                fp_idx = 0
            for row in reader:
                if row and len(row) > fp_idx:
                    fp = row[fp_idx].strip()
                    if fp:
                        fingerprints.add(fp)
    except Exception as e:
        print(f"[警告] 读取 CSV 指纹集合异常: {e}")
    return fingerprints


def save_reviews_to_csv(
    raw_reviews: List[Dict[str, Any]],
    output_path: str,
    default_source: str = "itunes_rss"
) -> Dict[str, Any]:
    """
    统一增量保存入口：清洗 -> 指纹去重 -> 增量追加写入 CSV

    :param raw_reviews: 待保存的评价原始数据
    :param output_path: 输出 CSV 路径
    :param default_source: 默认数据来源标识
    :return: 统计报告
    """
    dir_name = os.path.dirname(output_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    existing_fps = load_existing_fingerprints(output_path)
    file_exists = os.path.exists(output_path) and os.path.getsize(output_path) > 0

    new_records = []
    skipped_count = 0
    rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

    for raw in raw_reviews:
        cleaned = clean_review_record(raw, default_source=default_source)
        fp = cleaned["fingerprint"]
        if fp in existing_fps:
            skipped_count += 1
            continue
            
        existing_fps.add(fp)
        new_records.append(cleaned)
        rating_counts[cleaned["rating"]] = rating_counts.get(cleaned["rating"], 0) + 1

    # 追加写入 CSV (UTF-8 with BOM)
    if new_records:
        write_mode = "a" if file_exists else "w"
        with open(output_path, mode=write_mode, encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if not file_exists:
                writer.writeheader()
            for record in new_records:
                writer.writerow(record)

    return {
        "output_path": output_path,
        "input_count": len(raw_reviews),
        "added_count": len(new_records),
        "skipped_count": skipped_count,
        "total_records": len(existing_fps),
        "batch_ratings": rating_counts
    }


def prune_reviews_data(
    csv_path: str,
    retention_days: int = 180,
    max_count: int = 10000,
    keep_all_helpful: bool = True
) -> Dict[str, Any]:
    """
    执行评价数据生命周期裁剪：
    1. 提取所有 is_most_helpful == True 的高赞评价，永久保留（不受 180 天与 1w 条裁剪影响）；
    2. 对于普通最新评价，剔除超过 retention_days (180天) 的记录；
    3. 剩余普通评价按 review_date 倒序排序，最多保留 max_count (10,000条)；
    4. 合并保护池与普通评价，按时间倒序重新写回 CSV，并生成/更新对应的 Markdown 概览。
    """
    if not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0:
        return {"csv_path": csv_path, "status": "file_empty_or_not_found"}

    all_records = []
    with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            all_records.append(row)

    total_before = len(all_records)
    if total_before == 0:
        return {"csv_path": csv_path, "status": "no_records"}

    now_utc = datetime.now(timezone.utc)
    cutoff_date = now_utc - timedelta(days=retention_days)
    cutoff_iso = cutoff_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    helpful_pool = []
    ordinary_pool = []

    for r in all_records:
        is_helpful = str(r.get("is_most_helpful", "")).strip().lower() in ("true", "1", "yes")
        if keep_all_helpful and is_helpful:
            helpful_pool.append(r)
        else:
            # 普通评价检查是否过期
            r_date = r.get("review_date", "")
            if r_date >= cutoff_iso:
                ordinary_pool.append(r)

    # 普通评价按日期降序排列并截断至上限
    ordinary_pool.sort(key=lambda x: x.get("review_date", ""), reverse=True)
    ordinary_retained = ordinary_pool[:max_count]

    # 合并保护池与保留的普通评价，使用 fingerprint 确保无重
    seen_fps = set()
    combined_records = []

    # 优先放入 helpful 保护池
    for r in helpful_pool:
        fp = r.get("fingerprint")
        if fp and fp not in seen_fps:
            seen_fps.add(fp)
            combined_records.append(r)

    for r in ordinary_retained:
        fp = r.get("fingerprint")
        if fp and fp not in seen_fps:
            seen_fps.add(fp)
            combined_records.append(r)

    # 最终按时间倒序
    combined_records.sort(key=lambda x: x.get("review_date", ""), reverse=True)

    # 写回 CSV
    with open(csv_path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in combined_records:
            writer.writerow(r)

    total_after = len(combined_records)
    pruned_count = total_before - total_after

    # 自动生成/更新配套 Markdown 概览文件
    generate_review_summary_md(csv_path, combined_records)

    return {
        "csv_path": csv_path,
        "total_before": total_before,
        "total_after": total_after,
        "pruned_count": pruned_count,
        "helpful_retained": len(helpful_pool),
        "ordinary_retained": len(ordinary_retained)
    }


def generate_review_summary_md(csv_path: str, records: List[Dict[str, Any]]) -> str:
    """
    根据当前 CSV 数据自动生成配套 Markdown 概览文件 (data/reviews_{app_id}.md)
    """
    md_path = os.path.splitext(csv_path)[0] + ".md"
    if not records:
        return md_path

    app_id = records[0].get("app_id", "")
    app_name = records[0].get("app_name", app_id)
    total = len(records)

    rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    helpful_list = []

    for r in records:
        try:
            star = int(r.get("rating", 5))
            if star in rating_counts:
                rating_counts[star] += 1
        except (ValueError, TypeError):
            pass

        if str(r.get("is_most_helpful", "")).lower() in ("true", "1") and len(helpful_list) < 15:
            helpful_list.append(r)

    latest_date = records[0].get("review_date", "") if records else ""
    earliest_date = records[-1].get("review_date", "") if records else ""

    md_content = [
        f"# {app_name} 评价监控概览与分析",
        "",
        f"- **App ID**: `{app_id}`",
        f"- **有效评价总数**: {total:,} 条",
        f"- **数据覆盖周期**: `{earliest_date[:10]}` 至 `{latest_date[:10]}`",
        f"- **数据源文件**: [{os.path.basename(csv_path)}]({os.path.basename(csv_path)})",
        "",
        "## 评分分布统计",
        "",
        "| 星级 | 评价条数 | 占比 | 分布条 |",
        "| :--- | :--- | :--- | :--- |"
    ]

    for star in (5, 4, 3, 2, 1):
        cnt = rating_counts[star]
        pct = (cnt / total * 100) if total > 0 else 0
        bar_len = int(pct / 4)
        bar = "█" * bar_len
        md_content.append(f"| ⭐ {star} 星 | {cnt:,} 条 | {pct:.1f}% | `{bar:<25}` |")

    if helpful_list:
        md_content.extend([
            "",
            "## 核心精选评价（最有帮助）",
            ""
        ])
        for idx, item in enumerate(helpful_list, 1):
            title = item.get("title", "")
            author = item.get("author", "用户")
            date = item.get("review_date", "")[:10]
            star = "⭐" * int(item.get("rating", 5))
            content = item.get("content", "").replace("\n", " ")
            if len(content) > 150:
                content = content[:150] + "..."
            md_content.append(f"{idx}. **[{star}] {title}** - *{author} ({date})*")
            md_content.append(f"   > {content}")
            md_content.append("")

    with open(md_path, mode="w", encoding="utf-8") as f:
        f.write("\n".join(md_content) + "\n")

    return md_path
