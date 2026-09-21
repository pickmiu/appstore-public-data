#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_chunking.py - Unit tests for automatic CSV chunking, rolling, and cross-part lifecycle management
"""

import os
import csv
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

from src.pipeline import (
    get_app_chunk_prefix,
    get_app_chunk_files,
    get_active_chunk_file,
    load_all_existing_review_keys,
    save_reviews_to_csv,
    prune_reviews_data,
    CSV_COLUMNS
)


class TestChunking(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.base_csv = os.path.join(self.test_dir, "reviews_12345_TestApp_us.csv")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_chunk_prefix_and_file_listing(self):
        # 1. Prefix resolution
        self.assertEqual(
            get_app_chunk_prefix("/data/reviews_123_App_us.csv"),
            "/data/reviews_123_App_us"
        )
        self.assertEqual(
            get_app_chunk_prefix("/data/reviews_123_App_us_part1.csv"),
            "/data/reviews_123_App_us"
        )
        self.assertEqual(
            get_app_chunk_prefix("/data/reviews_123_App_us_part12.csv"),
            "/data/reviews_123_App_us"
        )

        # 2. File listing with non-existent files
        self.assertEqual(get_app_chunk_files(self.base_csv), [])

        # 3. File listing with base file only
        with open(self.base_csv, "w", encoding="utf-8") as f:
            f.write("test")
        self.assertEqual(get_app_chunk_files(self.base_csv), [self.base_csv])

        # 4. File listing with parts
        os.remove(self.base_csv)
        part1 = os.path.join(self.test_dir, "reviews_12345_TestApp_us_part1.csv")
        part2 = os.path.join(self.test_dir, "reviews_12345_TestApp_us_part2.csv")
        part10 = os.path.join(self.test_dir, "reviews_12345_TestApp_us_part10.csv")
        for p in (part2, part10, part1):
            with open(p, "w", encoding="utf-8") as f:
                f.write("test")

        files = get_app_chunk_files(self.base_csv)
        self.assertEqual(files, [part1, part2, part10])

    def test_chunk_rollover_on_save(self):
        # Use a tiny chunk size of 0.002 MB (~2KB) to trigger chunk rolling
        tiny_chunk_mb = 0.002
        long_text = "This is a detailed and long review content designed to consume storage bytes quickly. " * 5

        # Batch 1: write 10 reviews (~4-5KB total, should fill base and roll to part1 + part2)
        batch_1 = [
            {
                "review_id": str(i),
                "app_id": "12345",
                "app_name": "TestApp",
                "country": "us",
                "rating": 5,
                "title": f"Review Title {i}",
                "content": long_text,
                "author": f"User_{i}",
                "review_date": "2026-09-20T10:00:00Z"
            }
            for i in range(1, 11)
        ]

        rep1 = save_reviews_to_csv(batch_1, self.base_csv, chunk_size_mb=tiny_chunk_mb)
        self.assertEqual(rep1["added_count"], 10)

        # Verify that chunk files were created
        chunk_files = get_app_chunk_files(self.base_csv)
        self.assertTrue(len(chunk_files) >= 2, f"Expected multiple chunks, got {chunk_files}")

        # Verify every chunk file has valid BOM and header
        for cf in chunk_files:
            with open(cf, "rb") as f:
                header_bytes = f.read(3)
                self.assertEqual(header_bytes, b"\xef\xbb\xbf", f"File {cf} missing UTF-8 BOM")
            with open(cf, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader)
                self.assertEqual(header, CSV_COLUMNS)

    def test_cross_chunk_deduplication(self):
        tiny_chunk_mb = 0.002
        long_text = "Deduplication testing content. " * 8

        # Ingest first batch across chunks
        batch_1 = [
            {
                "review_id": f"rev_{i}",
                "app_id": "12345",
                "app_name": "TestApp",
                "country": "us",
                "rating": 5,
                "title": f"Title {i}",
                "content": long_text,
                "author": f"Author_{i}",
                "review_date": "2026-09-20T10:00:00Z"
            }
            for i in range(1, 15)
        ]
        rep1 = save_reviews_to_csv(batch_1, self.base_csv, chunk_size_mb=tiny_chunk_mb)
        self.assertEqual(rep1["added_count"], 14)
        chunks_before = get_app_chunk_files(self.base_csv)
        self.assertTrue(len(chunks_before) >= 2)

        # Ingest duplicate batch + 2 new reviews
        batch_2 = [
            {
                "review_id": f"rev_{i}",
                "app_id": "12345",
                "app_name": "TestApp",
                "country": "us",
                "rating": 5,
                "title": f"Title {i}",
                "content": long_text,
                "author": f"Author_{i}",
                "review_date": "2026-09-20T10:00:00Z"
            }
            for i in range(1, 17) # rev_1 to rev_14 duplicate, rev_15 & rev_16 new
        ]
        rep2 = save_reviews_to_csv(batch_2, self.base_csv, chunk_size_mb=tiny_chunk_mb)
        self.assertEqual(rep2["added_count"], 2)
        self.assertEqual(rep2["dedup_skipped_count"], 14)

        # Verify global keys
        all_ids, _ = load_all_existing_review_keys(self.base_csv)
        self.assertEqual(len(all_ids), 16)

    def test_multi_chunk_lifecycle_pruning(self):
        tiny_chunk_mb = 0.002
        long_text = "Pruning test review body content. " * 6
        now_utc = datetime.now(timezone.utc)

        # 1 old ordinary review (> 180 days)
        old_date = (now_utc - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 1 old MOST HELPFUL review (> 180 days, must be protected!)
        old_helpful_date = (now_utc - timedelta(days=250)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Fresh reviews
        fresh_date = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        reviews = [
            {
                "review_id": "old_ord",
                "app_id": "12345",
                "app_name": "TestApp",
                "country": "us",
                "rating": 1,
                "title": "Old Ordinary",
                "content": long_text,
                "author": "OldUser",
                "review_date": old_date,
                "is_most_helpful": False
            },
            {
                "review_id": "old_help",
                "app_id": "12345",
                "app_name": "TestApp",
                "country": "us",
                "rating": 5,
                "title": "Old Helpful (Protected)",
                "content": long_text,
                "author": "HelpfulUser",
                "review_date": old_helpful_date,
                "is_most_helpful": True
            }
        ]

        # Add 12 fresh reviews
        for i in range(1, 13):
            reviews.append({
                "review_id": f"fresh_{i}",
                "app_id": "12345",
                "app_name": "TestApp",
                "country": "us",
                "rating": 5,
                "title": f"Fresh Review {i}",
                "content": long_text,
                "author": f"FreshUser_{i}",
                "review_date": fresh_date,
                "is_most_helpful": False
            })

        save_reviews_to_csv(reviews, self.base_csv, chunk_size_mb=tiny_chunk_mb)

        # Run pruning
        prune_rep = prune_reviews_data(
            self.base_csv,
            retention_days=180,
            max_count=100000,
            keep_all_helpful=True,
            chunk_size_mb=tiny_chunk_mb
        )

        self.assertEqual(prune_rep["pruned_count"], 1) # old_ord must be pruned
        self.assertEqual(prune_rep["helpful_retained"], 1) # old_help must be retained

        # Verify keys in remaining chunks
        remaining_ids, _ = load_all_existing_review_keys(self.base_csv)
        self.assertNotIn("old_ord", remaining_ids)
        self.assertIn("old_help", remaining_ids)
        self.assertEqual(len(remaining_ids), 13) # 1 helpful + 12 fresh


if __name__ == "__main__":
    unittest.main()
