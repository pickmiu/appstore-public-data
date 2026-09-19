#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - App Store 公开数据监控统一执行入口

使用示例：
  python3 main.py --mode reviews     # 仅执行应用评价增量监控与去重裁剪
  python3 main.py --mode rankings    # 仅执行每日零点榜单快照与清理
  python3 main.py --mode all         # 执行全量任务 (评价 + 榜单)
"""

import os
import sys
import argparse
from typing import Dict, Any

try:
    import yaml
except ImportError:
    print("[错误] 未检测到 pyyaml 库，请先执行: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

from src.fetch_reviews import run_reviews_pipeline
from src.fetch_rankings import run_rankings_pipeline


def load_config(config_path: str = "config.yaml") -> Dict[str, Any]:
    """加载统一配置文件 config.yaml"""
    if not os.path.exists(config_path):
        print(f"[错误] 配置文件不存在: {config_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(config_path, mode="r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            return config or {}
    except Exception as e:
        print(f"[错误] 解析配置文件 {config_path} 失败: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="App Store 公开数据监控与自动化沉淀系统",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--mode",
        choices=["reviews", "rankings", "all"],
        default="all",
        help="执行模式: reviews (仅评价), rankings (仅榜单快照), all (全量)"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径"
    )

    args = parser.parse_args()
    config = load_config(args.config)

    print("=" * 60)
    print("🌟 App Store 公开数据自动化监控流水线启动")
    print(f"⚙️  运行模式: {args.mode.upper()} | 配置文件: {args.config}")
    print("=" * 60)

    if args.mode in ("reviews", "all"):
        run_reviews_pipeline(config)

    if args.mode in ("rankings", "all"):
        run_rankings_pipeline(config)

    print("\n" + "=" * 60)
    print("🎉 任务执行完毕，所有数据已同步沉淀至本地目录！")
    print("=" * 60)


if __name__ == "__main__":
    main()
