"""MessageDisplay hook: hide Claude's answer while it streams, then show it rewritten by agy in plainer Korean.

Claude Code runs up to 3 flushes of one message at once, so each delta goes to its own file
(<data dir>/state/<message_id>/<index>.txt) and the final flush joins them in index order. If a part never
arrives, the answer is shown as it is, with a warning where the part is missing: those lines were already
hidden while streaming, so leaving the gap unmarked would lose them silently.

Code, links, URLs and paths never reach agy (see protect.py), and the rewrite is checked before it is
shown. When agy is slow or fails, or the check fails, the original text is shown with a one-line note.

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

from agy_readable import config, daemon, login, protect

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_ko.txt")
STALE = 3600  # leftover state from aborted messages is removed after this many seconds
GAP = ("\n\n> ⚠️ agy-readable: 이 자리에 있어야 할 답변 일부를 화면에 표시하지 못했습니다. "
       "Claude가 저장한 답변에는 전체가 있습니다(`/export`로 볼 수 있음).\n\n")

SIGNED_OUT = "Antigravity 로그인 필요"
HANGUL = re.compile(r"[가-힣]")


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


def ask_agy(prompt):
    """Returns (agy output or None, reason when None, how it ran)."""
    start = time.time()
    if config.USE_DAEMON and daemon.ensure_running(wait=2.0):
        try:
            r = daemon.call({"op": "ask", "prompt": prompt, "model": config.MODEL,
                             "timeout": config.TIMEOUT - (time.time() - start)}, config.TIMEOUT + 5)
            how = ("warm" if r.get("warm") else "cold") + ("+hedge" if r.get("hedges") else "")
            if r.get("ok"):
                return r["text"], None, how
            if r.get("auth_required"):
                return None, SIGNED_OUT, how
            return None, over_budget() if r.get("timed_out") else r.get("error"), how
        except (OSError, ValueError) as e:
            log(daemon_error=repr(e)[-200:])
    left = config.TIMEOUT - (time.time() - start)
    if left < 5:
        return None, over_budget(), "oneshot"
    return (*one_shot(prompt, left), "oneshot")


def rewrite(text):
    """Returns (rewritten text or None, reason when None, how agy ran)."""
    masked, spans = protect.mask(text)
    if masked is None:
        return None, "원문에 ⟦숫자⟧ 표기가 있어 보호할 수 없음", None
    with open(PROMPT, encoding="utf-8") as f:
        prompt = f.read() + masked
    out, why, how = ask_agy(prompt)
    if out is None:
        return None, why, how
    refined, why = protect.restore(text, masked, spans, out)
    return refined, why, how


def finish(event, full):
    reason = skip_reason(full)
    if reason:
        log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), outcome="skipped",
            reason=reason)
        return full
    if not shutil.which(config.AGY):
        why, refined, how, seconds = f"agy를 찾을 수 없음 ({config.AGY})", None, None, 0.0
    else:
        start = time.time()
        refined, why, how = rewrite(full)
        seconds = round(time.time() - start, 1)
    log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), out=len(refined or ""),
        seconds=seconds, via=how, outcome="refined" if refined else "fallback", reason=why)
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
