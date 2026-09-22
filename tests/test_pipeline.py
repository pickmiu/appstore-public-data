#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pipeline.py - Unit tests for data cleaning, deduplication, and lifecycle maintenance logic
"""

import os
import shutil
import tempfile
import csv
import unittest
from datetime import datetime, timezone, timedelta

from src.pipeline import (
    clean_text,
    parse_standard_datetime,
    compute_fingerprint,
    clean_review_record,
    load_existing_fingerprints,
    save_reviews_to_csv,
    prune_reviews_data,
    CSV_COLUMNS
)
from src.fetch_rankings import is_game_item, prune_historical_rankings
from src.fetch_reviews import check_overflow_risk, get_review_filename, build_reviews_commit_message


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_csv = os.path.join(self.test_dir, "test_reviews.csv")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_clean_text(self):
        dirty = "  Test \x00\x08 Title \r\n\r\n\r\n\r\nExtra newlines   "
        cleaned = clean_text(dirty)
        self.assertEqual(cleaned, "Test  Title\n\nExtra newlines")

    def test_datetime_parsing(self):
        dt_str = "2026-09-18T10:20:30-07:00"
        parsed = parse_standard_datetime(dt_str)
        self.assertTrue(parsed.endswith("Z"))
        self.assertEqual(parsed, "2026-09-18T17:20:30Z")

        # Millisecond / microsecond Web SSR format and timezone parsing
        ms_str1 = "2026-09-18T00:03:55.000Z"
        self.assertEqual(parse_standard_datetime(ms_str1), "2026-09-18T00:03:55Z")

        ms_str2 = "2026-09-18T00:03:55.123456+08:00"
        self.assertEqual(parse_standard_datetime(ms_str2), "2026-09-17T16:03:55Z")

    def test_fingerprint_determinism(self):
        fp1 = compute_fingerprint("6670324846", "UserA", "Great", "Nice app", "2026-09-18T10:00:00Z")
        fp2 = compute_fingerprint("6670324846", "UserA", "Great", "Nice app", "2026-09-18T23:59:59Z")
        self.assertEqual(fp1, fp2)

        fp3 = compute_fingerprint("6670324846", "UserB", "Great", "Nice app", "2026-09-18T10:00:00Z")
        self.assertNotEqual(fp1, fp3)

    def test_quality_flags(self):
        # English short review
        rec_short_en = clean_review_record({"title": "", "content": "ok"})
        self.assertTrue(rec_short_en["is_short"])

        # Chinese short review (1 char is short, 2 chars retained)
        rec_short_cn = clean_review_record({"title": "", "content": "好"})
        self.assertTrue(rec_short_cn["is_short"])
        rec_valid_cn = clean_review_record({"title": "", "content": "好用"})
        self.assertFalse(rec_valid_cn["is_short"])

        # Promotional spam detection
        rec_spam = clean_review_record({"title": "Promo", "content": "Contact weixin: test8888 for free"})
        self.assertTrue(rec_spam["is_spam"])

    def test_deduplication_and_save(self):
        batch_1 = [
            {"review_id": "1", "app_id": "100", "author": "U1", "title": "T1", "content": "C1", "review_date": "2026-09-18T10:00:00Z"},
            {"review_id": "2", "app_id": "100", "author": "U2", "title": "T2", "content": "C2", "review_date": "2026-09-18T10:00:00Z"}
        ]
        rep1 = save_reviews_to_csv(batch_1, self.test_csv)
        self.assertEqual(rep1["added_count"], 2)
        self.assertEqual(rep1["skipped_count"], 0)

        # Save duplicate batch + 1 new review
        batch_2 = [
            {"review_id": "1", "app_id": "100", "author": "U1", "title": "T1", "content": "C1", "review_date": "2026-09-18T10:00:00Z"},
            {"review_id": "3", "app_id": "100", "author": "U3", "title": "T3", "content": "C3", "review_date": "2026-09-18T11:00:00Z"}
        ]
        rep2 = save_reviews_to_csv(batch_2, self.test_csv)
        self.assertEqual(rep2["added_count"], 1)
        self.assertEqual(rep2["skipped_count"], 1)
        self.assertEqual(rep2["total_records"], 3)

        # review_id priority deduplication test
        batch_3 = [
            {"review_id": "2", "app_id": "100", "author": "U2", "title": "T2 (Web format diff)", "content": "C2", "review_date": "2026-09-18T10:00:00Z"}
        ]
        rep3 = save_reviews_to_csv(batch_3, self.test_csv)
        self.assertEqual(rep3["added_count"], 0)
        self.assertEqual(rep3["skipped_count"], 1)
        self.assertEqual(rep2["total_records"], 3)

    def test_pruning_and_helpful_protection(self):
        now = datetime.now(timezone.utc)
        date_recent = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        date_old = (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Test dataset:
        # 1 recent ordinary review
        # 1 expired (200 days old) ordinary review -> should be pruned
        # 1 expired (200 days old) featured review (is_most_helpful=True) -> permanently protected
        test_records = [
            {"review_id": "101", "app_id": "200", "author": "A1", "title": "Recent", "content": "Good", "review_date": date_recent, "is_most_helpful": False},
            {"review_id": "102", "app_id": "200", "author": "A2", "title": "Old Ordinary", "content": "Old", "review_date": date_old, "is_most_helpful": False},
            {"review_id": "103", "app_id": "200", "author": "A3", "title": "Old Helpful", "content": "In-depth feedback", "review_date": date_old, "is_most_helpful": True},
        ]
        save_reviews_to_csv(test_records, self.test_csv)

        # Execute pruning (180 days, 10,000 ceiling, keep helpful)
        res = prune_reviews_data(self.test_csv, retention_days=180, max_count=10000, keep_all_helpful=True)
        self.assertEqual(res["total_before"], 3)
        self.assertEqual(res["total_after"], 2)  # Pruned 1 expired ordinary review
        self.assertEqual(res["pruned_count"], 1)
        self.assertEqual(res["helpful_retained"], 1)
        with open(self.test_csv, mode="r", encoding="utf-8-sig") as f:
            surviving_ids = [r["review_id"] for r in csv.DictReader(f)]
        self.assertEqual(surviving_ids, ["101", "103"])

    def test_retention_pre_filtering_in_save(self):
        now = datetime.now(timezone.utc)
        date_recent = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        date_old = (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")

        records = [
            {"review_id": "201", "app_id": "300", "author": "U1", "title": "Recent", "content": "Recent review", "review_date": date_recent, "is_most_helpful": False},
            {"review_id": "202", "app_id": "300", "author": "U2", "title": "Expired Ord", "content": "Old review", "review_date": date_old, "is_most_helpful": False},
            {"review_id": "203", "app_id": "300", "author": "U3", "title": "Expired Helpful", "content": "Old helpful", "review_date": date_old, "is_most_helpful": True},
        ]
        # Ingestion with retention_days=180:
        # review 202 (expired ordinary) must be skipped directly without entering CSV
        # review 201 (recent) and 203 (expired helpful) must be ingested
        rep = save_reviews_to_csv(records, self.test_csv, retention_days=180, keep_all_helpful=True)
        self.assertEqual(rep["added_count"], 2)
        self.assertEqual(rep["expired_skipped_count"], 1)
        self.assertEqual(rep["skipped_count"], 1)

        # Subsequent pruning should prune 0 records because expired ordinary reviews were already filtered
        prune_rep = prune_reviews_data(self.test_csv, retention_days=180, max_count=10000, keep_all_helpful=True)
        self.assertEqual(prune_rep["pruned_count"], 0)
        self.assertEqual(prune_rep["total_after"], 2)

    def test_game_item_filter(self):
        # Game genre ID 6014 or containing game keywords
        self.assertTrue(is_game_item(["6014"], ["Games"]))
        self.assertTrue(is_game_item(["7001"], ["Action Games"]))
        self.assertTrue(is_game_item(["1234"], ["角色扮演游戏"]))
        # Non-game apps
        self.assertFalse(is_game_item(["6005"], ["Social Networking"]))
        self.assertFalse(is_game_item(["6007"], ["Productivity"]))

    def test_historical_rankings_pruning(self):
        rankings_dir = os.path.join(self.test_dir, "rankings")
        os.makedirs(rankings_dir, exist_ok=True)

        now = datetime.now(timezone.utc)
        recent_name = (now - timedelta(days=10)).strftime("%Y-%m-%d") + "_cn.md"
        old_name = (now - timedelta(days=200)).strftime("%Y-%m-%d") + "_cn.md"

        with open(os.path.join(rankings_dir, recent_name), "w") as f:
            f.write("recent")
        with open(os.path.join(rankings_dir, old_name), "w") as f:
            f.write("old")

        deleted = prune_historical_rankings(rankings_dir, retention_days=180)
        self.assertEqual(deleted, 1)
        self.assertTrue(os.path.exists(os.path.join(rankings_dir, recent_name)))
        self.assertFalse(os.path.exists(os.path.join(rankings_dir, old_name)))

    def test_overflow_risk_detection(self):
        # 1. Reaching ceiling and additions >= threshold -> triggers alert
        msg1 = check_overflow_risk("Muse", "6760173601", added_count=450, hit_ceiling=True, threshold=300)
        self.assertIsNotNone(msg1)
        self.assertIn("6760173601", msg1)
        self.assertIn("450", msg1)

        # 2. Reaching ceiling but few additions (mostly re-crawled existing reviews) -> no alert
        msg2 = check_overflow_risk("Muse", "6760173601", added_count=10, hit_ceiling=True, threshold=300)
        self.assertIsNone(msg2)

        # 3. Not hitting 500-review ceiling -> no alert
        msg3 = check_overflow_risk("Muse", "6760173601", added_count=200, hit_ceiling=False, threshold=300)
        self.assertIsNone(msg3)

    def test_get_review_filename(self):
        # Single country English name
        fn1 = get_review_filename("6448311069", "ChatGPT", ["us"])
        self.assertEqual(fn1, "reviews_6448311069_ChatGPT_us.csv")

        # Multi-word name with spaces
        fn2 = get_review_filename("6473753684", "Claude by Anthropic", ["us"])
        self.assertEqual(fn2, "reviews_6473753684_Claude_by_Anthropic_us.csv")

        # Name with non-ASCII and parentheses
        fn3 = get_review_filename("1529124445", "CapCut (剪映海外版)", ["us"])
        self.assertEqual(fn3, "reviews_1529124445_CapCut_剪映海外版_us.csv")

        # Multi-country
        fn4 = get_review_filename("6737597349", "DeepSeek", ["cn", "us"])
        self.assertEqual(fn4, "reviews_6737597349_DeepSeek_cn_us.csv")

    def test_build_reviews_commit_message(self):
        # 1. Multi-app update: sorted descending by added count, collapsed when exceeding limit
        stats_many = [
            {"name": "通义千问", "added": 10, "pruned": 0},
            {"name": "ChatGPT", "added": 110, "pruned": 0},
            {"name": "DeepSeek", "added": 78, "pruned": 0},
            {"name": "豆包", "added": 24, "pruned": 0},
            {"name": "Grok AI", "added": 21, "pruned": 0},
            {"name": "讯飞星火", "added": 0, "pruned": 0},  # Filtered expired app, no actual changes
        ]
        msg = build_reviews_commit_message(stats_many)
        lines = msg.split("\n")
        title = lines[0]
        self.assertTrue(title.startswith("chore(data): +110 ChatGPT, +78 DeepSeek"))
        self.assertIn("(+2 more)", title)
        self.assertTrue(title.endswith("[skip ci]"))
        self.assertLessEqual(len(title), 72)
        self.assertIn("Review update summary (sorted by new reviews):", msg)
        self.assertIn("- ChatGPT: +110", msg)
        self.assertIn("- DeepSeek: +78", msg)
        self.assertIn("- 通义千问: +10", msg)
        self.assertNotIn("讯飞星火", msg)

        # 2. Single app update
        stats_single = [{"name": "ChatGPT", "added": 15, "pruned": 0}]
        msg_single = build_reviews_commit_message(stats_single)
        self.assertEqual(msg_single.split("\n")[0], "chore(data): +15 ChatGPT [skip ci]")

        # 3. Lifecycle pruning only (added is 0)
        stats_prune = [
            {"name": "讯飞星火", "added": 0, "pruned": 2},
            {"name": "ChatGPT", "added": 0, "pruned": 0}
        ]
        msg_prune = build_reviews_commit_message(stats_prune)
        self.assertIn("chore(data): prune expired reviews (-2 讯飞星火) [skip ci]", msg_prune)
        self.assertIn("pruned 2 expired review(s)", msg_prune)

        # 4. No changes
        stats_empty = [{"name": "ChatGPT", "added": 0, "pruned": 0}]
        msg_empty = build_reviews_commit_message(stats_empty)
        self.assertEqual(msg_empty, "chore(data): auto-update App Store reviews [skip ci]")

    def test_nul_byte_resilience(self):
        # Verify clean_review_record strips NUL bytes
        raw_nul = {
            "app_id": "123\x00456",
            "app_name": "App\x00Name",
            "country": "cn\x00",
            "review_id": "rev\x001",
            "author": "User\x00A",
            "title": "Bad\x00Title",
            "content": "Body\x00Text",
            "version": "1.0\x00",
            "review_date": "2026-09-18T10:00:00Z"
        }
        cleaned = clean_review_record(raw_nul)
        self.assertEqual(cleaned["app_id"], "123456")
        self.assertNotIn("\x00", cleaned["content"])
        self.assertNotIn("\x00", cleaned["original_content"])

        # Test CSV saving and pruning resilience with NUL bytes
        save_reviews_to_csv([cleaned], self.test_csv)
        report = prune_reviews_data(self.test_csv, retention_days=180, max_count=1000)
        self.assertEqual(report["total_after"], 1)


if __name__ == "__main__":
    unittest.main()
