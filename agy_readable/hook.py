"""MessageDisplay hook: hide Claude's answer while it streams, then show it rewritten by agy in plainer Korean.

Claude Code runs up to 3 flushes of one message at once, so each delta goes to its own file
(<data dir>/state/<message_id>/<index>.txt) and the final flush joins them in index order. If a part never
arrives, the answer is shown as it is, with a warning where the part is missing: those lines were already
hidden while streaming, so leaving the gap unmarked would lose them silently.

Code, links, URLs and paths never reach agy (see protect.py). agy rewrites the answer freely (headings,
a summary, questions and answers), then, in a second request, checks that rewrite against the original and
fixes only the sentences that add or change something (see review.py). The result is checked before it is
shown. If the second pass fails or its fixes break the checks, the first rewrite is shown when it passed
them itself. A rewrite the checks reject is asked for once more, with what was wrong, while the time left
allows it. When agy is slow or fails, or no attempt passes, the original text is shown with a one-line note.

The last config.KEEP originals and rewrites (rejected ones too) are kept in <data dir>/samples, readable by
the user only, for `agy-readable samples` and `agy-readable diff`. hook.log keeps outcomes, never text.

An answer with sentences in another language (lang.py) is translated into Korean instead: faithfully, with
no second pass, in pieces translated side by side when it is long. A translation the checks still reject after
a retry is shown anyway, with its code, links and paths put back and a one-line warning, never the original;
only a piece agy gives no translation for at all stays as it was.

agy runs through the daemon (agy_readable.daemon), which keeps a pre-started single-use agy waiting;
the first flush of each message starts the daemon if it is down. If the daemon cannot be reached,
a one-shot `agy -p` is used instead.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

from agy_readable import config, daemon, lang, login, proc, protect, review, reviewer

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_ko.txt")
TRANSLATE_PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_translate_ko.txt")
TRANSLATE_RATIO = (0.2, 3.0)  # Korean often takes a third of the characters English does
STALE = 3600  # leftover state from aborted messages is removed after this many seconds
GAP = ("\n\n> ⚠️ agy-readable: 이 자리에 있어야 할 답변 일부를 화면에 표시하지 못했습니다. "
       "Claude가 저장한 답변에는 전체가 있습니다(`/export`로 볼 수 있음).\n\n")

SIGNED_OUT = "Antigravity 로그인 필요"
HANGUL = re.compile(r"[가-힣]")
SEP = "---\n"  # ends the instructions in prompt_ko.txt; the answer follows it
RETRY_MIN_LEFT = 8.0  # seconds; with less left, another attempt would only delay showing the original
REVIEW_MIN_LEFT = 4.0  # seconds; with less left, the second pass is skipped
# what the second pass can put back; any other failure of a rewrite means asking for a new one
FIXABLE = ("숫자가 빠지거나", "원문에 없는 숫자", "코드·링크·경로 일부가 빠짐", "단서가 빠짐", "원문에 없는 코드·링크·경로가 생김")
# what the next attempt is told, by the start of the reason protect.restore gave; first match wins
RETRY_HINTS = [
    ("숫자가 빠지거나", "원문의 숫자는 나온 자리마다 모두, 원문 표기 그대로 남겨라. "
                    "숫자가 든 문장이나 표 칸을 합치거나 빼지 마라."),
    ("단서가 빠짐", "'추정', '확인된 사실' 같은 단서는 그 내용이 나오는 곳마다 빠짐없이 붙여라."),
    ("원문에 없는 숫자", "원문에 없는 숫자를 쓰지 마라. 한글로 쓴 수를 숫자로 바꾸지 말고, 번호를 새로 붙이지 마라."),
    ("원문에 없는 코드", "원문에 없는 코드, 코드 블록, 링크, 경로를 만들지 마라."),
    ("코드 블록", "혼자 한 줄에 있던 ⟦숫자⟧ 표시는 원래 순서대로, 계속 혼자 한 줄에 두어라."),
    ("코드·링크·경로", "⟦숫자⟧ 표시를 하나도 빼거나 겹치지 말고 모두 한 번씩, 모양 그대로 남겨라."),
    ("결과 길이", "원문 내용을 빼거나 늘리지 말고 다시 구성하라."),
]


def log(**fields):
    config.log("hook.log", **fields)


def reply(text):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "MessageDisplay", "displayContent": text}},
                     ensure_ascii=False))


def state_dir():
    return os.path.join(config.data_dir(), "state")


def write_part(mdir, index, text):
    path = os.path.join(mdir, f"{index:06d}.txt")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)  # the final flush never sees a half-written part


def read_parts(mdir, last):
    """Parts 0..last in order (None for one that never came), waiting for flushes that started earlier
    but have not written their part yet."""
    deadline = time.time() + config.PART_WAIT
    paths = [os.path.join(mdir, f"{i:06d}.txt") for i in range(last + 1)]
    while any(not os.path.exists(p) for p in paths) and time.time() < deadline:
        time.sleep(0.02)
    parts = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                parts.append(f.read())
        except FileNotFoundError:
            parts.append(None)
    return parts


def join_parts(parts):
    """The parts in order, with one warning for each run of missing parts."""
    text = ""
    for i, part in enumerate(parts):
        if part is not None:
            text += part
        elif i == 0 or parts[i - 1] is not None:
            text = text.rstrip("\n") + GAP
    return text


def prune_stale():
    now = time.time()
    for name in os.listdir(state_dir()):
        p = os.path.join(state_dir(), name)
        try:
            if now - os.path.getmtime(p) > STALE:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        except OSError:
            pass


def skip_reason(text):
    if len(text) < config.MIN_CHARS:
        return "short"
    if len(text) > config.MAX_CHARS:
        return "long"
    if not HANGUL.search(text):
        return "no_korean"
    if sum(len(m.group(0)) for m in protect.FENCED.finditer(text)) > len(text) * 0.6:
        return "mostly_code"
    return None


def over_budget():
    return f"agy 응답이 {config.TIMEOUT:g}초를 넘음"


def one_shot(prompt, timeout):
    """Returns (agy output or None, reason when None)."""
    cwd = os.path.join(config.data_dir(), "agy_cwd")  # empty, so agy has no files to pick up as context
    os.makedirs(cwd, exist_ok=True)
    try:
        # stdin is a closed pipe: signed out, agy then fails at once instead of waiting 60 s for a sign-in code
        p = subprocess.Popen([proc.agy(), "--model", config.MODEL, "--disable-slash-commands", "-p", prompt],
                             cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace", **proc.detached())
    except OSError as e:
        return None, f"agy 실행 실패 ({e.strerror})"
    p.stdin.close()
    p.stdin = None
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill_tree(p)  # agy may leave helpers behind; take the whole group down
        p.communicate()
        return None, over_budget()
    if p.returncode != 0 and "authentication required" in err:
        return None, SIGNED_OUT
    if p.returncode != 0 or not out.strip():
        log(agy_rc=p.returncode, agy_err=err[-300:])
        return None, f"agy 오류 (종료 코드 {p.returncode})"
    return out, None


def ask_agy(prompt, budget=None, weight=None):
    """Returns (agy output or None, reason when None, how it ran), within `budget` seconds (config.TIMEOUT).
    `weight`: how long the answer should take, as the prompt length the daemon's hedging would expect for it
    (it sends a request that takes much longer to one more agy); the prompt's own length when None."""
    budget = config.TIMEOUT if budget is None else budget
    start = time.time()
    if config.USE_DAEMON and daemon.ensure_running(wait=2.0):
        try:
            r = daemon.call({"op": "ask", "prompt": prompt, "model": config.MODEL, "weight": weight,
                             "timeout": budget - (time.time() - start)}, budget + 5)
            how = ("warm" if r.get("warm") else "cold") + ("+hedge" if r.get("hedges") else "")
            if r.get("ok"):
                return r["text"], None, how
            if r.get("auth_required"):
                return None, SIGNED_OUT, how
            return None, over_budget() if r.get("timed_out") else r.get("error"), how
        except (OSError, ValueError) as e:
            log(daemon_error=repr(e)[-200:])
    left = budget - (time.time() - start)
    if left < 5:
        return None, over_budget(), "oneshot"
    return (*one_shot(prompt, left), "oneshot")


def keep_sample(msg, outcome, attempt, before, after, reason=None, **extra):
    """Keep one original and its rewrite; only the last config.KEEP files stay. A rejected rewrite is kept
    as agy wrote it, placeholders and all, next to the masked original it was asked to rewrite. `extra`: the
    first rewrite and the second pass's answer, masked, when there was one."""
    if config.KEEP <= 0:
        return
    try:
        d = config.samples_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        path = os.path.join(d, f"{int(time.time() * 1000)}-{msg or 'x'}-{attempt}-{outcome}.json")
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
            json.dump({"t": round(time.time(), 3), "msg": msg, "model": config.MODEL, "attempt": attempt,
                       "outcome": outcome, "reason": reason, "masked": outcome != "refined",
                       "before": before, "after": after, **extra}, f, ensure_ascii=False)
        for old in sorted(os.listdir(d))[:-config.KEEP]:
            os.remove(os.path.join(d, old))
    except OSError as e:  # keeping a sample must never keep the answer off screen
        log(sample_error=repr(e)[-200:])


def with_feedback(base, why, did="다듬은", redo="다듬되"):
    """The instructions, plus what the rejected attempt got wrong, before the separator the answer follows."""
    hint = next((h for key, h in RETRY_HINTS if why.startswith(key)), "")
    note = f"직전에 {did} 글은 검사에서 걸려 버려졌다({why}). 처음부터 다시 {redo} 이 문제가 없게 하라. {hint}".rstrip()
    head, sep, tail = base.rpartition(SEP)
    return head + note + "\n" + sep + tail if sep else base + note + "\n"


def ask_reviewer(prompt, budget):
    """The second pass's request: to Claude (reviewer.py) through the daemon, else one-shot; or to agy when
    config.REVIEWER is "agy". Returns (answer or None, reason when None, how it ran)."""
    if config.REVIEWER != "opus":
        out, why, how = ask_agy(prompt, budget, weight=len(prompt) // 4)  # a long prompt, but only a few lines back
        return out, why, how
    start = time.time()
    if config.USE_DAEMON and daemon.ensure_running(wait=2.0):
        try:
            r = daemon.call({"op": "review", "prompt": prompt, "timeout": budget - (time.time() - start)}, budget + 5)
            how = "opus-" + ("warm" if r.get("warm") else "cold")
            return (r["text"], None, how) if r.get("ok") else (None, r.get("error"), how)
        except (OSError, ValueError) as e:
            log(daemon_error=repr(e)[-200:])
    left = budget - (time.time() - start)
    if left < 5:
        return None, "claude 응답 시간 초과", "opus-oneshot"
    return (*reviewer.one_shot(prompt, left), "opus-oneshot")


def second_pass(text, masked, spans, draft, budget, translated=False, ratio=(0.5, 2.0)):
    """The reviewer checks the rewrite `draft` (masked) against the original and fixes what is off (review.py).
    Returns (the fixed text, restored, or None; what happened, for the log; how it ran or None;
    the reviewer's answer, masked, or None)."""
    prompt, lines, us, n = review.build(text, masked, draft, translated, always=config.REVIEWER == "opus")
    if prompt is None:
        return None, "후보 없음", None, None
    out, why, how = ask_reviewer(prompt, budget)
    if out is None:
        return None, f"실패: {why}", how, None
    fixed, done, bad = review.apply(lines, us, protect.clean(out))
    note = " · ".join(f"{k} {v}" for k, v in done.items()) or "고칠 것 없음"
    if bad:
        note += f" · 못 읽은 줄 {len(bad)}"
    if not done:
        return None, note, how, out
    restored, why = protect.restore(text, masked, spans, fixed, ratio)
    if restored is None:
        return None, f"{note} · 고친 글이 검사에 걸림: {why}", how, out
    return restored, note, how, out


def base_prompt(kind):
    """The first pass's instructions: rewrite a Korean answer, translate faithfully, or both at once."""
    with open(TRANSLATE_PROMPT if kind == "translate" else PROMPT, encoding="utf-8") as f:
        base = f.read()
    if kind == "both":
        first = "아래는 코딩 에이전트가 사용자에게 보낸 한국어 답변이다."
        assert first in base
        base = base.replace(first, "아래는 코딩 에이전트가 사용자에게 보낸 답변이다. 한국어가 아닌 문장이 들어 있다. "
                                   "한국어가 아닌 문장, 제목, 표 머리글은 쉬운 우리말로 옮기고, 서비스·제품·명령어·설정·"
                                   "파일 이름과 약어는 원문 표기 그대로 둬라.", 1)
    return base


def rewrite(text, msg="", kind="rewrite", end=None):
    """`kind`: "rewrite" (a Korean answer), "both" (translate and rewrite at once) or "translate" (faithful).
    Returns (text or None, why, how agy ran, retries used, what the second pass did, seconds each request took,
    loose). `loose`: a translation the checks rejected every time, shown anyway (protect.restore_loose) with
    what was wrong as `why`; a Korean answer is never shown loose, the original is.

    A rewrite goes to the second pass when it passed the checks, or failed only on what that pass can put
    back (FIXABLE). One the checks still reject is asked for again, told what was wrong, up to config.RETRIES
    times, while the time left is enough for another attempt as slow as the last; all requests share
    config.TIMEOUT (or `end`). When agy itself fails there is no retry here: the daemon has already raced
    other workers."""
    masked, spans = protect.mask(text)
    base = base_prompt(kind)
    translated = kind != "rewrite"
    ratio = TRANSLATE_RATIO if translated else (0.5, 2.0)
    did, redo = ("옮긴", "옮기되") if kind == "translate" else ("다듬은", "다듬되")
    end = end or time.time() + config.TIMEOUT
    hows, rejected, note, stages, draft, why = [], [], None, [], None, None
    left = lambda: end - time.time()  # noqa: E731
    for attempt in range(max(0, config.RETRIES) + 1):
        began = time.time()
        prompt = with_feedback(base, rejected[-1], did, redo) if rejected else base
        # a free rewrite takes about twice as long as the daemon's default expects for a prompt this size
        out, why, how = ask_agy(prompt + masked, left(), weight=2 * len(prompt + masked))
        hows.append(how)
        stages.append(round(time.time() - began, 1))
        if out is None:
            break
        draft, fixes = protect.clean(out), None
        shown, why = protect.restore(text, masked, spans, draft, ratio)
        if shown is not None or why.startswith(FIXABLE):
            note = "꺼짐" if not config.REVIEW else "시간 부족" if left() < REVIEW_MIN_LEFT else None
            if note is None:
                t = time.time()
                fixed, note, rhow, fixes = second_pass(text, masked, spans, draft, left(), translated, ratio)
                if rhow:
                    hows.append("review:" + rhow)
                    stages.append(round(time.time() - t, 1))
                if fixed is not None:
                    shown, why = fixed, None
        if shown is not None:
            keep_sample(msg, "translated" if translated else "refined", attempt, text, shown, None,
                        draft=draft, review=note, fixes=fixes)
            return shown, None, ",".join(hows), attempt, note, stages, False
        keep_sample(msg, "rejected", attempt, masked, draft, why, review=note, fixes=fixes)
        rejected.append(why)
        if left() < max(RETRY_MIN_LEFT, 1.2 * (time.time() - began)):
            break
    retries = len(rejected) - 1 if out is not None else len(rejected)
    if translated and draft is not None:  # never the original once there is a translation
        return protect.restore_loose(spans, draft), rejected[-1], ",".join(hows), retries, note, stages, True
    if not rejected or not retries or why == SIGNED_OUT:  # finish() offers the sign-in on SIGNED_OUT as is
        return None, why, ",".join(hows), retries, note, stages, False
    if why == rejected[0] and len(set(rejected)) == 1:
        return None, f"{why} (다시 시도해도 같음)", ",".join(hows), retries, note, stages, False
    return None, f"{rejected[0]} · 다시 시도: {why}", ",".join(hows), retries, note, stages, False


def translate(text, msg=""):
    """A long answer, or one mostly code, translated faithfully in pieces (lang.chunks), all at once, sharing
    config.TIMEOUT; each piece gets its own second pass. Returns what rewrite() does, `why` naming the pieces
    that failed or were shown loose."""
    pieces = lang.chunks(text, config.CHUNK_CHARS) if len(text) > config.CHUNK_CHARS else [text]
    end = time.time() + config.TIMEOUT
    results = [None] * len(pieces)

    def run(i):
        try:
            results[i] = rewrite(pieces[i], msg, "translate", end)
        except Exception as e:  # one piece failing must not take the others with it
            results[i] = (None, f"오류 {e!r}"[:200], "", 0, None, [], False)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(pieces))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0.0, end - time.time()) + 10)
    results = [r or (None, over_budget(), "", 0, None, [], False) for r in results]
    how = ",".join(r[2] for r in results if r[2])
    retries, stages = sum(r[3] for r in results), [x for r in results for x in r[5]]
    notes = [r[4] for r in results if r[4]]
    note = (f"{len(pieces)}조각 · " if len(pieces) > 1 else "") + (" / ".join(dict.fromkeys(notes)) or "번역")
    failed = [r for r in results if r[0] is None]
    if len(failed) == len(pieces):
        return None, failed[0][1], how, retries, note, stages, False
    shown = "\n\n".join(r[0] if r[0] is not None else p for p, r in zip(pieces, results))
    loose = [r for r in results if r[6]]
    if failed:
        why = f"일부 번역 실패({len(failed)}/{len(pieces)}조각): {failed[0][1]} · 그 부분은 원문 그대로"
    elif loose:
        why = loose[0][1]
    else:
        why = None
    return shown, why, how, retries, note, stages, bool(loose)


def choose(full):
    """(what to do, or None to show the answer as it is; why it is left, for the log)."""
    if not (config.TRANSLATE and lang.needs_translation(full)):
        reason = skip_reason(full)
        return (None, reason) if reason else ("rewrite", None)
    if len(full) > config.TRANSLATE_MAX:
        return None, "long"
    if len(full) > config.MAX_CHARS or skip_reason(full) == "mostly_code":
        return "translate_pieces", None
    if len(full) < config.MIN_CHARS:
        return "translate", None  # a progress line: translated, not rebuilt
    return "both", None


def finish(event, full):
    mode, reason = choose(full)
    if mode is None:
        log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), outcome="skipped",
            reason=reason)
        return full
    translating = mode != "rewrite"
    if not shutil.which(config.AGY):
        why, refined, how, retries, note, stages, loose = f"agy를 찾을 수 없음 ({config.AGY})", None, None, 0, None, [], False
        seconds = 0.0
    else:
        start = time.time()
        msg = event["message_id"][:8]
        if mode == "translate_pieces":
            refined, why, how, retries, note, stages, loose = translate(full, msg)
        else:
            refined, why, how, retries, note, stages, loose = rewrite(full, msg, mode)
        seconds = round(time.time() - start, 1)
    if refined is None:
        outcome = "fallback"
    elif translating:
        outcome = "translated_loose" if why else "translated"
    else:
        outcome = "refined"
    log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), out=len(refined or ""),
        seconds=seconds, stages=stages, via=how, retries=retries, review=note, mode=mode,
        outcome=outcome, reason=why)
    if refined:
        if why and config.NOTES:  # a translation shown although a check or a piece failed
            if loose and not why.startswith("일부 번역"):
                why = f"번역이 검사에 걸렸지만 번역본 표시: {why}"
            return refined.rstrip() + f"\n\n_(agy-readable: {why})_\n"
        return refined
    if why == SIGNED_OUT:  # the one note shown even with notes off: without it the plugin never works
        r = login.start(auto=True)
        log(login_offer=True, fresh=r.get("fresh"), opened=r.get("opened"), cooldown=r.get("cooldown"),
            error=r.get("error"))
        return full.rstrip() + f"\n\n_(agy-readable: {login.note(r)})_\n"
    skipped = "번역" if translating else "다듬기"
    return full.rstrip() + (f"\n\n_({skipped} 생략: {why} · 원문 표시)_\n" if config.NOTES else "\n")


def main():
    event = json.load(sys.stdin)
    if os.environ.get("AGY_READABLE_INNER"):  # inside the Claude that does the second pass: never recurse
        reply(event.get("delta", ""))  # each piece shown as it came
        return
    mdir = os.path.join(state_dir(), event["message_id"])
    os.makedirs(mdir, exist_ok=True)
    write_part(mdir, int(event.get("index", 0)), event.get("delta", ""))
    if not event.get("final"):
        if event.get("index") == 0 and config.USE_DAEMON and shutil.which(config.AGY):
            try:
                daemon.ensure_running()  # warm up while the answer is still streaming
            except OSError:
                pass
        reply("")
        return
    full = None
    try:
        parts = read_parts(mdir, int(event.get("index", 0)))
        full = join_parts(parts)
        shutil.rmtree(mdir, ignore_errors=True)
        prune_stale()
        missing = [i for i, part in enumerate(parts) if part is None]
        if missing:  # never rewrite a partial answer: show what there is, and where the rest is missing
            log(msg=event["message_id"][:8], parts=len(parts), chars=len(full), outcome="incomplete",
                parts_missing=len(missing), missing_at=missing[:10])
            reply(full)
            return
        reply(finish(event, full))
    except Exception as e:  # never leave the earlier (hidden) lines off screen
        log(msg=event["message_id"][:8], outcome="error", reason=repr(e)[-300:])
        reply(full if full is not None else event.get("delta", ""))


if __name__ == "__main__":
    main()
