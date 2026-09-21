# Technical Architecture & Design Document (DESIGN.md)

## 1. Overview & Background

This project provides an automated, zero-cost App Store public data monitoring and archival system hosted entirely within a GitHub repository and executed via free GitHub Actions Runners.

The system tracks two core categories of public data in flat text formats (CSV and Markdown):
1. **User Reviews of Monitored Applications**: Covers key strategic storefronts (e.g., US, CN, GB, DE, JP, KR, MX), performing periodic incremental scraping, real-time sanitization, SHA-256 fingerprint deduplication, and retention management.
2. **App Store Non-Game Rankings Snapshots**: Generated daily at 00:15 CST (16:15 UTC), capturing Top 100 non-game free apps (games strictly excluded) and Top 10 primary category rankings for target regions into Markdown snapshots, maintained for 365 days (1 year).

The architecture strictly adheres to **zero heavyweight dependencies, centralized configuration, high reliability, and minimal runner consumption**.

---

## 2. Core Architecture & Data Flow

```mermaid
flowchart TD
    subgraph GitHub_Actions_Runner [GitHub Actions Free Runner Environment]
        A[Scheduled Triggers\n- Reviews: Every 30 mins\n- Rankings: Daily 00:15 CST] --> B[Read Single Config\nconfig.yaml]
        
        B --> C1[Review Pipeline\nsrc/fetch_reviews.py]
        B --> C2[Rankings Pipeline\nsrc/fetch_rankings.py]
        
        subgraph Review_Pipeline [Review Ingestion Pipeline]
            C1 --> D1[Apple RSS & Web Scraping]
            D1 --> D2[Text Sanitization & Schema Alignment]
            D2 --> D3[SHA-256 Fingerprint Deduplication]
            D3 --> D4[Lifecycle Pruning\n- 180-day window\n- 10k ceiling\n- Permanent helpful protection]
            D4 --> E1[Append / Update CSV\ndata/reviews_{app_id}_{name}_{region}.csv]
        end
        
        subgraph Ranking_Pipeline [Rankings Pipeline]
            C2 --> F1[Lightweight Concurrent Fetch\n- Main Chart Top 100\n- Primary Categories Top 10]
            F1 --> F2[Format Markdown Snapshot\nrankings/{Date}_{Region}.md]
            F2 --> F3[Purge Snapshots > 180 Days]
        end
        
        E1 --> G[Git Change Detection]
        F3 --> G
        G -->|Data Modified| H[Automated Commit & Push]
        G -->|No Changes| I[Silent Exit]
    end
```

---

## 3. Centralized Configuration Design (`config.yaml`)

All system parameters are controlled from a single configuration file:

```yaml
# ==========================================
# App Store Public Data Monitoring Config
# ==========================================

# 1. Global Retention & Lifecycle Rules
retention:
  reviews_days: 365            # Review retention in days (1 year)
  reviews_max_count: 1000000   # Max standard reviews per app (1M)
  keep_all_helpful: true       # Permanently retain all most helpful reviews
  chunk_size_mb: 45            # Max size per chunk in MB (auto-splits into _partN.csv)
  rankings_days: 365           # Ranking snapshot retention in days (1 year)

# 2. Monitored Applications List
monitored_apps:
  - id: "6448311069"
    name: "ChatGPT"
    countries: ["us"]
  - id: "6737597349"
    name: "DeepSeek"
    countries: ["cn", "us"]

# 3. Rankings Snapshot Configuration
rankings:
  regions: ["cn", "us"]        # Monitored storefronts
  exclude_games: true          # Exclude all game apps and subgenres
  main_chart_limit: 100        # Main chart limit (non-game apps only)
  primary_genre_limit: 10      # Top N per primary category
  secondary_genre_limit: 5     # Top N per secondary category
  primary_genres:
    - { id: 6005, name: "Social Networking" }
    - { id: 6007, name: "Productivity" }
    - { id: 6015, name: "Finance" }
    - { id: 6002, name: "Utilities" }
```

---

## 4. Review Monitoring, Sanitization, Deduplication & Retention

### 4.1 Data Schema

Every review record is aligned into the following CSV schema:

| Column | Type | Description |
| :--- | :--- | :--- |
| `fingerprint` | string | **Global Primary Key**: `SHA256(app_id + author + title + content + date)` |
| `review_id` | string | Official App Store review ID (if available) |
| `app_id` | string | Application ID |
| `app_name` | string | Application name |
| `country` | string | Storefront region code (e.g. `cn`, `us`, `gb`) |
| `rating` | integer | Star rating (1 to 5) |
| `title` | string | Sanitized review title |
| `content` | string | Sanitized review body content |
| `original_content` | string | Unmodified raw body content (for auditability) |
| `author` | string | Reviewer handle |
| `version` | string | App version referenced in review |
| `review_date` | string | Standard ISO-8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`) |
| `is_most_helpful` | boolean | Whether review is flagged as featured / most helpful |
| `source` | string | Data origin (`itunes_rss` / `web_ssr`) |
| `is_short` | boolean | Quality flag: ultra-short content |
| `is_spam` | boolean | Quality flag: promotional spam or handles |

### 4.2 Engineering Strategy for Zero Missed Reviews

* **API Constraint**: Apple's public endpoints do not provide a full historical pagination API; the RSS feed dynamically exposes only the latest review sliding window (up to 500 items).
* **Mitigation**:
  1. **Short Polling Window**: Polling is scheduled every 30 minutes via external dispatch. For the vast majority of applications, new reviews arriving within 30 minutes remain well below 500 items, guaranteeing complete coverage.
  2. **Multi-tier Deduplication**: Reviews are indexed by `review_id` and SHA-256 `fingerprint` into in-memory Sets for $O(1)$ duplicate checking.
  3. **Overflow Ceiling Alert**: If an app's new review count in a single cycle touches the 500-review ceiling and additions exceed the threshold (300), the runner raises a GitHub Actions `::warning` annotation and job summary alert.

### 4.3 Lifecycle & Retention Implementation

At the end of each ingestion run, `prune_reviews_data()` runs:
1. **Protection Pool**: Extracts all records where `is_most_helpful == True`. These in-depth reviews are **permanently exempt from cleanup**.
2. **Time Window Filter**: Discards ordinary reviews older than 365 days based on UTC timestamps.
3. **Volume Truncation**: Sorts remaining ordinary reviews by `review_date` descending and retains the top 1,000,000.
4. **Re-merging & Writeback**: Merges protected and retained reviews, writing back to CSV with `UTF-8 with BOM` for Excel compatibility.

### 4.4 Automatic File Chunking & Roll-Over Architecture

To strictly safeguard against GitHub's 50 MB warning threshold and 100 MB push rejection ceiling:
1. **Safety Threshold**: When an application's review CSV reaches or exceeds `chunk_size_mb` (default: 45 MB, configurable in `config.yaml`), the engine triggers an automatic roll-over.
2. **Naming Convention**:
   - Initial state (< 45 MB): `reviews_{app_id}_{name}_{regions}.csv` (fully backward compatible).
   - Once threshold is reached: Base file rolls to `reviews_{app_id}_{name}_{regions}_part1.csv` and is frozen. Active writes continue into `_part2.csv`, `_part3.csv`, etc.
   - Each part is an independent, valid CSV containing the standard UTF-8 BOM and schema headers.
3. **Cross-Chunk Global Deduplication**: `load_all_existing_review_keys()` scans all existing chunks of the target application in memory, guaranteeing global uniqueness across parts.
4. **Multi-Chunk Lifecycle Pruning**: Pruning purges expired ordinary reviews across all parts while preserving all `is_most_helpful` reviews and recycling obsolete trailing parts.

---

## 5. Daily Non-Game Rankings Snapshot Design

### 5.1 Trigger & Timing
* **Schedule**: Daily at 00:15 CST (16:15 UTC previous day).
* **Idempotency**: Snapshots are keyed by CST date (`rankings/YYYY-MM-DD_{region}.md`). Re-running updates the existing file in place with fresh timestamps, never creating duplicate files.

### 5.2 API Selection & Non-Game Filtering
* **Main Chart (Non-Game Top Free)**:
  - Fetched via Apple Media Services Modern Feed API:
    `https://rss.applemarketingtools.com/api/v2/{region}/apps/top-free/100/apps.json`
  - Any app belonging to "Games" (ID: 6014) is filtered out, yielding a pure application ranking (typically 95~98 apps).
* **Primary Category Top 10**:
  - Fetched via iTunes RSS Category endpoint:
    `https://itunes.apple.com/{region}/rss/topfreeapplications/limit=10/genre={genre_id}/json`
  - Restricted to non-game categories (Social Networking, Productivity, Finance, Utilities, Shopping, etc.).
* **Secondary Categories**:
  - Since Apple secondary genres (7001~7019) are game subgenres, secondary genres are defaulted to empty (`secondary_genres: []`).

### 5.3 Snapshot Pruning
Snapshot files older than 365 days are automatically deleted during the daily run to keep repository size lean.

---

## 6. GitHub Actions Execution & Resource Budget

### 6.1 Runner Consumption
* GitHub free personal accounts include **2,000 minutes/month** of `ubuntu-latest`.
* Review monitoring: ~15-20s per run, 48 runs/day, consuming ~400 minutes/month.
* Rankings snapshot: ~15s per run, 1 run/day, consuming ~8 minutes/month.
* Overall usage remains well within free allowances with zero server hosting costs.

### 6.2 Git Commit Automation
Commits are tagged with `[skip ci]` to prevent recursive workflow execution:
```bash
git commit -F .commit_msg || git commit -m "chore(data): auto-update reviews [skip ci]"
git push
```

---

## 7. Trade-offs, Considerations & Recommendations

### 7.1 Git Storage for Time-Series Data
* **Trade-off**: Git is designed for code, not database storage. Frequent updates to large CSV files cause `.git` blob history accumulation over years.
* **Optimization**:
  1. **Strict File Ceiling**: Capping files at 180 days / 10,000 records limits each CSV to 2~4 MB, keeping multi-year repo growth predictable.
  2. **Daily Snapshot Files**: Ranking files are generated once per day without modification, enabling optimal Git packing.
  3. **Future Scalability**: If monitored apps expand into hundreds, data can be decoupled into a dedicated `orphan` branch (e.g. `data-storage`).

### 7.2 Concurrency & Network Overhead
* **Optimization**: Uses Python's built-in `concurrent.futures.ThreadPoolExecutor` with 4 worker threads for category scraping. Complete ranking retrieval finishes in 5-8 seconds without third-party asynchronous frameworks, maintaining zero bloat.
