This is an English-language base project. Use English throughout the project, except for original names, and answer user's questions in Chinese.

When analyzing product reviews, always base the analysis on the existing real data in the data folder（You can `git pull` the latest data, but do not execute the code locally to retrieve it）. Include representative examples from the original reviews and provide the percentage of each viewpoint. 

Keep the analysis objective, rigorous, accurate, and reliable.

Explain the analysis methodology and clearly describe how the conclusions are derived from the review data.

Do not mix in personal opinions or draw conclusions beyond what is supported by the reviews.

分析“近期趋势”时，只计算 source == itunes_rss_mostRecent 的纯时序样本，坚决排除 is_most_helpful == True 的历史高赞混杂数据。 

好评统计必须做“脱水”拆解：
区分【名义好评率】与【实质好评率】。在给出好评百分比时，主动剔除 $\le 6$ 字符的无意义刷屏/灌水短评，向用户揭示刷评后的真实口碑。

统一标明“时间窗口天数”而非绝对月份：
不笼统说“8~9月的评价”，而是明确指出：“拼多多样本代表最近 12 天（日均 40 条），得物样本代表最近 34 天（日均 15 条）”。
