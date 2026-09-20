#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - App Store Public Data Monitoring Unified Execution Entrypoint

Usage Examples:
  python3 main.py --mode reviews     # Run incremental review monitoring and pruning only
  python3 main.py --mode rankings    # Run daily ranking snapshot and retention cleanup only
  python3 main.py --mode all         # Run all tasks (reviews + rankings)
"""

import os
import sys
import argparse
from typing import Dict, Any

try:
    import yaml
except ImportError:
    print("[Error] PyYAML library not detected. Please install: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from src.fetch_reviews import run_reviews_pipeline
from src.fetch_rankings import run_rankings_pipeline


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """Load unified configuration file config.yaml"""
    if not os.path.exists(config_path):
        print(f"[Error] Configuration file does not exist: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, mode="r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            return config or {}
    except Exception as e:
        print(f"[Error] Failed to parse configuration file {config_path}: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="App Store Public Data Monitoring and Automated Archival System",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--mode",
        choices=["reviews", "rankings", "all"],
        default="all",
        help="Execution mode: reviews (reviews only), rankings (rankings snapshot only), all (full pipeline)"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to configuration file"
    )

    args = parser.parse_args()
    config = load_config(args.config)

    print("=" * 60)
    print("🌟 App Store Public Data Monitoring Pipeline Started")
    print(f"⚙️  Execution Mode: {args.mode.upper()} | Config: {args.config}")
    print("=" * 60)

    if args.mode in ("reviews", "all"):
        run_reviews_pipeline(config)

    if args.mode in ("rankings", "all"):
        run_rankings_pipeline(config)

    print("\n" + "=" * 60)
    print("🎉 All pipeline tasks completed. Data synchronized to local directory!")
    print("=" * 60)


if __name__ == "__main__":
    main()
