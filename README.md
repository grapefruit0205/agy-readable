# agy-readable

**English** · [한국어](README.ko.md)

**Claude's Korean answers, rewritten into Korean you can read at a glance.** Claude Code's answers in Korean are often dense: long sentences, translated-sounding phrasing, the point buried in the middle. agy-readable is a Claude Code plugin that hands each finished answer to [agy](https://antigravity.google/) (the Antigravity CLI) running Gemini 3.8 Flash, which rewrites it into short, plainly structured Korean. Code, links, URLs and file paths never reach agy: they go out as placeholders and are put back byte for byte afterwards. Numbers stay in the text and must come back with the same sign and count, with none added. If a check fails, or agy is slow or fails, you get Claude's original answer instead, with one line saying why. See [What is checked](#what-is-checked) for the details and for what the checks cannot catch.

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

When a rewrite is not used, the original is shown with a note such as `_(다듬기 생략: agy 응답이 40초를 넘음 · 원문 표시)_`. Possible reasons: agy is not signed in (see above), agy took longer than the timeout, agy returned an error (for example a 503 from its backend), agy is not on PATH, or the rewrite failed a check ([What is checked](#what-is-checked)). A failed check is named in the note, for example `코드·링크·경로 일부가 빠짐: …`, `숫자가 빠지거나 바뀜: -3`, `원문에 없는 숫자가 생김: 5` or `결과 길이가 비정상`.

If a piece of the answer never reaches the hook (Claude Code runs up to three display hooks at once, and one can fail), the answer is not rewritten. What did arrive is shown, with a warning where the piece is missing; the full answer is in Claude's transcript and `/export`. The hook waits up to 10 seconds for a late piece before deciding it is missing.

## What is checked

A rewrite is shown only if it passes all of these; otherwise you see the original with a note.

- **Code, links, URLs and file paths are never sent to agy.** Fenced code blocks, inline code, Markdown links, bare URLs and file paths (`/etc/app.conf`, `~/notes.md`, `src/app/main.py`) are replaced by placeholders such as `⟦0⟧` before the answer goes to agy. The rewrite must carry every placeholder exactly once, a code block's alone on its line and code blocks in their original order. The originals are then put back byte for byte, so these parts cannot change; inline ones may move within the text.
- **Numbers** stay in the text, since the sentences are written around them. Every number of the original must come back with its sign and as often as it appeared: `-3` becoming `3`, or one of two `3`s going missing, fails. No number may be added, including a Korean number word turned into digits (세 가지 → 3가지). Dropping a thousands separator (1,024 → 1024) and turning list numbering into bullets are fine.
- **Nothing invented**: no new code block, and no inline code, link, URL or path that is not in the original. Backticks around a word already in the text are fine.
- **Length**: the prose, placeholders left out, stays between half and twice the original.
- An answer that already contains `⟦n⟧` is not rewritten, as the placeholders could not be told apart.

What the checks cannot catch:

- two values of the same kind trading places: "A is 10, B is 20" → "A is 20, B is 10" has the same numbers;
- a sentence whose meaning shifts while every number, code, link and path stays the same.

The prompt forbids both, but nothing verifies it. When the exact wording matters, Claude's original is in `/export` and the transcript. On 2026-09-24, 9 real Claude answers rewritten twice each passed the checks 18 times out of 18.

## How it works

```
Claude streams an answer ──► hook (hooks/run → agy_readable/hook.py)
   each flush: store the new lines, show nothing
   final flush: join the lines, code / links / paths → placeholders
        ──► daemon (unix socket) ──► a pre-started agy, stream-json mode
                                     one answer per agy process, then it exits
   check placeholders / numbers / length, put the originals back ──► rewritten answer, or the original + a note
```

- **Why a daemon.** Starting agy costs 5 to 10 seconds of sign-in, quota and conversation setup, and 30 to 60 seconds when its backend is slow. A small background process (`python3 -m agy_readable.daemon`) keeps one agy started and waiting, so a rewrite takes only the model's own time. The first flush of each answer starts the daemon if it is not running. It exits after 30 minutes without use and takes its agy with it.
- **No context carried over.** Each agy process rewrites exactly one answer and is then shut down, so nothing from one answer can reach the next. A fresh one is started as soon as one is taken.
- **Backend stalls.** agy's backend sometimes stalls one process for 30 to 60 seconds while others are fine. So a request is hedged: it goes to one more agy when no answer has come after about 8 seconds (longer for long answers), or when its agy fails, up to three per answer. A waiting agy that is still not ready after 20 seconds gets another started beside it. The first answer wins; the rest are shut down.
- **If the daemon cannot be reached,** the hook runs a one-shot `agy -p` instead.

Measured on 2026-09-24 with Gemini 3.8 Flash Low, answers of 500 to 2,000 characters: 3 to 6 seconds from the end of Claude's answer to the rewritten one with a waiting agy (occasionally 11), against 10 to 11 seconds for a one-shot `agy -p` and 60 seconds or more during backend stalls. Flash High added about 10 seconds of thinking per answer without a visible gain for this task, so Low is the default.

## Settings

Set at install time, or later with `/plugin` → agy-readable → configure:

| Option | Default | |
|---|---|---|
| `model` | `gemini-3.8-flash-low` | Model agy rewrites with. `agy models` lists them. |
| `timeout` | `40` | Seconds to wait for agy before showing the original. |
| `notes` | `true` | The one-line note under an answer shown unrewritten. |

Environment variables override these and expose a few more: `AGY_READABLE_MODEL`, `AGY_READABLE_TIMEOUT`, `AGY_READABLE_NOTES`, `AGY_READABLE_AGY` (path to agy), `AGY_READABLE_BROWSER` (command that opens the sign-in page; `none` = link only), `AGY_READABLE_MIN_CHARS` (300), `AGY_READABLE_MAX_CHARS` (6000), `AGY_READABLE_DAEMON=0` (a one-shot `agy -p` per answer, nothing left running), `AGY_READABLE_SPARES` (1), `AGY_READABLE_IDLE_EXIT` (1800 s), `AGY_READABLE_HEDGE_AFTER` (8 s), `AGY_READABLE_STUCK_AFTER` (20 s), `AGY_READABLE_PART_WAIT` (10 s, how long to wait for a late piece of the answer).

The rewriting instructions are in [`agy_readable/prompt_ko.txt`](agy_readable/prompt_ko.txt).

## Checking on it

Claude Code puts the plugin's `bin/` on the Bash tool's PATH, so inside a session (or with the full path from a shell):

```
agy-readable status    # settings, agy, sign-in, daemon, the last 10 answers' outcomes
agy-readable login     # sign agy in (in a terminal it asks for the code itself)
agy-readable log 50    # the last 50 hook log lines
agy-readable stop      # stop the daemon and its waiting agy; it restarts with the next answer
```

Logs and temporary files live in the plugin's data directory (`~/.claude/plugins/data/agy-readable-agy-readable/`): `hook.log` and `daemon.log` hold lengths, timings, outcomes and token counts, not the answers themselves (a "changed" note names the code or number that changed, up to 30 characters each). `state/` holds the lines of an answer still being written and is cleared when the answer is done (leftovers from interrupted answers after an hour).

## Cost and privacy

- **Your answers are sent to Google.** Every rewritten answer goes through agy to Google's Gemini service under your Antigravity account and its terms. Don't install this where that is not acceptable.
- **Quota.** agy adds its own system prompt, so a rewrite uses about 14,500 input tokens plus the answer, on your Antigravity quota. A hedged request can use two or three.
- One agy process waits in the background while the daemon runs (up to 30 minutes after the last answer).

## Limits

- Nothing is shown while Claude is still writing; the answer appears when it is complete.
- Korean only: the prompt and the checks are written for Korean answers.
- The checks cannot tell values of the same kind trading places, or a changed meaning ([What is checked](#what-is-checked)).
- Claude's stored answer is unchanged. Copying an answer, `/export` and the transcript give Claude's original text.
- A slow agy delays the answer by up to the timeout (40 s by default) before the original is shown.

## Development

```
python3 -m unittest discover -s tests -v
```

The tests run the hook the way Claude Code does (`sh hooks/run`, event JSON on stdin) against a fake agy in `tests/fakebin`, so they need neither agy nor a sign-in.

## License

MIT
