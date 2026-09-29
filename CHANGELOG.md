# Changelog

## 0.6.1 — 2026-09-29

- **agy and the second pass get the conversation so far** (`context.py`): your messages and the answers Claude showed, newest first, up to 10,000 characters, each message cut to 2,000, read from the session transcript the hook input names. Tool calls and their output, system reminders, a compacted conversation's summary, subagents' messages and Claude Code's own `<synthetic>` lines never go; code, links and paths in it are masked as `[코드]` (not numbered, so they cannot pass for the answer's placeholders). It goes before the instructions, with a note to use it only for understanding and never to add what is only in it; the checks still hold the rewrite to the answer. Reading this session's 10.6 MB transcript took 0.16 s. `context_chars` option / `AGY_READABLE_CONTEXT_CHARS` (10000, 0 = none), `AGY_READABLE_CONTEXT_MESSAGE_CHARS` (2000). The log records `context` (characters sent).
- **The second pass runs on the conversation's model** by default: `review_model` option / `AGY_READABLE_REVIEW_MODEL` is `main`, the model of the last answer in the transcript, else Opus; any `claude --model` name sets another. The daemon's waiting Claude follows the model of the last request. The log records `review_model`. `reviewer` is now `claude` (`opus` still works); `via` says `claude-warm` / `claude-cold` / `claude-oneshot`.
- The second pass also fixes sentences that end in the plain form (~다) to the polite form (~습니다); a translation sometimes came back in the plain form and passed.
- A number an English original spells as a word (`one minute`, `the sixth run`, `twice`) may be written in digits: "약 1분" was rejected as a new number, and "약 one분" was shown instead.
- Measured on 2026-09-29 with the daemon and real agy (Flash Low) and Claude (Opus 5.5), one run each: a 362-character Korean answer 8.0 s with the conversation, 8.2 s without; a 437-character English answer 8.4-10.8 s with it, 7.1-8.8 s without. The difference is within the spread between runs.

## 0.6.0 — 2026-09-29

- **Claude (Opus) does the second pass.** agy rewrites, Claude checks the rewrite against the original sentence by sentence and answers with only the lines to change, and the checks run on the result as before. Claude reads every rewrite, not only those with suspects. It runs headless with the Claude Code sign-in already on the computer: `claude -p --model opus --effort low`, no tools, no settings sources (so no plugins, hooks or MCP servers, and this plugin's hook cannot run inside it; `AGY_READABLE_INNER` makes sure), no CLAUDE.md, no auto-memory, no session saved, a short system prompt of its own, an empty working directory (`reviewer.py`). The daemon keeps one such Claude waiting in stream-json mode, like agy's spares: on 2026-09-29 a warm Claude answered in 3.4-4.6 s, against about 5 s for a fresh `claude -p`. If Claude fails or is signed out, the first rewrite is shown when it passed the checks. `reviewer` option / `AGY_READABLE_REVIEWER` (`opus`, or `agy` for the old second pass), `AGY_READABLE_REVIEW_MODEL` (`opus`), `AGY_READABLE_REVIEW_EFFORT` (`low`), `AGY_READABLE_CLAUDE` (path).
- **Answers in other languages are translated into Korean.** Until now an answer with no Hangul was shown as it was (`no_korean`), and so was any answer under 300 characters, so Claude's English progress lines and English answers reached the screen untranslated: on 2026-09-29, 577 short answers and 3 with no Korean went by unchanged.
  - An answer counts as another language when any sentence, table cell or heading has no Hangul and either three or more Latin words with an everyday function word (`the`, `is`, `que`, `und`, ...) or two or more letters of another script. Code, links and paths are masked first; bare names (`CloudFront + WAF`, `Security & Access`) and commands written without backticks stay in the Korean rewrite (`lang.py`).
  - 300 to 6,000 characters: translated and rebuilt in one agy request (the Korean sentences are rebuilt too), then checked by Claude. Under 300 (a progress line): translated faithfully, then checked. Over 6,000, or mostly code: cut at blank lines outside code blocks into pieces of about 4,000 characters, translated faithfully side by side and each checked, all within the one timeout; up to 40,000 characters.
  - Claude is told the rewrite is a translation, and its suspect list leaves out new Korean words (all of them are new); numbers, code and marks are still listed. The length may be a fifth to three times the original's.
  - Never the original once agy has written a translation. One that still fails a check after its retry is shown with its code, links and paths put back (one whose placeholder was lost is added at the end) and a one-line note. Only a piece agy wrote nothing for (signed out, timeout, error) stays as it was.
  - `translate` option / `AGY_READABLE_TRANSLATE` (on), `AGY_READABLE_CHUNK_CHARS` (4000), `AGY_READABLE_TRANSLATE_MAX` (40000). The log records `mode` (`rewrite`, `both`, `translate`, `translate_pieces`) and the outcomes `translated` and `translated_loose`; `agy-readable status` shows them as 번역 and 번역(경고).
- Measured on 2026-09-29 with the daemon (Flash Low, then Opus): a 61-character English line 10.7 s (agy cold 7.3 + Opus 3.4), a 634-character English answer with a table and code 7.5 s (3.8 + 3.7), a Korean answer with one English sentence 8.5 s (3.9 + 4.6).
- Tests: a fake `claude` in `tests/fakebin`, passed to the hook by its full path, so no test reaches the real Claude Code.

## 0.5.0 — 2026-09-29

- Windows is supported. Until now the hook did nothing there and answers were shown as Claude wrote them.
  - The daemon listens on TCP at 127.0.0.1 where Python has no unix sockets. Its port and a random token are in `<data dir>/daemon.addr`, and a request without the token is dropped unanswered. A file left by a daemon that was killed is recognised by its pid, so the next answer does not wait about 2 s for Windows to refuse the connection.
  - The daemon's lock uses `msvcrt.locking`. agy runs in its own process group with a hidden console, so no console window pops up, and is stopped with `taskkill /T`. agy is started by its full path (`agy.exe` or whatever `agy` resolves to on PATH).
  - agy's output is read as UTF-8, and `hooks/run` and `bin/agy-readable` set `PYTHONUTF8=1`, so Korean is not garbled through the code page (cp949). They also try `py` after `python3` and `python`, and run from the plugin root, so `python -m` finds the package even if Git Bash's `/c/...` PYTHONPATH does not reach Python.
  - Sign-in: Windows has no pseudo-terminal in the standard library, so agy cannot be signed in from inside Claude Code. The note under an answer and `/agy-readable:login` say to run `agy` once in a terminal instead. A pasted code is kept from Claude and not used.
  - Needs Git for Windows: Claude Code runs hooks in Git Bash, and without it in PowerShell, where the hooks cannot run.
- Tests run on Windows too (agy faked by an `agy.exe` wrapper), and on Linux over TCP (`AGY_READABLE_TCP=1`).

## 0.4.1 — 2026-09-25

- An answer that already has `⟦n⟧` in it (one explaining this plugin, say) is rewritten too. Until now it was shown as it was, with `원문에 ⟦숫자⟧ 표기가 있어 보호할 수 없음`. Such a `⟦n⟧` is now protected like code: set aside before the placeholders are made, put back inside any code that holds it, and given a placeholder of its own in prose. Placeholders are put back in one pass, so a `⟦n⟧` inside restored code is left as it is.
- The second pass no longer adds a code block the rewrite already has. Its numbered rewrite left out lines holding only a placeholder, so agy took the answer's code blocks for missing and put them in again, and the doubled blocks failed the check (`고친 글이 검사에 걸림: 코드·링크·경로 자리 표시가 중복되거나 바뀜`); the first rewrite was shown, without the second pass's other fixes. Such lines now appear in place, unnumbered, the prompt says they are already there, and a code block the rewrite has is not inserted again. One that is really missing goes on its own line.

## 0.4.0 — 2026-09-25

The point of the plugin is an answer you can read. Polishing sentence by sentence left most answers 92-99% unchanged, so the answer is now rebuilt freely (headings, a short summary, questions and answers), and a second request has agy check that rewrite against the original and fix what it added.

- Two passes. The first rewrites freely; its prompt still forbids new facts, reasons, sources and exaggeration, new table columns, and dropped hedges. The second gets the original, the rewrite cut into numbered sentences and a suspect list (per sentence: words, numbers and code-looking text the original lacks, and a claim the original marks `추정` or `확인된 사실` repeated without the mark; overall: numbers, placeholders and marks the rewrite lost, with the original sentences that lost them first), and answers with only the lines to change (`[n] …`, `[n] (삭제)`, `[n+] …`). If it fails, or its fixes break a check, the first rewrite is shown when it passed the checks itself. Blank lines and horizontal rules left behind by a deleted block are tidied away. The free rewrite is told not to use HTML tags (`<br>` in a table cell) and not to add a summary when the original already has one; the second pass is told to keep a summary made only of the original's content. Measured on 2026-09-25 on a one-go Gemini rewrite with 12 known slips and 5 dropped marks: 10.1 s, all marks and invented numbers fixed, 5 of the 10 meaning slips; without the word list, 1 of 10. `review` option / `AGY_READABLE_REVIEW` (on).
- Checks fit a free rewrite: a number must appear at least as often as in the original (a summary may repeat it; none may be added), an inline placeholder may appear again (a code block's still exactly once, alone and in order), heading numbers (`## 1.`) and section numbers in headings (`### 3장`) are left out like list numbers, words joined by slashes in capitals (`HTTP/HTTPS/TLS`) are prose, not a path, and marks of certainty (`추정`, `확인된 사실`, `확인한 사실`, `일반적인`) must not be dropped (`단서가 빠짐: 추정`). A lost number, placeholder or mark, or an invented number, path or code, goes to the second pass first; only if the fixed text still fails is the rewrite asked for again.
- Default timeout 40 → 60 s, for both passes together. Measured one-shot with Flash Low, both passes: 792 characters 18.1 s, 2,176 characters 16.4 s, 5,354 characters 19.2 s, no retries; earlier drafts of 0.4.0 took up to 57.8 s, mostly on retries for checks since relaxed.
- The daemon's `ask` takes an optional `weight` (prompt characters' worth of expected time) for hedging: the free rewrite counts double, the second pass half the answer, so a slow but normal rewrite is not raced by a second agy.
- A rewrite that fails a check is asked for once more, with what was wrong (`숫자가 빠지거나 바뀜: 4` and a hint). Only when the time left within the timeout is at least what the rejected attempt took; otherwise the original shows at once. All five fallbacks logged on 2026-09-24/25 were number checks. `retries` option / `AGY_READABLE_RETRIES` (1; 0 = off). The log records `retries`, `review` and `stages` (seconds per request, shown by `agy-readable status` and `log` as `27.1+6.3s`); a second failure is noted as `… (다시 시도해도 같음)` or `… · 다시 시도: …`.
- The last 20 originals and rewrites, rejected ones included, are kept in `<data dir>/samples` (directory 700, files 600) with the free rewrite (`draft`), what the second pass did (`review`) and its answer (`fixes`), so a rewrite can be compared with what Claude wrote. `agy-readable samples` lists them, `agy-readable diff [N]` shows what changed. `keep` option / `AGY_READABLE_KEEP` (20; 0 = none). `hook.log` still holds no text.

## 0.3.1 — 2026-09-24

- Paths and URLs with Korean in them are protected whole. 0.3.0 stopped at the first Korean character: in `/home/user/문서/설정.json` only `/home/user/` was protected, and the rest could change unnoticed.
- A Korean particle right after an English name or an extension (`README를`, `설정.json에서`) is left to the sentence; after a Korean name (`~/문서에`) it is protected with the path, as the two cannot be told apart.
- Korean word lists with slashes (`빌드/테스트/배포`, `입력/출력`) are not taken for paths.

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
