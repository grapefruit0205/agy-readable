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

agy runs through the daemon (agy_readable.daemon), which keeps a pre-started single-use agy waiting;
the first flush of each message starts the daemon if it is down. If the daemon cannot be reached,
a one-shot `agy -p` is used instead.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

from agy_readable import config, daemon, login, protect, review

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_ko.txt")
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
        p = subprocess.Popen([config.AGY, "--model", config.MODEL, "--disable-slash-commands", "-p", prompt],
                             cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    except OSError as e:
        return None, f"agy 실행 실패 ({e.strerror})"
    p.stdin.close()
    p.stdin = None
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)  # agy may leave helpers behind; take the whole group down
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


def with_feedback(base, why):
    """The instructions, plus what the rejected attempt got wrong, before the separator the answer follows."""
    hint = next((h for key, h in RETRY_HINTS if why.startswith(key)), "")
    note = f"직전에 다듬은 글은 검사에서 걸려 버려졌다({why}). 처음부터 다시 다듬되 이 문제가 없게 하라. {hint}".rstrip()
    head, sep, tail = base.rpartition(SEP)
    return head + note + "\n" + sep + tail if sep else base + note + "\n"


def second_pass(text, masked, spans, draft, budget):
    """agy checks its rewrite `draft` (masked) against the original and fixes what is off (review.py).
    Returns (the fixed text, restored, or None; what happened, for the log; how agy ran or None;
    agy's answer, masked, or None)."""
    prompt, lines, us, n = review.build(text, masked, draft)
    if prompt is None:
        return None, "후보 없음", None, None
    out, why, how = ask_agy(prompt, budget, weight=len(masked) // 2)  # a long prompt, but only a few lines back
    if out is None:
        return None, f"실패: {why}", how, None
    fixed, done, bad = review.apply(lines, us, protect.clean(out))
    note = " · ".join(f"{k} {v}" for k, v in done.items()) or "고칠 것 없음"
    if bad:
        note += f" · 못 읽은 줄 {len(bad)}"
    if not done:
        return None, note, how, out
    restored, why = protect.restore(text, masked, spans, fixed)
    if restored is None:
        return None, f"{note} · 고친 글이 검사에 걸림: {why}", how, out
    return restored, note, how, out


def rewrite(text, msg=""):
    """Returns (rewritten text or None, reason when None, how agy ran, retries used, what the second pass did,
    seconds each request took).

    A rewrite goes to the second pass when it passed the checks, or failed only on what that pass can put
    back (FIXABLE). One the checks still reject is asked for again, told what was wrong, up to config.RETRIES
    times, while the time left is enough for another attempt as slow as the last; all requests share
    config.TIMEOUT. When agy itself fails there is no retry here: the daemon has already raced other workers."""
    masked, spans = protect.mask(text)
    if masked is None:
        return None, "원문에 ⟦숫자⟧ 표기가 있어 보호할 수 없음", None, 0, None, []
    with open(PROMPT, encoding="utf-8") as f:
        base = f.read()
    start, hows, rejected, note, stages = time.time(), [], [], None, []
    left = lambda: config.TIMEOUT - (time.time() - start)  # noqa: E731
    for attempt in range(max(0, config.RETRIES) + 1):
        began = time.time()
        prompt = with_feedback(base, rejected[-1]) if rejected else base
        # a free rewrite takes about twice as long as the daemon's default expects for a prompt this size
        out, why, how = ask_agy(prompt + masked, left(), weight=2 * len(prompt + masked))
        hows.append(how)
        stages.append(round(time.time() - began, 1))
        if out is None:
            break
        draft, fixes = protect.clean(out), None
        shown, why = protect.restore(text, masked, spans, draft)
        if shown is not None or why.startswith(FIXABLE):
            note = "꺼짐" if not config.REVIEW else "시간 부족" if left() < REVIEW_MIN_LEFT else None
            if note is None:
                t = time.time()
                fixed, note, rhow, fixes = second_pass(text, masked, spans, draft, left())
                if rhow:
                    hows.append("review:" + rhow)
                    stages.append(round(time.time() - t, 1))
                if fixed is not None:
                    shown, why = fixed, None
        if shown is not None:
            keep_sample(msg, "refined", attempt, text, shown, None, draft=draft, review=note, fixes=fixes)
            return shown, None, ",".join(hows), attempt, note, stages
        keep_sample(msg, "rejected", attempt, masked, draft, why, review=note, fixes=fixes)
        rejected.append(why)
        if left() < max(RETRY_MIN_LEFT, 1.2 * (time.time() - began)):
            break
    retries = len(rejected) - 1 if out is not None else len(rejected)
    if not rejected or not retries or why == SIGNED_OUT:  # finish() offers the sign-in on SIGNED_OUT as is
        return None, why, ",".join(hows), retries, note, stages
    if why == rejected[0] and len(set(rejected)) == 1:
        return None, f"{why} (다시 시도해도 같음)", ",".join(hows), retries, note, stages
    return None, f"{rejected[0]} · 다시 시도: {why}", ",".join(hows), retries, note, stages


def finish(event, full):
    reason = skip_reason(full)
    if reason:
        log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), outcome="skipped",
            reason=reason)
        return full
    if not shutil.which(config.AGY):
        why, refined, how, retries, note, stages = f"agy를 찾을 수 없음 ({config.AGY})", None, None, 0, None, []
        seconds = 0.0
    else:
        start = time.time()
        refined, why, how, retries, note, stages = rewrite(full, event["message_id"][:8])
        seconds = round(time.time() - start, 1)
    log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), out=len(refined or ""),
        seconds=seconds, stages=stages, via=how, retries=retries, review=note,
        outcome="refined" if refined else "fallback", reason=why)
    if refined:
        return refined
    if why == SIGNED_OUT:  # the one note shown even with notes off: without it the plugin never works
        r = login.start(auto=True)
        log(login_offer=True, fresh=r.get("fresh"), opened=r.get("opened"), cooldown=r.get("cooldown"),
            error=r.get("error"))
        return full.rstrip() + f"\n\n_(agy-readable: {login.note(r)})_\n"
    return full.rstrip() + (f"\n\n_(다듬기 생략: {why} · 원문 표시)_\n" if config.NOTES else "\n")


def main():
    event = json.load(sys.stdin)
    if os.name == "nt":  # the daemon needs unix sockets; printing nothing leaves the answer as Claude wrote it
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
