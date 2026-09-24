# Changelog

## 0.2.0 — 2026-09-24

- Sign-in from inside Claude Code. When agy is not signed in to Antigravity, the answer shows a sign-in note and the Google sign-in page opens in the browser; the code the page shows, pasted into the prompt, is taken by a `UserPromptSubmit` hook (it never reaches the model) and typed into agy's own sign-in, which stores the token as usual. Wrong or late codes start a fresh attempt. `/agy-readable:login` and `agy-readable login` start one on demand.
- A signed-out agy no longer costs retries: the daemon stops hedging, starts no spares, and checks again every minute. A one-shot `agy -p` gets a closed pipe as stdin, so a signed-out agy fails at once instead of waiting 60 s for a code.
- `AGY_READABLE_BROWSER`: the command that opens the sign-in page, or `none`.

## 0.1.0 — 2026-09-24

First release.

- `MessageDisplay` hook: hides a Korean answer while it streams, then shows it rewritten by agy (Gemini 3.8 Flash Low by default).
- Checks that code blocks, inline code, link targets and numbers survive the rewrite, and that the length stays within half to twice the original; otherwise shows the original with a one-line note.
- Background daemon with pre-started, single-use agy workers (no context carried between answers), hedging against backend stalls, idle exit after 30 minutes. Falls back to a one-shot `agy -p`.
- Plugin options `model`, `timeout`, `notes`; `AGY_READABLE_*` environment overrides.
- `agy-readable status | log | stop`.
