# Changelog

## 0.3.0 — 2026-09-24

Fixes from a review of 0.2.0.

- Code, links, URLs and file paths are kept out of the model's hands instead of being checked afterwards: they are replaced by placeholders before the answer goes to agy and put back byte for byte. The rewrite must carry every placeholder exactly once, code blocks alone on their line and in order. Plain file paths are now protected too; 0.2.0 checked only code and link targets, by substring.
- Numbers are compared with their sign and count, and added numbers are rejected. 0.2.0 compared sets, so `-3` → `3`, a dropped repeated number, or a number replaced by one found elsewhere in the answer passed.
- New code blocks, and inline code, links or paths not in the original, are rejected.
- A streamed piece of the answer that never arrives no longer vanishes: the hook waits up to 10 s for it (was 3 s), then shows what arrived with a warning where the piece is missing, and does not rewrite the partial answer. `AGY_READABLE_PART_WAIT` sets the wait.
- Sign-in: an agy that has not printed its sign-in URL 10 s after starting is no longer taken for signed in. The note says the sign-in is still starting; `/agy-readable:login` picks the attempt up.
- Prompt: explains the placeholders and keeps Korean number words as words (한 번 is not 1회). 9 real answers rewritten twice each passed the checks 18 times out of 18.
- Documented what the checks cannot catch: values of the same kind trading places, and meaning shifts without any number, code, link or path changing.

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
