This is an English-language base project. Use English throughout the project, except for original names, and answer user's questions in Chinese.

## Long-Running Task & Progress Reporting Guidelines
- For commands or tasks expected to take non-trivial time (e.g., code scans, test suites, external audits):
  1. Never simply dispatch them to the background and end the turn silently without monitoring.
  2. Actively track progress (via timers, status checks, or synchronous waiting when appropriate) and provide updates.
  3. As soon as the task completes, proactively present the comprehensive results, findings, and next steps to the user without requiring the user to prompt for an update.

  When analyzing product reviews, always base the analysis on the existing real data in the data folder. Include representative examples from the original reviews and provide the percentage of each viewpoint. 

Keep the analysis objective, rigorous, accurate, and reliable.

Explain the analysis methodology and clearly describe how the conclusions are derived from the review data.

Do not mix in personal opinions or draw conclusions beyond what is supported by the reviews.

分析“近期趋势”时，只计算 source == itunes_rss_mostRecent 的纯时序样本，坚决排除 is_most_helpful == True 的历史高赞混杂数据。 

好评统计必须做“脱水”拆解：
区分【名义好评率】与【实质好评率】。在给出好评百分比时，主动剔除无意义字符、重复刷评、无意义刷屏/灌水短评，向用户揭示刷评后的真实口碑。

统一标明“时间窗口天数”而非绝对月份：
不笼统说“8~9月的评价”，而是明确指出：“拼多多样本代表最近 12 天（日均 40 条），得物样本代表最近 34 天（日均 15 条）”。

报告必须包含【分析方法与过程简述】：
后续给出任何数据分析报告时，必须在报告结尾，简要通俗说明所采用的方法与分析过程（包括：样本切片与时间窗口标定规则、好评脱水与噪音清洗逻辑、核心标签/维度的自底向上挖掘方法及归因步骤），确保分析过程透明、逻辑严谨且可验证复现。

表格纵向展示，方便阅读和对比