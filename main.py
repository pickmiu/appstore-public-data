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
import traceback
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
            if not isinstance(config, dict):
                print(f"[Error] Configuration file {config_path} must contain a top-level mapping", file=sys.stderr)
                sys.exit(1)
            return config
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        print(f"[Error] Failed to load configuration file {config_path}: {e}", file=sys.stderr)
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

    failures = []

    if args.mode in ("reviews", "all"):
        try:
            run_reviews_pipeline(config)
        except Exception as e:
            print(f"[Error] Reviews pipeline failed: {e}", file=sys.stderr)
            traceback.print_exc()
            failures.append("reviews")

    if args.mode in ("rankings", "all"):
        try:
            run_rankings_pipeline(config)
        except Exception as e:
            print(f"[Error] Rankings pipeline failed: {e}", file=sys.stderr)
            traceback.print_exc()
            failures.append("rankings")

    if failures:
        print(f"\n[Error] Pipeline finished with failures in: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("🎉 All pipeline tasks completed. Data synchronized to local directory!")
    print("=" * 60)


if __name__ == "__main__":
    main()
