# agy-readable

**English** · [한국어](README.ko.md)

**Claude's Korean answers, rewritten into Korean you can read at a glance.** Claude Code's answers in Korean are often dense: long sentences, translated-sounding phrasing, the point buried in the middle. agy-readable is a Claude Code plugin that hands each finished answer to [agy](https://antigravity.google/) (the Antigravity CLI) running Gemini 3.8 Flash, which rebuilds it into plain Korean with a clear structure: headings, a short summary, questions and answers. A free rewrite reads better but can slip in a reason or a stronger word the original never had, so agy is asked twice: first to rewrite freely, then to check that rewrite against the original sentence by sentence and fix only what is off ([The second pass](#the-second-pass)). Code, links, URLs and file paths never reach agy: they go out as placeholders and are put back byte for byte afterwards. Numbers, and marks such as `추정` (estimate) and `확인된 사실` (verified), stay in the text; none of the original's may go missing, and no number may be added. If a check fails, or agy is slow or fails, you get Claude's original answer instead, with one line saying why. See [What is checked](#what-is-checked) for the details and for what the checks cannot catch.

It changes only what you see. The conversation Claude keeps, and its context for the next turn, is Claude's original text.

## Requirements

- Claude Code **2.1.280 or newer** (the `MessageDisplay` hook), in a terminal or the desktop app
- **Linux or macOS**. Windows is not supported (the helper process uses unix sockets); there the hook stays out of the way and answers are shown unchanged.
- **Python 3.8 or newer** as `python3` or `python`
- **agy (the Antigravity CLI) installed**, on your PATH. It has to be signed in to your own Antigravity account; if it is not, agy-readable asks you to sign in from inside Claude Code ([Signing in](#signing-in-to-antigravity)). agy is headless: it signs in with the OAuth token Antigravity keeps in your OS keyring. agy-readable only runs the `agy` on your PATH. It never reads, copies or stores credentials, and it ships with none: every user runs on their own sign-in and their own quota.

## Install

```
claude plugin marketplace add grapefruit0205/agy-readable
claude plugin install agy-readable@agy-readable
```

or inside Claude Code: `/plugin marketplace add grapefruit0205/agy-readable`, then `/plugin install agy-readable@agy-readable`. Start a new session afterwards.

- Update: `claude plugin marketplace update agy-readable`, then `claude plugin update agy-readable@agy-readable`.
- Turn off for a while: `claude plugin disable agy-readable@agy-readable`. Remove: `claude plugin uninstall agy-readable@agy-readable`.

## Signing in to Antigravity

agy-readable uses agy's own sign-in and never handles passwords or tokens. If agy is not signed in:

1. The next answer appears as Claude wrote it, with a note: *Antigravity 로그인이 필요합니다…*. The Google sign-in page opens in your browser (macOS, and Linux with a desktop); the note carries the link too.
2. Sign in and allow access. The page then shows a code starting with `4/`.
3. Paste the code by itself into Claude Code's input box and press Enter, **within 60 seconds** of the note (agy's own limit). agy-readable takes it out of the prompt, so it is not sent to Claude, hands it to agy, and replies *Antigravity 로그인이 끝났습니다*. From the next answer on, answers are rewritten.

If agy is slow to start and has not printed its sign-in URL within 10 seconds, the note says the sign-in is still starting and asks you to type `/agy-readable:login` a little later; the attempt keeps running and the command picks it up. If the 60 seconds run out or the code is wrong, it says so and opens a fresh sign-in page. An ignored offer is not reopened by answers for 10 minutes; start again any time by typing `/agy-readable:login` in the input box. Without a browser on this machine (over SSH, for example), running `agy` once in a terminal signs in the same way.

How it works: agy offers its sign-in only when its input is a terminal, so the daemon runs `agy -p ok` on a pseudo-terminal, shows the URL agy prints, and types the pasted code into that terminal. agy exchanges the code and stores its token in the OS keyring as usual; the one-off `ok` request is its only model call. Claude Code shows a blocked input back to you as "Original prompt"; the code is single-use and tied to that sign-in attempt, so it is worthless afterwards. Set `AGY_READABLE_BROWSER=none` to only show the link, or to the command that should open it.

## What you see

While Claude is writing, the answer is hidden. When it is done, the rewritten answer appears in one piece. Answers are left exactly as Claude wrote them when they are:

- shorter than 300 or longer than 6,000 characters,
- not in Korean,
- mostly code (more than 60% inside code fences).

When a rewrite is not used, the original is shown with a note such as `_(다듬기 생략: agy 응답이 60초를 넘음 · 원문 표시)_`. Possible reasons: agy is not signed in (see above), agy took longer than the timeout, agy returned an error (for example a 503 from its backend), agy is not on PATH, or the rewrite failed a check, retry included ([What is checked](#what-is-checked)). A failed check is named in the note, for example `코드·링크·경로 일부가 빠짐: …`, `숫자가 빠지거나 바뀜: -3`, `원문에 없는 숫자가 생김: 5`, `단서가 빠짐: 추정` or `결과 길이가 비정상`.

A rewrite that fails a check is not given up on at once. A missing number, placeholder or mark goes to the second pass to be put back, and an invented number, path or code to be taken out; otherwise, or if it is still missing, agy is told what was wrong (for example `숫자가 빠지거나 바뀜: 4`) and asked for the rewrite again from the start, when the time left within the timeout is at least what the rejected attempt took. With less, the original is shown straight away rather than after a wait. If the second attempt fails too, the note says so: `숫자가 빠지거나 바뀜: 4 (다시 시도해도 같음)` or `… · 다시 시도: agy 응답이 60초를 넘음`.

If a piece of the answer never reaches the hook (Claude Code runs up to three display hooks at once, and one can fail), the answer is not rewritten. What did arrive is shown, with a warning where the piece is missing; the full answer is in Claude's transcript and `/export`. The hook waits up to 10 seconds for a late piece before deciding it is missing.

## The second pass

A free rewrite can add a reason, an example or a stronger word ("perfectly", "best practice") the original never had. Asked simply to "fix the mistakes", agy finds few of its own. So the machine points at the suspects first:

1. The rewrite is cut into numbered sentences.
2. Each sentence with a word, a number, or code-looking text the original lacks goes on a **suspect list**, with the words. So does a sentence that repeats a claim the original marks as `추정` or `확인된 사실` without the mark, as a summary tends to (two neighbouring words shared with the marked sentence or the one before it). Numbers, placeholders and marks the rewrite lost are listed too, each with the original sentences they were in, those the rewrite did not keep first.
3. agy gets the original, the numbered rewrite and the list, and answers with only the lines to change: `[12] fixed sentence`, `[12] (삭제)` (delete), `[12+] sentence to add after 12`. It does not write the whole answer again, so this pass is quick.
4. The fixed text must pass every check below. If the second pass fails, or its fixes break a check, the first rewrite is shown when it passed the checks itself.

Words new to the original are only candidates: most are the plainer words the rewrite was asked for, and agy is told to leave those alone when they mean the same.

Measured on 2026-09-25 on a free rewrite made by Gemini in one go, with 12 places that said something the original did not and 5 marks dropped (one-shot `agy -p`, Flash Low, one run each):

| The second pass was given | Time | Marks | Invented numbers | Sentences that changed the meaning |
|---|---|---|---|---|
| the suspect list, words included | 10.1 s | 5/5 back | 2/2 fixed | 5 of 10 fixed |
| numbers and marks only | 10.2 s | 5/5 back | 2/2 fixed | 1 of 10 fixed |

What it missed were all sentences wrong in wording only (an exaggeration, a wrong technical term), with no number or mark involved. The same day, the original rebuilt from scratch with a first version of the current prompt had 15 of 140 sentences on the suspect list, and the second pass found nothing to fix.

## What is checked

A rewrite is shown only if it passes all of these. A missing number, placeholder or mark, or an invented number, path or code, is sent to the second pass to be fixed; any other failure has agy told what failed and asked once more, and if that fails too you see the original with a note.

- **Code, links, URLs and file paths are never sent to agy.** Fenced code blocks, inline code, Markdown links, bare URLs and file paths (`/etc/app.conf`, `~/notes.md`, `src/app/main.py`) are replaced by placeholders such as `⟦0⟧` before the answer goes to agy. The rewrite must carry every placeholder; a code block's exactly once, alone on its line and in the original order, while an inline one may appear again (in a summary, say). The originals are then put back byte for byte, so these parts cannot change; inline ones may move within the text. Capital-letter acronyms or Korean words joined by slashes (`TCP/IP`, `HTTP/HTTPS/TLS`, `입력/출력`) are prose. Paths and URLs may contain Korean (`~/문서/설정.json`). A particle written right after an English name or an extension (`README를`, `설정.json에`) stays in the sentence; after a Korean name (`~/문서에`) it cannot be told from the name, so it is protected along with the path.
- **Numbers** stay in the text, since the sentences are written around them. Every number of the original must come back with its sign and at least as often as it appeared: `-3` becoming `3`, or one of two `3`s going missing, fails; repeating one in a summary is fine. No number may be added, including a Korean number word turned into digits (세 가지 → 3가지). Dropping a thousands separator (1,024 → 1024), turning list numbering into bullets and numbering headings (`## 1.`) are fine, and so is dropping a section number from a heading (`### 3장: 설계`), since a free rewrite regroups the sections.
- **Marks of certainty**: `추정`, `확인된 사실`, `확인한 사실` and `일반적인` must appear at least as often as in the original, so that a guess is not turned into a fact.
- **Nothing invented**: no new code block, and no inline code, link, URL or path that is not in the original. Backticks around a word already in the text are fine.
- **Length**: the prose, placeholders left out, stays between half and twice the original.
- A `⟦n⟧` already in the answer (one about this plugin, say) is protected like code, so it is never taken for a placeholder.

What the checks cannot catch:

- two values of the same kind trading places: "A is 10, B is 20" → "A is 20, B is 10" has the same numbers;
- a number dropped in one place while the same number appears again elsewhere, in a summary for instance;
- a mark dropped from a claim repeated elsewhere (a summary) while the original place keeps it. The second pass is pointed at such sentences, but the machine finds them by shared words, so not always;
- a sentence whose meaning shifts while every number, code, link, path and mark stays the same. The second pass looks for these, but as the table above shows, it does not catch them all;
- a relative path with one slash and no extension (`docs/README`, `docs/초안`): it looks like `TCP/IP` or `입력/출력`, so it stays in the text, where only its numbers are checked.

When the exact wording matters, Claude's original is in `/export` and the transcript.

## How it works

```
Claude streams an answer ──► hook (hooks/run → agy_readable/hook.py)
   each flush: store the new lines, show nothing
   final flush: join the lines, code / links / paths → placeholders
        ──► daemon (unix socket) ──► a pre-started agy, stream-json mode
                                     one answer per agy process, then it exits
   ① a free rewrite
   ② suspect sentences pointed out, only the lines to fix come back (a second request, another agy)
   check placeholders / numbers / marks / length ──► on failure, ask ① once more with what was wrong (if time allows)
   put the originals back ──► rewritten answer, or the original + a note
```

- **Why a daemon.** Starting agy costs 5 to 10 seconds of sign-in, quota and conversation setup, and 30 to 60 seconds when its backend is slow. A small background process (`python3 -m agy_readable.daemon`) keeps one agy started and waiting, so a rewrite takes only the model's own time. The first flush of each answer starts the daemon if it is not running. It exits after 30 minutes without use and takes its agy with it.
- **No context carried over.** Each agy process rewrites exactly one answer and is then shut down, so nothing from one answer can reach the next. A fresh one is started as soon as one is taken.
- **Backend stalls.** agy's backend sometimes stalls one process for 30 to 60 seconds while others are fine. So a request is hedged: it goes to one more agy when no answer has come after about 8 seconds (longer for long answers and for the free rewrite), or when its agy fails, up to three per request. A waiting agy that is still not ready after 20 seconds gets another started beside it. The first answer wins; the rest are shut down.
- **If the daemon cannot be reached,** the hook runs a one-shot `agy -p` instead.

**Time.** Two passes take roughly one and a half to four times as long as the sentence-level polish of 0.3.x. Measured on 2026-09-25, one-shot `agy -p`, Flash Low, both passes together (free rewrite + second pass):

| Answer | This version | Earlier 0.4.0 drafts |
|---|---|---|
| 792 characters | 18.1 s (10.1 + 8) | 27.1 s, 26.8 s |
| 2,176 characters | 16.4 s (9.7 + 6.7) | 35.7 s, 37.7 s |
| 5,354 characters | 19.2 s (13.2 + 6) | 48.1 s, 57.8 s |

The earlier drafts' slower runs came mostly from retries for checks this version has relaxed (a heading's section number, `HTTP/HTTPS/TLS` taken for a path); one free rewrite took 7.7 to 26 s, one second pass 6 to 19.1 s, so times vary widely from run to run. With a waiting agy, 0.3.x polished answers under 1,000 characters in 4.7 s and 2,500 to 5,000 characters in 8.2 s (medians of `hook.log`); one-shot, it polished the 5,354-character answer in 13.8 s. A waiting agy saves a few seconds over one-shot, and races a second one when an answer is slow. Flash High took 43.7 s and 84.4 s just to polish that answer, too slow for this; Low is the default.

## Settings

Set at install time, or later with `/plugin` → agy-readable → configure:

| Option | Default | |
|---|---|---|
| `model` | `gemini-3.8-flash-low` | Model agy rewrites with. `agy models` lists them. |
| `timeout` | `60` | Seconds to wait for agy, both passes together, before showing the original. |
| `review` | `true` | The second pass (checking the rewrite against the original). Off is faster but leaves anything the rewrite added. |
| `notes` | `true` | The one-line note under an answer shown unrewritten. |
| `retries` | `1` | How many times a rewrite that fails a check is asked for again, with what was wrong. `0` = show the original at once. |
| `keep` | `20` | How many originals and rewrites to keep for comparing. `0` = keep none. |

Environment variables override these and expose a few more: `AGY_READABLE_MODEL`, `AGY_READABLE_TIMEOUT`, `AGY_READABLE_REVIEW`, `AGY_READABLE_NOTES`, `AGY_READABLE_RETRIES`, `AGY_READABLE_KEEP`, `AGY_READABLE_AGY` (path to agy), `AGY_READABLE_BROWSER` (command that opens the sign-in page; `none` = link only), `AGY_READABLE_MIN_CHARS` (300), `AGY_READABLE_MAX_CHARS` (6000), `AGY_READABLE_DAEMON=0` (a one-shot `agy -p` per answer, nothing left running), `AGY_READABLE_SPARES` (1), `AGY_READABLE_IDLE_EXIT` (1800 s), `AGY_READABLE_HEDGE_AFTER` (8 s), `AGY_READABLE_STUCK_AFTER` (20 s), `AGY_READABLE_PART_WAIT` (10 s, how long to wait for a late piece of the answer).

The instructions are in [`agy_readable/prompt_ko.txt`](agy_readable/prompt_ko.txt) (the free rewrite) and [`agy_readable/prompt_review_ko.txt`](agy_readable/prompt_review_ko.txt) (the second pass).

## Checking on it

Claude Code puts the plugin's `bin/` on the Bash tool's PATH, so inside a session (or with the full path from a shell):

```
agy-readable status    # settings, agy, sign-in, daemon, the last 10 answers' outcomes
agy-readable login     # sign agy in (in a terminal it asks for the code itself)
agy-readable log 50    # the last 50 hook log lines
agy-readable samples   # kept originals and rewrites, newest first
agy-readable diff 1    # the lines the rewrite of sample 1 changed
agy-readable stop      # stop the daemon and its waiting agy; it restarts with the next answer
```

Logs and temporary files live in the plugin's data directory (`~/.claude/plugins/data/agy-readable-agy-readable/`): `hook.log` and `daemon.log` hold lengths, timings, outcomes and token counts, not the answers themselves (a "changed" note names the code or number that changed, up to 30 characters each). `state/` holds the lines of an answer still being written and is cleared when the answer is done (leftovers from interrupted answers after an hour). `samples/` holds the last 20 (`keep`) originals with their rewrites, the free rewrite (`draft`), what the second pass did (`review`) and its answer (`fixes`), rejected ones included as agy wrote them (placeholders and all), readable by you only (directory 700, files 600); the oldest go first. `agy-readable samples` and `diff` show them.

## Cost and privacy

- **Your answers are sent to Google.** Every rewritten answer goes through agy to Google's Gemini service under your Antigravity account and its terms. Don't install this where that is not acceptable.
- **Quota.** agy adds its own system prompt, so each request uses about 14,500 input tokens plus the text sent, on your Antigravity quota. An answer takes two requests, and the second carries the original and the rewrite. A hedged request can use two or three.
- A rewrite asked for again after a failed check uses quota once more.
- **What stays on this computer.** The last 20 originals and rewrites stay in `samples/`. Set `keep` to `0` to keep none.
- One agy process waits in the background while the daemon runs (up to 30 minutes after the last answer).

## Limits

- Nothing is shown while Claude is still writing; the answer appears when it is complete.
- Korean only: the prompt and the checks are written for Korean answers.
- The checks cannot tell values of the same kind trading places, or a changed meaning; the second pass fixes some of the latter, not all ([What is checked](#what-is-checked)).
- Claude's stored answer is unchanged. Copying an answer, `/export` and the transcript give Claude's original text.
- Two passes make the answer appear two to three times later than a plain polish would. A slow agy delays it by up to the timeout (60 s by default) before the original is shown.

## Development

```
python3 -m unittest discover -s tests -v
```

The tests run the hook the way Claude Code does (`sh hooks/run`, event JSON on stdin) against a fake agy in `tests/fakebin`, so they need neither agy nor a sign-in.

## License

MIT
