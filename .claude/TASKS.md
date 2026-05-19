# Roadmap: Gabagent Self-Improvements

## Priority 1: Crash Recovery (Foundational)
- [ ] **Global Exception Handler**: Implement in `__main__.py` to catch unhandled exceptions, capture `sys.exc_info()`, and trigger `postmortem_log`.
- [ ] **Startup Crash Detection**: Check `.cache/gabagent/crashes/` (or similar) on startup. Prompt user to review/report if a previous crash is detected.

## Priority 2: Performance & Maintenance
- [ ] **Performance Monitoring**: Track and log execution times for individual tool calls to identify slow bottlenecks.
- [ ] **Memory Health**: Automatically aggregate and summarize dated memory entries into an `archive/` summary to maintain context window efficiency.
