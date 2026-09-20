# App Store Public Data Monitoring & Archival System (App Store Public Data)

An automated App Store public data monitoring and archival system powered by free GitHub Actions Runners. Persistently records monitored app user reviews and daily non-game ranking snapshots in CSV and Markdown formats, with all data synced directly to the GitHub repository.

---

## Key Features

- **Incremental Review Monitoring**: Periodically polls monitored apps for new reviews. Employs `SHA-256` content fingerprinting for instant deduplication, ensuring zero missed reviews.
- **Data Cleaning & Quality Flagging**: Automatically sanitizes invisible control characters and excess blank lines, normalizes timestamps to ISO-8601 UTC, and flags ultra-short or promotional spam content.
- **Lifecycle & Retention Management**:
  - Reviews are retained for **180 days (half a year)** by default;
  - Standard reviews are capped at **10,000 latest entries** per application;
  - **All "Most Helpful" in-depth reviews are permanently preserved** and exempt from expiration cleanup.
- **Daily Midnight Non-Game Rankings Snapshot**: Automatically scrapes Top 100 free apps (games strictly filtered) and Top 10 apps across primary non-game categories for China and United States every day at 00:00 CST / 16:00 UTC, outputting clean Markdown snapshots retained for 180 days.
- **Centralized Configuration**: All monitored apps, regions, category codes, and retention periods are managed in [`config.yaml`](./config.yaml).
- **Zero-Cost Operation**: Runs entirely on free GitHub Actions Runners with zero cloud server expenses, consuming under 5% of monthly free runner minutes.

---

## Directory Structure

```text
appstore-public-data/
├── .github/
│   └── workflows/
│       ├── monitor_reviews.yml    # Review monitoring workflow (every 30 mins with flood detection)
│       └── snapshot_rankings.yml  # Daily rankings snapshot workflow
├── config.yaml                    # Global unified configuration (central control)
├── DESIGN.md                      # Detailed technical architecture and design document
├── README.md                      # Project documentation
├── main.py                        # Unified CLI entrypoint for local and runner execution
├── src/
│   ├── __init__.py
│   ├── pipeline.py                # Cleaning, fingerprint deduplication, and pruning engine
│   ├── fetch_reviews.py           # Review scraping and incremental ingestion module
│   └── fetch_rankings.py          # Rankings scraping and snapshot generation module
├── data/                          # Review data storage directory
│   └── reviews_{app_id}_{name}_{region}.csv # Application reviews (UTF-8 with BOM, Excel-friendly)
└── rankings/                      # Daily rankings snapshots (retained for 180 days)
    ├── 2026-09-19_cn.md           # China non-game main chart Top 100 + primary categories Top 10
    ├── 2026-09-19_us.md           # US non-game main chart Top 100 + primary categories Top 10
    └── ...
```

---

## Quickstart & Configuration

### 1. Unified Configuration (`config.yaml`)

Edit `config.yaml` in the repository root to customize monitored applications, target regions, and retention policies:

```yaml
# Data retention rules
retention:
  reviews_days: 180            # Review retention in days
  reviews_max_count: 10000     # Max standard reviews per app
  keep_all_helpful: true       # Permanently preserve most helpful reviews
  rankings_days: 180           # Rankings snapshot retention in days

# Monitored applications list
monitored_apps:
  - id: "6670324846"
    name: "Grok AI"
    countries: ["us", "cn"]
  - id: "6760173601"
    name: "Muse from Meta"
    countries: ["us", "cn"]

# Rankings configuration (games strictly excluded)
rankings:
  regions: ["cn", "us"]        # Target regions
  exclude_games: true          # Filter out all game apps and subcategories
  main_chart_limit: 100        # Main chart limit (non-game apps only)
  primary_genre_limit: 10      # Top N per primary category
  secondary_genre_limit: 5     # Top N per secondary category
  secondary_genres: []         # Secondary categories list
```

### 2. Local Testing & Execution

Built with a minimal dependency footprint, requiring only Python 3.8+ and `PyYAML`:

```bash
# 1. Install dependencies
pip install pyyaml

# 2. Run review scraping and incremental ingestion
python3 main.py --mode reviews

# 3. Run ranking snapshot generation
python3 main.py --mode rankings

# 4. Run complete pipeline (reviews + rankings + lifecycle pruning)
python3 main.py --mode all
```

---

## Automated Scheduling Mechanism

To eliminate high latency and dropped execution risks common to native GitHub Actions Cron triggers, the system utilizes **Cloudflare Workers (Cron Triggers)** for external high-precision dispatch paired with **GitHub Actions (`workflow_dispatch`)**:

1. **Review Monitoring** (`monitor_reviews.yml`):
   - **Schedule**: Triggered every 30 minutes on schedule;
   - **Logic**: Ingests new reviews and deduplicates using SHA-256 fingerprints into CSV. If a single run reaches the 500-review ceiling (page 10 full) with high new volume, it automatically triggers GitHub Actions `::warning` annotations and `$GITHUB_STEP_SUMMARY` alert boxes to flag potential overflow.
2. **Rankings Snapshot** (`snapshot_rankings.yml`):
   - **Schedule**: Triggered daily at 00:15 CST (16:15 UTC);
   - **Logic**: Scrapes non-game main chart and primary category rankings for China and the US, outputs formatted Markdown, and purges snapshots older than 180 days.
   - **Idempotency Guarantee**: Snapshot files are keyed by date (`rankings/YYYY-MM-DD_{region}.md`). If triggered multiple times on the same date, the system overwrites the file in place with updated timestamps and rankings, **never generating duplicate files or dirty records**.

Whenever data changes occur, the workflow creates an automated `git commit` and pushes back to the repository.

> [!NOTE]
> For in-depth architectural details, API comparisons, Git storage optimization, and concurrency design, refer to [DESIGN.md](./DESIGN.md).
