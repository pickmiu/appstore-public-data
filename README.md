# App Store 公开数据监控与自动化存储 (App Store Public Data)

基于 GitHub Actions 免费 Runner 运行的 App Store 公开数据自动化监控系统。通过 CSV 与 Markdown 格式持久化存储被监控应用的用户评价及每日全品类榜单快照，所有数据自动同步沉淀至 GitHub 仓库。

---

## 核心特性

- **应用评价增量监控**：定时轮询拉取监控应用的最新评价，基于 `SHA256` 内容指纹秒级去重，保证增量评价一条不漏。
- **数据清洗与质量打标**：自动过滤不可见字符、多余空行，时间标准化为 ISO-8601 UTC，自动标记超短评与广告引流内容。
- **生命周期与留存管理**：
  - 评价默认保留 **半年（180 天）** 数据；
  - 单个应用普通评价上限 **10,000 条最新**；
  - **所有“最有帮助”（Most Helpful）深度评价永久留存**，豁免过期清理。
- **每日零点中美纯应用榜单快照**：每天北京时间 00:00 自动抓取中国区与美国区的主榜单前 100（自动剔除游戏，纯应用榜）及一级应用品类前 10，生成 Markdown 快照，默认保留半年。彻底排除所有游戏相关内容。
- **统一配置文件管理**：所有应用列表、榜单地区、品类代码、留存天数统一在 [`config.yaml`](./config.yaml) 中维护。
- **零成本运行**：依托 GitHub Actions 免费 Runner 执行，零云服务器成本，每月仅耗费约 5% 免费额度。

---

## 目录结构

```text
appstore-public-data/
├── .github/
│   └── workflows/
│       ├── monitor_reviews.yml    # 评价高频监控工作流 (每 3 小时自动运行)
│       └── snapshot_rankings.yml  # 每日北京时间零点榜单快照工作流
├── config.yaml                    # 全局统一配置文件 (核心管控点)
├── DESIGN.md                      # 系统详细技术设计与客观利弊分析文档
├── README.md                      # 项目说明文档
├── main.py                        # 本地与 Runner 统一入口脚本
├── src/
│   ├── __init__.py
│   ├── pipeline.py                # 清洗、指纹去重与数据裁剪引擎
│   ├── fetch_reviews.py           # 评价抓取与增量入库模块
│   └── fetch_rankings.py          # 榜单抓取与快照生成模块
├── data/                          # 评价数据存储目录
│   ├── reviews_{app_id}.csv       # 单个应用评价明细 (UTF-8 BOM 编码，Excel 友好)
│   └── reviews_{app_id}.md        # 单个应用评价概览与口碑分布
└── rankings/                      # 每日榜单快照目录 (保留 180 天)
    ├── 2026-09-19_cn.md           # 中国区纯应用主榜 Top100 + 一级应用品类前 10
    ├── 2026-09-19_us.md           # 美区纯应用主榜 Top100 + 一级应用品类前 10
    └── ...
```

---

## 快速上手与配置

### 1. 统一配置 (`config.yaml`)

编辑仓库根目录下的 `config.yaml`，默认仅针对中美大区，并彻底排除所有游戏相关内容：

```yaml
# 数据留存规则
retention:
  reviews_days: 180            # 评价保留天数
  reviews_max_count: 10000     # 单应用普通评价上限
  keep_all_helpful: true       # 最有帮助评价永久保留
  rankings_days: 180           # 榜单快照保留天数

# 监控应用列表 (默认仅监控中美两区)
monitored_apps:
  - id: "6670324846"
    name: "Grok AI"
    countries: ["us", "cn"]
  - id: "6760173601"
    name: "Muse from Meta"
    countries: ["us", "cn"]

# 榜单快照配置 (排除所有游戏)
rankings:
  regions: ["cn", "us"]        # 默认仅抓取中国和美国
  exclude_games: true          # 排除所有游戏类应用及子品类
  main_chart_limit: 100        # 主榜单前 100 (纯应用榜，自动过滤游戏)
  primary_genre_limit: 10      # 一级品类前 10
  secondary_genre_limit: 5     # 二级品类前 5
  secondary_genres: []         # 因排除游戏，默认不抓取游戏二级子品类
```

### 2. 本地调试与运行

系统采用极简设计，仅需 Python 3.8+ 及 `PyYAML`：

```bash
# 1. 安装基础依赖
pip install pyyaml

# 2. 运行评价抓取与增量入库
python3 main.py --mode reviews

# 3. 运行榜单快照生成
python3 main.py --mode rankings

# 4. 全量运行 (抓取评价 + 生成快照 + 执行数据生命周期裁剪)
python3 main.py --mode all
```

---

## GitHub Actions 自动化机制

本仓库配置了两个自动调度工作流：
1. **评价监控**（`monitor_reviews.yml`）：每 3 小时轻量轮询一次，增量提取新评价并写入 CSV/MD。
2. **榜单快照**（`snapshot_rankings.yml`）：每天北京时间 00:00（UTC 16:00）运行，抓取各地区各品类榜单并清理半年前旧快照。

工作流运行后若产生数据变动，将由系统自动完成 `git commit` 并同步推送至当前仓库。

> [!NOTE]
> 更多详细的架构设计、API 来源对比、Git 仓库存储优化与并发策略，请参阅完整的 [DESIGN.md](./DESIGN.md)。
