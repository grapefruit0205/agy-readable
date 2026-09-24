# Changelog

## 0.1.0 — 2026-09-24

First release.

- `MessageDisplay` hook: hides a Korean answer while it streams, then shows it rewritten by agy (Gemini 3.8 Flash Low by default).
- Checks that code blocks, inline code, link targets and numbers survive the rewrite, and that the length stays within half to twice the original; otherwise shows the original with a one-line note.
- Background daemon with pre-started, single-use agy workers (no context carried between answers), hedging against backend stalls, idle exit after 30 minutes. Falls back to a one-shot `agy -p`.
- Plugin options `model`, `timeout`, `notes`; `AGY_READABLE_*` environment overrides.
- `agy-readable status | log | stop`.
