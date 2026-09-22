This is an English-language base project. Use English throughout the project, except for original names, and answer user's questions in Chinese.

## Long-Running Task & Progress Reporting Guidelines
- For commands or tasks expected to take non-trivial time (e.g., code scans, test suites, external audits):
  1. Never simply dispatch them to the background and end the turn silently without monitoring.
  2. Actively track progress (via timers, status checks, or synchronous waiting when appropriate) and provide updates.
  3. As soon as the task completes, proactively present the comprehensive results, findings, and next steps to the user without requiring the user to prompt for an update.