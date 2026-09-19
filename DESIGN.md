# App Store 公开数据存储与监控系统技术设计方案 (DESIGN.md)

## 1. 概述与背景

本项目旨在构建一个完全托管于 GitHub 仓库、依托免费 GitHub Actions Runner 运行的 App Store 公开数据监控与自动化沉淀系统。

系统通过纯文本（CSV 和 Markdown）格式，自动化跟踪两类核心数据：
1. **被监控应用的用户评价**：默认覆盖中美核心大区（US/CN），增量抓取、实时清洗、指纹去重、生命周期留存管理；
2. **App Store 纯应用榜单快照**：每日北京时间零点（UTC 16:00），生成中美两区（CN/US）的纯应用主榜单（Top 100，自动剔除所有游戏）、纯应用一级品类（Top 10）的 Markdown 状态快照，彻底排除所有游戏相关内容。

整个系统坚持**零重度外部依赖、配置高度集中、高稳定性、节约 Runner 资源**的原则设计。

---

## 2. 核心架构与数据流图

```mermaid
flowchart TD
    subgraph GitHub_Actions_Runner [GitHub Actions 免费 Runner 环境]
        A[定时触发调度\n- 评价: 每 30 分钟 (48次/天)\n- 榜单: 每天 00:00] --> B[读取单一配置文件\nconfig.yaml]
        
        B --> C1[评价监控引擎\nsrc/fetch_reviews.py]
        B --> C2[榜单快照引擎\nsrc/fetch_rankings.py]
        
        subgraph Review_Pipeline [评价处理管道]
            C1 --> D1[Apple RSS & Web 抓取]
            D1 --> D2[文本清洗 & 字段标准化]
            D2 --> D3[SHA256 内容指纹去重]
            D3 --> D4[生命周期裁剪\n- 180天保留\n- 1w条上限\n- mostHelpful 永久保护]
            D4 --> E1[追加/更新 CSV\ndata/reviews_{app_id}.csv]
        end
        
        subgraph Ranking_Pipeline [榜单处理管道]
            C2 --> F1[轻量并发拉取\n- 主榜 Top 100\n- 一级品类 Top 10\n- 二级品类 Top 5]
            F1 --> F2[格式化为快照 MD\nrankings/{Date}_{Region}.md]
            F2 --> F3[自动清理 >180 天历史快照]
        end
        
        E1 --> G[Git 变更检测]
        F3 --> G
        G -->|有新增数据| H[自动 commit & push 仓库]
        G -->|无变化| I[静默结束]
    end
```

---

## 3. 统一配置文件设计 (`config.yaml`)

全系统所有参数在单个配置文件中统一管控，便于随时调整监控范围与策略：

```yaml
# ==========================================
# App Store 公开数据监控配置
# ==========================================

# 1. 全局数据保留与生命周期规则
retention:
  reviews_days: 180            # 评价默认保留天数（半年）
  reviews_max_count: 10000     # 单个应用普通最新评价上限
  keep_all_helpful: true       # 永久保留所有标记为“最有帮助”的评价（不受过期与1w条剔除）
  rankings_days: 180           # 榜单快照默认保留天数（半年）

# 2. 监控应用列表 (评价监控)
monitored_apps:
  - id: "6670324846"
    name: "Grok AI"
    countries: ["us", "cn"]
  - id: "6760173601"
    name: "Muse from Meta"
    countries: ["us", "cn"]

# 3. 榜单快照配置 (每天北京时间 00:00 执行)
rankings:
  # 监控地区列表 (默认仅抓取中国和美国，支持按需扩展)
  regions:
    - "cn"
    - "us"

  # 游戏过滤策略 (默认开启：排除所有游戏类应用及子品类)
  exclude_games: true

  # 抓取深度限制
  main_chart_limit: 100        # 主榜单前 N 名 (纯应用榜，自动过滤游戏)
  primary_genre_limit: 10      # 一级品类前 N 名 (默认 10)
  secondary_genre_limit: 5     # 二级品类前 N 名 (默认 5)

  # 一级品类配置 (Primary Genres，已剔除游戏 6014，专注纯应用领域)
  primary_genres:
    - { id: 6005, name: "社交 (Social Networking)" }
    - { id: 6007, name: "效率 (Productivity)" }
    - { id: 6015, name: "财务 (Finance)" }
    - { id: 6002, name: "工具 (Utilities)" }
    - { id: 6008, name: "摄影与录像 (Photo & Video)" }
    - { id: 6016, name: "娱乐 (Entertainment)" }
    - { id: 6017, name: "教育 (Education)" }
    - { id: 6012, name: "生活 (Lifestyle)" }
    - { id: 6009, name: "新闻 (News)" }
    - { id: 6024, name: "购物 (Shopping)" }
    - { id: 6013, name: "健康健美 (Health & Fitness)" }
    - { id: 6000, name: "商务 (Business)" }

  # 二级品类配置 (Secondary Genres)
  # 因排除所有游戏相关内容，原游戏子品类(动作、RPG、休闲等 7001~7019)全部剔除，默认不抓取游戏二级品类
  secondary_genres: []
```

---

## 4. 评价监控、清洗、去重与生命周期管理

### 4.1 数据规范 (Schema)

每条评价记录在 CSV 中对齐为以下标准字段：

| 字段名 | 类型 | 说明 |
| :--- | :--- | :--- |
| `fingerprint` | string | **全局主键**：`SHA256(app_id + author + title + content + date)` |
| `review_id` | string | 官方评价 ID（若有） |
| `app_id` | string | 目标应用 ID |
| `app_name` | string | 应用名称 |
| `country` | string | 所在商店国家代码（如 `cn`, `us`） |
| `rating` | integer | 评分（1 ~ 5 星） |
| `title` | string | 清洗后的评价标题 |
| `content` | string | 清洗后的评价正文 |
| `author` | string | 评论者昵称 |
| `version` | string | 评价对应的 App 版本号 |
| `review_date` | string | 标准 ISO-8601 UTC 时间（`YYYY-MM-DDTHH:MM:SSZ`） |
| `is_most_helpful` | boolean | 是否为官方标记/推荐的“最有帮助”评价 |
| `source` | string | 数据来源渠道（`itunes_rss` / `web_ssr`） |
| `is_short` | boolean | 质量标记：内容过短（≤ 2字符） |
| `is_spam` | boolean | 质量标记：疑似广告引流 |

### 4.2 保证“评价不漏”的工程策略

* **客观事实**：Apple 官方公开接口并无真正的“全量历史分页接口”，RSS 接口仅动态暴露最新发生的评价窗口。
* **解法机制**：
  1. **缩短轮询窗口**：GitHub Actions 调度设为每 2~3 小时轻量轮询一次。对于绝大部分应用，2~3 小时内的新增评价远低于单次拉取窗口（50~500条），确保增量窗口覆盖率 100%。
  2. **毫秒级指纹比对**：每次运行时预加载该应用 CSV 首列 `fingerprint` 集合进内存 Set。比对操作为 $O(1)$，新记录直接追加，已存记录直接忽略。
  3. **指数退避重试**：当遭遇网络抖动或苹果边缘节点临时阻断时，自动重试 3 次，防止单次调度漏抓。

### 4.3 生命周期与留存规则实现

每次抓取完成后，执行裁剪维护函数 `prune_reviews_data()`：
1. **提取保护池**：筛选出所有 `is_most_helpful == True` 的记录。此部分为高价值深度反馈，**永久豁免任何清理**。
2. **时间窗过滤**：计算基准日期，对于普通评价，过滤掉距离当前超过 180 天的记录。
3. **数量上限截断**：将剩余的普通评价按 `review_date` 降序排列，保留最新的不超过 10,000 条。
4. **重新合并与写回**：将保护池记录与普通评价合并，采用 `UTF-8 with BOM` 编码写回 CSV（确保 Excel 打开不乱码），并同步更新 Markdown 摘要。

---

## 5. 每日零点榜单快照设计

### 5.1 时间与触发
* **触发时间**：每天北京时间 00:00。对应 GitHub Actions 的 UTC 时间为前一天的 **16:00**（Cron: `0 16 * * *`）。

### 5.2 数据来源选型与游戏过滤机制
* **主榜单 (纯应用)**：
  - 采用 Apple Media Services 现代 Feed API：
    `https://rss.applemarketingtools.com/api/v2/{region}/apps/top-free/100/apps.json`
  - **API 深度与游戏过滤说明**：Apple 官方公开 Top Free Feed 单次物理硬上限为 100 款。解析时自动识别 `genres` 或 `primaryGenreName` 剔除所有属于 "Games" (ID: 6014) 的项目，生成纯粹的应用生态排行（通常为 95~98 款应用）。目录导航锚点与标题根据实际数量动态对应，杜绝失效。
* **一级品类 Top 10**：
  - 采用 iTunes Genre API：
    `https://itunes.apple.com/{region}/rss/topfreeapplications/limit=10/genre={genre_id}/json`
  - 严格聚焦纯应用领域（社交、效率、财务、工具、摄影与录像、生活、娱乐、教育、新闻、商务等），彻底不请求游戏分类。
* **二级品类 Top 5 处理**：
  - 在 Apple 官方分类体系中，二级细分品类（7001~7019）绝大多数为游戏专属（动作、RPG、益智等）。依据“所有游戏相关不抓取”原则，系统默认将二级品类置空（`secondary_genres: []`），不再拉取任何游戏细分子榜，既净化数据又节省 Runner 耗时。

### 5.3 快照文件格式 (`rankings/YYYY-MM-DD_{region}.md`)
生成的快照文件包含元信息头和格式化的 Markdown 表格：
* 快照生成时间（北京时间 & UTC）
* 榜单地区（默认 `cn` 与 `us`）
* 🏆 **纯应用 Top 100 免费榜**（已剔除游戏，包含排名、App 名称、开发者、分类、App Store 链接）
* 📂 **纯应用一级品类 Top 10**（按社交、财务、效率等应用大类分章节展示）

### 5.4 榜单生命周期清理
在生成当天快照后，遍历 `rankings/` 目录：
* 解析文件名中的日期（`YYYY-MM-DD`）；
* 删除早于 180 天前的历史快照文件，保持仓库体积精简。

---

## 6. GitHub Actions 运行与存储策略

### 6.1 Runner 资源评估
* GitHub 免费个人账号每月提供 **2,000 分钟**的 `ubuntu-latest` 运行时间。
* 评价监控：每次运行耗时约 **15~20 秒**，每天 8 次，每月约消耗 **80 分钟**。
* 榜单快照：每次运行耗时约 **20~30 秒**，每天 1 次，每月约消耗 **15 分钟**。
* **总消耗 < 100 分钟/月**，仅占免费额度的 5%，极度充裕且零成本。

### 6.2 工作流拆分设计
1. **`monitor_reviews.yml`**：专注于评价的高频轮询增量追加。
2. **`snapshot_rankings.yml`**：专注于北京时间零点的榜单快照生成与历史文件清理。
3. **自动化提交**：
   ```bash
   git config --local user.email "github-actions[bot]@users.noreply.github.com"
   git config --local user.name "github-actions[bot]"
   git add data/ rankings/
   git commit -m "chore(data): auto-update reviews and rankings [skip ci]" || exit 0
   git push
   ```
   *带 `[skip ci]` 标记，避免触发任何不必要的递归工作流。*

---

## 7. 客观利弊分析与优化解法 (更优解建议)

遵循大道至简与客观求真的原则，梳理该体系的关键利弊与优化策略：

### 7.1 Git 仓库存储时序数据的问题与优化
* **弊端**：Git 的底层是为源代码设计的，而非数据库。高频变更同一个 CSV 文件（每 30 分钟轮询，有新数据即 commit）会在 `.git` 中沉淀大量快照 blob，导致长期下来 `git clone` 速度变慢。
* **优化解法**：
  1. **严格限制单文件上限**：设定 180 天与 10,000 条上限，使单应用 CSV 大小锁定在 2~3MB 以内，半年物理体积增长可控在几十 MB；
  2. **榜单按天分文件**：榜单快照每天独立生成一个文件，历史文件不修改只新增，Git 能高效压缩，过期后直接删除；
  3. **进阶建议（未来可选）**：若监控应用扩充到数十款，建议建立专门的 `orphan` 数据分支（如 `data-storage` 分支），与代码主分支彻底解耦。

### 7.2 榜单并发请求与网络开销优化
* **弊端**：多地区（如 cn, us, jp）配合数十个品类，若采用同步串行请求，需要发起 50+ 次 HTTP 请求，耗时 1 分钟以上，且易因单次超时拖累全局。
* **优化解法**：使用 Python 内置的 `concurrent.futures.ThreadPoolExecutor`，限制 3~5 个轻量并发线程。整体榜单拉取可缩短到 5~8 秒内完成，且不需要第三方重型异步框架（如 asyncio/aiohttp），兼顾极简与高效。
