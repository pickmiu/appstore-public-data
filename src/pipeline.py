#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pipeline.py - Core Engine for Review Cleaning, Fingerprinting, Deduplication, and Lifecycle Management

Follows simplicity and zero-redundancy principles. Employs SHA-256 content fingerprints to guarantee
zero duplicates in incremental review collection, and enforces a 180-day retention window, a 10,000-review
ceiling, and permanent retention for "Most Helpful" reviews.
"""

import os
import re
import csv
import hashlib
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Set, Tuple

# Standardized CSV Header Definition
CSV_COLUMNS = [
    "fingerprint",       # Primary key: SHA-256 (app_id + author + title + content + date_day)
    "review_id",         # Official App Store review ID (if present)
    "app_id",            # Application ID
    "app_name",          # Application name
    "country",           # Country / storefront code (e.g. us, cn)
    "rating",            # Star rating (1-5 integer)
    "title",             # Cleaned review title
    "content",           # Cleaned review body content
    "original_content",  # Unmodified original body (for traceability)
    "author",            # Reviewer username
    "version",           # App version associated with review
    "review_date",       # Standard ISO-8601 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)
    "is_most_helpful",   # Whether flagged as featured/most-helpful (permanently protected)
    "source",            # Data source identifier (itunes_rss / web_ssr / manual)
    "is_short",          # Quality flag: ultra-short or meaningless review
    "is_spam"            # Quality flag: suspected spam, promotion, or bot activity
]

# Regular expression for spam and promotional patterns (links, handles, phone numbers)
SPAM_PATTERNS = re.compile(
    r"(https?://|www\.|t\.me/|bit\.ly/|weixin|vx:|微信|\+?\d{7,15})",
    re.IGNORECASE
)

# Regular expression for punctuation and whitespace only
PUNCT_ONLY_PATTERN = re.compile(r"^[\s\W_]+$")


def clean_text(text: Any) -> str:
    """
    Text cleaning and sanitization:
    1. Remove invisible ASCII control characters (preserving \n and \t).
    2. Normalize line breaks to Unix style (\n).
    3. Collapse redundant consecutive blank lines (max 2).
    4. Strip leading and trailing whitespace from each line.
    """
    if text is None:
        return ""
    s = str(text)
    # Remove ASCII control characters
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", s)
    # Normalize line breaks
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse 3 or more line breaks into 2
    s = re.sub(r"\n{3,}", "\n\n", s)
    # Strip whitespace per line
    lines = [line.strip() for line in s.split("\n")]
    return "\n".join(lines).strip()


def parse_standard_datetime(date_val: Any) -> str:
    """Convert various datetime representations into standard ISO-8601 UTC string (YYYY-MM-DDTHH:MM:SSZ)."""
    if not date_val:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    s = str(date_val).strip()
    # Strip millisecond / microsecond component (.000 or .123456)
    s = re.sub(r"\.\d+", "", s)
    # Parse standard formats
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
    Compute unique SHA-256 content fingerprint.

    Hashes app_id + author + title + content + date_day.
    Using the first 10 characters (YYYY-MM-DD) eliminates millisecond-level crawl discrepancies,
    ensuring that identical reviews posted by the same user on the same date are recognized as duplicates.
    """
    date_day = date_iso[:10] if len(date_iso) >= 10 else date_iso
    raw_str = f"{app_id}|{author.strip()}|{title.strip()}|{content.strip()}|{date_day}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()


def clean_review_record(raw: Dict[str, Any], default_source: str = "itunes_rss") -> Dict[str, Any]:
    """
    Standardize a single raw review dictionary, align fields, apply quality flags, and generate fingerprint.
    """
    app_id = str(raw.get("app_id", "")).strip()
    app_name = str(raw.get("app_name", "")).strip()
    country = str(raw.get("country", "")).strip().lower()
    review_id = str(raw.get("review_id", "")).strip()
    author = str(raw.get("author", "Anonymous")).strip()
    version = str(raw.get("version", "")).strip()
    source = str(raw.get("source", default_source)).strip()

    # Normalize star rating (integer between 1 and 5)
    try:
        rating = int(raw.get("rating", 5))
        rating = max(1, min(5, rating))
    except (ValueError, TypeError):
        rating = 5

    # Text cleaning
    orig_content = str(raw.get("content", ""))
    cleaned_title = clean_text(raw.get("title", ""))
    cleaned_content = clean_text(orig_content)

    # Standardize datetime
    date_iso = parse_standard_datetime(raw.get("review_date") or raw.get("updated") or raw.get("date"))

    # Most helpful indicator
    is_most_helpful = bool(raw.get("is_most_helpful", False) or "helpful" in source.lower())

    # Quality flagging: CJK single char or non-CJK <= 2 chars or punctuation only marked as short review
    has_cjk = bool(re.search(r"[\u4e00-\u9fa5]", cleaned_content))
    min_len = 1 if has_cjk else 2
    is_short = len(cleaned_content) <= min_len or bool(PUNCT_ONLY_PATTERN.match(cleaned_content))
    is_spam = bool(SPAM_PATTERNS.search(cleaned_content) or SPAM_PATTERNS.search(cleaned_title))

    # Fingerprint generation
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


def load_existing_review_keys(csv_path: str) -> Tuple[Set[str], Set[str]]:
    """Quickly read (review_id set, fingerprint set) from an existing CSV file."""
    if not os.path.exists(csv_path):
        return set(), set()

    review_ids = set()
    fingerprints = set()
    try:
        with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            if not headers:
                return set(), set()
            try:
                id_idx = headers.index("review_id")
            except ValueError:
                id_idx = -1
            try:
                fp_idx = headers.index("fingerprint")
            except ValueError:
                fp_idx = 0

            for row in reader:
                if not row:
                    continue
                if id_idx != -1 and len(row) > id_idx:
                    rid = row[id_idx].strip()
                    if rid:
                        review_ids.add(rid)
                if len(row) > fp_idx:
                    fp = row[fp_idx].strip()
                    if fp:
                        fingerprints.add(fp)
    except Exception as e:
        print(f"[Warning] Error reading CSV index keys from {csv_path}: {e}")
    return review_ids, fingerprints


def load_existing_fingerprints(csv_path: str) -> Set[str]:
    """Read fingerprint set from existing CSV file (kept for backward compatibility)."""
    _, fps = load_existing_review_keys(csv_path)
    return fps


def save_reviews_to_csv(
    raw_reviews: List[Dict[str, Any]],
    output_path: str,
    default_source: str = "itunes_rss"
) -> Dict[str, Any]:
    """
    Unified incremental save: Clean -> Multi-tier deduplication -> Append to CSV.

    :param raw_reviews: List of raw review dictionaries to save
    :param output_path: Destination CSV filepath
    :param default_source: Default source identifier
    :return: Operation statistics summary dictionary
    """
    dir_name = os.path.dirname(output_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    existing_ids, existing_fps = load_existing_review_keys(output_path)
    file_exists = os.path.exists(output_path) and os.path.getsize(output_path) > 0

    new_records = []
    skipped_count = 0
    rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

    for raw in raw_reviews:
        cleaned = clean_review_record(raw, default_source=default_source)
        rev_id = str(cleaned.get("review_id", "")).strip()
        fp = cleaned["fingerprint"]

        # Prioritize review_id deduplication; fallback to content fingerprint
        if rev_id and rev_id in existing_ids:
            skipped_count += 1
            continue
        if fp in existing_fps:
            skipped_count += 1
            continue

        if rev_id:
            existing_ids.add(rev_id)
        existing_fps.add(fp)
        new_records.append(cleaned)
        rating_counts[cleaned["rating"]] = rating_counts.get(cleaned["rating"], 0) + 1

    # Append to CSV (UTF-8 with BOM for Excel friendliness)
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
    Execute data lifecycle pruning on reviews CSV:
    1. Extract all is_most_helpful == True reviews into a permanent protection pool (exempt from 180-day & 10k limits).
    2. Filter out ordinary reviews older than retention_days (180 days).
    3. Sort remaining ordinary reviews by review_date descending, retaining up to max_count (10,000).
    4. Merge protected reviews with retained ordinary reviews, write back to CSV, preserving timeline order.
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
            r_date = r.get("review_date", "")
            if r_date >= cutoff_iso:
                ordinary_pool.append(r)

    # Sort ordinary reviews descending by date and truncate to max_count
    ordinary_pool.sort(key=lambda x: x.get("review_date", ""), reverse=True)
    ordinary_retained = ordinary_pool[:max_count]

    # Merge protected pool and retained ordinary pool using fingerprint for uniqueness
    seen_fps = set()
    combined_records = []

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

    # Final sort descending by review date
    combined_records.sort(key=lambda x: x.get("review_date", ""), reverse=True)

    # Write back to CSV
    with open(csv_path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in combined_records:
            writer.writerow(r)

    total_after = len(combined_records)
    pruned_count = total_before - total_after

    return {
        "csv_path": csv_path,
        "total_before": total_before,
        "total_after": total_after,
        "pruned_count": pruned_count,
        "helpful_retained": len(helpful_pool),
        "ordinary_retained": len(ordinary_retained)
    }
