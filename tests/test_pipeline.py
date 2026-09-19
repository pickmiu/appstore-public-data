#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_pipeline.py - 数据清洗、去重与生命周期维护核心逻辑单元测试
"""

import os
import shutil
import tempfile
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


class TestPipeline(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.test_csv = os.path.join(self.test_dir, "test_reviews.csv")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_clean_text(self):
        dirty = "  测试 \x00\x08 标题 \r\n\r\n\r\n\r\n多余换行   "
        cleaned = clean_text(dirty)
        self.assertEqual(cleaned, "测试  标题\n\n多余换行")

    def test_datetime_parsing(self):
        dt_str = "2026-09-18T10:20:30-07:00"
        parsed = parse_standard_datetime(dt_str)
        self.assertTrue(parsed.endswith("Z"))
        self.assertEqual(parsed, "2026-09-18T17:20:30Z")

    def test_fingerprint_determinism(self):
        fp1 = compute_fingerprint("6670324846", "UserA", "Great", "Nice app", "2026-09-18T10:00:00Z")
        fp2 = compute_fingerprint("6670324846", "UserA", "Great", "Nice app", "2026-09-18T23:59:59Z")
        self.assertEqual(fp1, fp2)

        fp3 = compute_fingerprint("6670324846", "UserB", "Great", "Nice app", "2026-09-18T10:00:00Z")
        self.assertNotEqual(fp1, fp3)

    def test_quality_flags(self):
        # 英文短评测试
        rec_short_en = clean_review_record({"title": "", "content": "ok"})
        self.assertTrue(rec_short_en["is_short"])

        # 中文短评测试 (1 个字为短评，2 个字保留为有态度评价)
        rec_short_cn = clean_review_record({"title": "", "content": "好"})
        self.assertTrue(rec_short_cn["is_short"])
        rec_valid_cn = clean_review_record({"title": "", "content": "好用"})
        self.assertFalse(rec_valid_cn["is_short"])

        # 垃圾广告引流测试
        rec_spam = clean_review_record({"title": "推广", "content": "加微信号: test8888 免费领"})
        self.assertTrue(rec_spam["is_spam"])

    def test_deduplication_and_save(self):
        batch_1 = [
            {"review_id": "1", "app_id": "100", "author": "U1", "title": "T1", "content": "C1", "review_date": "2026-09-18T10:00:00Z"},
            {"review_id": "2", "app_id": "100", "author": "U2", "title": "T2", "content": "C2", "review_date": "2026-09-18T10:00:00Z"}
        ]
        rep1 = save_reviews_to_csv(batch_1, self.test_csv)
        self.assertEqual(rep1["added_count"], 2)
        self.assertEqual(rep1["skipped_count"], 0)

        # 再次保存相同批次 + 1条新数据
        batch_2 = [
            {"review_id": "1", "app_id": "100", "author": "U1", "title": "T1", "content": "C1", "review_date": "2026-09-18T10:00:00Z"},
            {"review_id": "3", "app_id": "100", "author": "U3", "title": "T3", "content": "C3", "review_date": "2026-09-18T11:00:00Z"}
        ]
        rep2 = save_reviews_to_csv(batch_2, self.test_csv)
        self.assertEqual(rep2["added_count"], 1)
        self.assertEqual(rep2["skipped_count"], 1)
        self.assertEqual(rep2["total_records"], 3)

    def test_pruning_and_helpful_protection(self):
        now = datetime.now(timezone.utc)
        date_recent = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        date_old = (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # 构造数据：
        # 1 条近期普通评价
        # 1 条超期（200天前）普通评价 -> 应被裁剪淘汰
        # 1 条超期（200天前）高赞评价 (is_most_helpful=True) -> 必须永久保护！
        test_records = [
            {"review_id": "101", "app_id": "200", "author": "A1", "title": "Recent", "content": "Good", "review_date": date_recent, "is_most_helpful": False},
            {"review_id": "102", "app_id": "200", "author": "A2", "title": "Old Ordinary", "content": "Old", "review_date": date_old, "is_most_helpful": False},
            {"review_id": "103", "app_id": "200", "author": "A3", "title": "Old Helpful", "content": "In-depth feedback", "review_date": date_old, "is_most_helpful": True},
        ]
        save_reviews_to_csv(test_records, self.test_csv)

        # 执行裁剪 (限制 180 天，上限 10,000，永久保留 helpful)
        res = prune_reviews_data(self.test_csv, retention_days=180, max_count=10000, keep_all_helpful=True)
        self.assertEqual(res["total_before"], 3)
        self.assertEqual(res["total_after"], 2)  # 淘汰了 1 条普通超期评价
        self.assertEqual(res["pruned_count"], 1)
        self.assertEqual(res["helpful_retained"], 1)

    def test_game_item_filter(self):
        # 游戏品类 ID 6014 或名称包含游戏
        self.assertTrue(is_game_item(["6014"], ["Games"]))
        self.assertTrue(is_game_item(["7001"], ["Action Games"]))
        self.assertTrue(is_game_item(["1234"], ["角色扮演游戏"]))
        # 普通纯应用
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


if __name__ == "__main__":
    unittest.main()
