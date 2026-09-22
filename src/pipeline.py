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
from typing import List, Dict, Any, Set, Tuple, Optional

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
    app_id = str(raw.get("app_id", "")).replace("\x00", "").strip()
    app_name = str(raw.get("app_name", "")).replace("\x00", "").strip()
    country = str(raw.get("country", "")).replace("\x00", "").strip().lower()
    review_id = str(raw.get("review_id", "")).replace("\x00", "").strip()
    author = str(raw.get("author", "Anonymous")).replace("\x00", "").strip()
    version = str(raw.get("version", "")).replace("\x00", "").strip()
    source = str(raw.get("source", default_source)).replace("\x00", "").strip()

    # Normalize star rating (integer between 1 and 5)
    try:
        rating = int(raw.get("rating", 5))
        rating = max(1, min(5, rating))
    except (ValueError, TypeError):
        rating = 5

    # Text cleaning
    orig_content = str(raw.get("content", "")).replace("\x00", "")
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
            reader = csv.reader(line.replace("\x00", "") for line in f)
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


def get_app_chunk_prefix(csv_path: str) -> str:
    """Extract base prefix without .csv extension or _partN suffix."""
    path_no_ext = csv_path[:-4] if csv_path.endswith(".csv") else csv_path
    return re.sub(r"_part\d+$", "", path_no_ext)


def get_app_chunk_files(base_path: str) -> List[str]:
    """
    Find all chunk/part files associated with a base CSV path.
    If part files (_part1.csv, _part2.csv, ...) exist, returns them sorted by part index.
    If only the base file exists, returns [base_file].
    If none exists, returns [].
    """
    prefix = get_app_chunk_prefix(base_path)
    dir_name = os.path.dirname(prefix) or "."
    base_file = f"{prefix}.csv"

    part_files = []
    prefix_base = os.path.basename(prefix)
    part_pattern = re.compile(rf"^{re.escape(prefix_base)}_part(\d+)\.csv$")

    if os.path.exists(dir_name):
        for fname in os.listdir(dir_name):
            m = part_pattern.match(fname)
            if m:
                part_files.append((int(m.group(1)), os.path.join(dir_name, fname)))

    if part_files:
        part_files.sort(key=lambda x: x[0])
        return [f for _, f in part_files]

    if os.path.exists(base_file):
        return [base_file]

    return []


def get_active_chunk_file(base_path: str, chunk_size_mb: float = 45.0) -> Tuple[str, int]:
    """
    Determine the current active (writable) file for appending reviews.
    Threshold is converted to bytes (chunk_size_mb * 1024 * 1024).

    Rules:
    1. If part files exist (_part1.csv, _part2.csv, ...):
       Check highest numbered part M:
       - If size < threshold: active file is part M.
       - If size >= threshold: roll to part M+1.
    2. If no part files exist, but base_file exists:
       - If size < threshold: active file is base_file (part 0).
       - If size >= threshold:
         Rename base_file -> prefix + "_part1.csv".
         Active file is prefix + "_part2.csv" (part 2).
    3. If neither exists:
       - Active file is base_file (part 0).

    Returns:
      (active_filepath, part_number)
    """
    prefix = get_app_chunk_prefix(base_path)
    dir_name = os.path.dirname(prefix) or "."
    base_file = f"{prefix}.csv"
    chunk_size_bytes = int(chunk_size_mb * 1024 * 1024)

    part_files = []
    prefix_base = os.path.basename(prefix)
    part_pattern = re.compile(rf"^{re.escape(prefix_base)}_part(\d+)\.csv$")

    if os.path.exists(dir_name):
        for fname in os.listdir(dir_name):
            m = part_pattern.match(fname)
            if m:
                part_files.append((int(m.group(1)), os.path.join(dir_name, fname)))

    if part_files:
        part_files.sort(key=lambda x: x[0])
        last_num, last_path = part_files[-1]
        if os.path.exists(last_path) and os.path.getsize(last_path) >= chunk_size_bytes:
            next_num = last_num + 1
            return os.path.join(dir_name, f"{prefix_base}_part{next_num}.csv"), next_num
        return last_path, last_num

    if os.path.exists(base_file):
        if os.path.getsize(base_file) >= chunk_size_bytes:
            part1_file = os.path.join(dir_name, f"{prefix_base}_part1.csv")
            part2_file = os.path.join(dir_name, f"{prefix_base}_part2.csv")
            os.rename(base_file, part1_file)
            return part2_file, 2
        return base_file, 0

    return base_file, 0


def load_all_existing_review_keys(base_path: str) -> Tuple[Set[str], Set[str]]:
    """
    Read (review_id set, fingerprint set) across ALL chunk files for the given app path.
    Guarantees global uniqueness across all historical and active chunks.
    """
    files = get_app_chunk_files(base_path)
    if not files:
        prefix = get_app_chunk_prefix(base_path)
        base_file = f"{prefix}.csv"
        if os.path.exists(base_file):
            files = [base_file]

    all_ids = set()
    all_fps = set()
    for f in files:
        r_ids, fps = load_existing_review_keys(f)
        all_ids.update(r_ids)
        all_fps.update(fps)
    return all_ids, all_fps


def save_reviews_to_csv(
    raw_reviews: List[Dict[str, Any]],
    output_path: str,
    default_source: str = "itunes_rss",
    retention_days: Optional[int] = None,
    keep_all_helpful: bool = True,
    chunk_size_mb: Optional[float] = 45.0
) -> Dict[str, Any]:
    """
    Unified incremental save: Clean -> Retention pre-filter -> Global Deduplication -> Write to active chunk.
    Automatically rolls over to _partN.csv if file size reaches chunk_size_mb threshold.

    :param raw_reviews: List of raw review dictionaries to save
    :param output_path: Destination CSV base filepath
    :param default_source: Default source identifier
    :param retention_days: Optional retention cutoff in days (ordinary reviews older than this are skipped)
    :param keep_all_helpful: Whether to exempt most helpful / featured reviews from retention cutoff
    :param chunk_size_mb: Chunk size threshold in MB (default 45MB)
    :return: Operation statistics summary dictionary
    """
    dir_name = os.path.dirname(output_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    cutoff_iso = None
    if retention_days is not None and retention_days > 0:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
        cutoff_iso = cutoff_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Load existing keys globally across all parts of this app
    existing_ids, existing_fps = load_all_existing_review_keys(output_path)

    new_records = []
    skipped_count = 0
    dedup_skipped_count = 0
    expired_skipped_count = 0
    rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

    for raw in raw_reviews:
        cleaned = clean_review_record(raw, default_source=default_source)
        rev_id = str(cleaned.get("review_id", "")).strip()
        fp = cleaned["fingerprint"]

        # 1. Pre-filter expired ordinary reviews before ingestion
        if cutoff_iso and not (keep_all_helpful and cleaned.get("is_most_helpful")):
            r_date = cleaned.get("review_date", "")
            if r_date and r_date < cutoff_iso:
                expired_skipped_count += 1
                skipped_count += 1
                continue

        # 2. Prioritize review_id deduplication; fallback to content fingerprint
        if rev_id and rev_id in existing_ids:
            dedup_skipped_count += 1
            skipped_count += 1
            continue
        if fp in existing_fps:
            dedup_skipped_count += 1
            skipped_count += 1
            continue

        if rev_id:
            existing_ids.add(rev_id)
        existing_fps.add(fp)
        new_records.append(cleaned)
        rating_counts[cleaned["rating"]] = rating_counts.get(cleaned["rating"], 0) + 1

    # Write records with chunk rolling support
    if new_records:
        if chunk_size_mb is None or chunk_size_mb <= 0:
            # Unchunked mode (legacy direct write)
            file_exists = os.path.exists(output_path) and os.path.getsize(output_path) > 0
            write_mode = "a" if file_exists else "w"
            with open(output_path, mode=write_mode, encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                if not file_exists:
                    writer.writeheader()
                for record in new_records:
                    writer.writerow(record)
        else:
            chunk_size_bytes = int(chunk_size_mb * 1024 * 1024)
            records_to_write = list(new_records)
            while records_to_write:
                active_file, _ = get_active_chunk_file(output_path, chunk_size_mb=chunk_size_mb)
                file_exists = os.path.exists(active_file) and os.path.getsize(active_file) > 0

                with open(active_file, mode="a" if file_exists else "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
                    if not file_exists:
                        writer.writeheader()
                        f.flush()

                    while records_to_write:
                        record = records_to_write.pop(0)
                        writer.writerow(record)
                        if f.tell() >= chunk_size_bytes and records_to_write:
                            f.flush()
                            break

    chunk_files = get_app_chunk_files(output_path)
    if not chunk_files:
        chunk_files = [output_path]
    active_path, _ = get_active_chunk_file(output_path, chunk_size_mb=chunk_size_mb or 45.0) if chunk_size_mb else (output_path, 0)

    return {
        "output_path": output_path,
        "active_path": active_path,
        "chunk_files": chunk_files,
        "input_count": len(raw_reviews),
        "added_count": len(new_records),
        "skipped_count": skipped_count,
        "dedup_skipped_count": dedup_skipped_count,
        "expired_skipped_count": expired_skipped_count,
        "total_records": len(existing_fps),
        "batch_ratings": rating_counts
    }


def prune_reviews_data(
    csv_path: str,
    retention_days: int = 180,
    max_count: int = 100000,
    keep_all_helpful: bool = True,
    chunk_size_mb: Optional[float] = 45.0
) -> Dict[str, Any]:
    """
    Execute data lifecycle pruning across all chunk files for an app:
    1. Extract all is_most_helpful == True reviews into a permanent protection pool.
    2. Filter out ordinary reviews older than retention_days.
    3. Sort remaining ordinary reviews by review_date descending, retaining up to max_count.
    4. Write back into chunked files respecting chunk_size_mb, cleaning up unneeded empty parts.
    """
    chunk_files = get_app_chunk_files(csv_path)
    if not chunk_files:
        if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
            chunk_files = [csv_path]
        else:
            return {"csv_path": csv_path, "status": "file_empty_or_not_found"}

    all_records = []
    for fpath in chunk_files:
        if os.path.exists(fpath) and os.path.getsize(fpath) > 0:
            with open(fpath, mode="r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(line.replace("\x00", "") for line in f)
                for row in reader:
                    all_records.append(row)

    total_before = len(all_records)
    if total_before == 0:
        return {"csv_path": csv_path, "status": "no_records"}

    now_utc = datetime.now(timezone.utc)
    cutoff_date = now_utc - timedelta(days=retention_days)
    cutoff_iso = cutoff_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    helpful_fps = set()
    ordinary_valid = []

    for r in all_records:
        is_helpful = str(r.get("is_most_helpful", "")).strip().lower() in ("true", "1", "yes")
        if keep_all_helpful and is_helpful:
            helpful_fps.add(r.get("fingerprint"))
        else:
            r_date = r.get("review_date", "")
            if r_date >= cutoff_iso:
                ordinary_valid.append(r)

    # Sort ordinary reviews descending by date and truncate to max_count
    ordinary_sorted = sorted(ordinary_valid, key=lambda x: x.get("review_date", ""), reverse=True)
    ordinary_retained_fps = {r.get("fingerprint") for r in ordinary_sorted[:max_count]}

    # Set of all retained fingerprints (helpful pool + within-retention ordinary pool)
    retained_fps = helpful_fps.union(ordinary_retained_fps)

    # Reconstruct records preserving existing file order and uniqueness
    seen_fps = set()
    combined_records = []
    for r in all_records:
        fp = r.get("fingerprint")
        if fp and fp in retained_fps and fp not in seen_fps:
            seen_fps.add(fp)
            combined_records.append(r)

    total_after = len(combined_records)
    pruned_count = total_before - total_after

    # Write back: if single file and not chunked and no pruning occurred, avoid touching disk
    prefix = get_app_chunk_prefix(csv_path)
    dir_name = os.path.dirname(prefix) or "."
    prefix_base = os.path.basename(prefix)
    chunk_size_bytes = int(chunk_size_mb * 1024 * 1024) if chunk_size_mb else 45 * 1024 * 1024

    is_already_chunked = len(chunk_files) > 1 or any("_part" in f for f in chunk_files)
    
    # If unchunked and pruned_count == 0, keep file untouched
    if not is_already_chunked and pruned_count == 0:
        return {
            "csv_path": csv_path,
            "chunk_files": chunk_files,
            "total_before": total_before,
            "total_after": total_after,
            "pruned_count": pruned_count,
            "helpful_retained": len(helpful_fps),
            "ordinary_retained": len(ordinary_retained_fps)
        }

    # If single file and fits in chunk size, write directly to base file
    if not is_already_chunked:
        base_file = f"{prefix}.csv"
        with open(base_file, mode="w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            for r in combined_records:
                writer.writerow(r)
        
        # Check if writing caused it to exceed chunk_size_bytes
        if chunk_size_mb and os.path.getsize(base_file) >= chunk_size_bytes:
            # Roll over immediately
            get_active_chunk_file(base_file, chunk_size_mb=chunk_size_mb)
            chunk_files = get_app_chunk_files(base_file)
        else:
            chunk_files = [base_file]

        return {
            "csv_path": csv_path,
            "chunk_files": chunk_files,
            "total_before": total_before,
            "total_after": total_after,
            "pruned_count": pruned_count,
            "helpful_retained": len(helpful_fps),
            "ordinary_retained": len(ordinary_retained_fps)
        }

    # Multi-chunk write back
    part_idx = 1
    records_left = list(combined_records)
    used_part_files = []

    while records_left or part_idx == 1:
        part_file = os.path.join(dir_name, f"{prefix_base}_part{part_idx}.csv")
        used_part_files.append(part_file)
        with open(part_file, mode="w", encoding="utf-8-sig", newline="") as pf:
            writer = csv.DictWriter(pf, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            while records_left:
                rec = records_left.pop(0)
                writer.writerow(rec)
                if pf.tell() >= chunk_size_bytes and records_left:
                    pf.flush()
                    part_idx += 1
                    break
        if not records_left:
            break

    # Clean up obsolete extra parts
    for old_f in chunk_files:
        if old_f not in used_part_files and os.path.exists(old_f):
            try:
                os.remove(old_f)
            except OSError:
                pass

    return {
        "csv_path": csv_path,
        "chunk_files": used_part_files,
        "total_before": total_before,
        "total_after": total_after,
        "pruned_count": pruned_count,
        "helpful_retained": len(helpful_fps),
        "ordinary_retained": len(ordinary_retained_fps)
    }
