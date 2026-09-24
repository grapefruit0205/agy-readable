"""MessageDisplay hook: hide Claude's answer while it streams, then show it rewritten by agy in plainer Korean.

Claude Code runs up to 3 flushes of one message at once, so each delta goes to its own file
(<data dir>/state/<message_id>/<index>.txt) and the final flush joins them in index order.
When agy is slow, fails, or drops code/numbers, the original text is shown with a one-line note.

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

from agy_readable import config, daemon

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_ko.txt")
PART_WAIT = 3.0  # how long the final flush waits for earlier flushes still writing their part
STALE = 3600  # leftover state from aborted messages is removed after this many seconds

FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
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
    """Join parts 0..last, waiting briefly for flushes that started earlier but have not written yet."""
    deadline = time.time() + PART_WAIT
    paths = [os.path.join(mdir, f"{i:06d}.txt") for i in range(last + 1)]
    while True:
        missing = [p for p in paths if not os.path.exists(p)]
        if not missing or time.time() > deadline:
            break
        time.sleep(0.02)
    text = ""
    for p in paths:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                text += f.read()
    return text, len(missing)


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
    if sum(len(m.group(0)) for m in FENCE.finditer(text)) > len(text) * 0.6:
        return "mostly_code"
    return None


def must_keep(text):
    """Code, link targets and numbers that the rewritten text has to carry over unchanged."""
    keep = {m.group(1).strip() for m in FENCE.finditer(text)}
    prose = FENCE.sub("", text)
    keep |= set(re.findall(r"`([^`\n]+)`", prose))
    keep |= set(re.findall(r"\]\(([^)\s]+)\)", prose))
    prose = re.sub(r"(?m)^\s*\d+[.)]\s", "", prose)  # list numbering may legitimately become bullets
    return keep, {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", prose)}


def lost(orig, new):
    keep, nums = must_keep(orig)
    new_nums = {n.replace(",", "") for n in re.findall(r"\d+(?:[.,]\d+)*", new)}
    return sorted(k for k in keep if k not in new) + sorted(nums - new_nums)


def over_budget():
    return f"agy 응답이 {config.TIMEOUT:g}초를 넘음"


def one_shot(prompt, timeout):
    """Returns (agy output or None, reason when None)."""
    cwd = os.path.join(config.data_dir(), "agy_cwd")  # empty, so agy has no files to pick up as context
    os.makedirs(cwd, exist_ok=True)
    try:
        p = subprocess.Popen([config.AGY, "--model", config.MODEL, "--disable-slash-commands", "-p", prompt],
                             cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, start_new_session=True)
    except OSError as e:
        return None, f"agy 실행 실패 ({e.strerror})"
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)  # agy may leave helpers behind; take the whole group down
        p.communicate()
        return None, over_budget()
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
            return None, over_budget() if r.get("timed_out") else r.get("error"), how
        except (OSError, ValueError) as e:
            log(daemon_error=repr(e)[-200:])
    left = config.TIMEOUT - (time.time() - start)
    if left < 5:
        return None, over_budget(), "oneshot"
    return (*one_shot(prompt, left), "oneshot")


def rewrite(text):
    """Returns (rewritten text or None, reason when None, how agy ran)."""
    with open(PROMPT, encoding="utf-8") as f:
        prompt = f.read() + text
    out, why, how = ask_agy(prompt)
    if out is None:
        return None, why, how
    out = out.strip()
    if out.startswith("```") and not text.lstrip().startswith("```"):  # model wrapped the answer in a fence
        out = re.sub(r"^```[^\n]*\n|\n?```$", "", out).strip()
    if not 0.5 <= len(out) / len(text) <= 2.0:
        return None, f"결과 길이가 비정상 ({len(text)}→{len(out)}자)", how
    missing = lost(text, out)
    if missing:
        return None, "코드·숫자 일부가 바뀜: " + ", ".join(m if len(m) <= 30 else m[:30] + "…" for m in missing[:3]), how
    return out, None, how


def finish(event, full, n_missing):
    reason = skip_reason(full)
    if reason:
        log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), outcome="skipped",
            reason=reason, parts_missing=n_missing)
        return full
    if not shutil.which(config.AGY):
        why, refined, how, seconds = f"agy를 찾을 수 없음 ({config.AGY})", None, None, 0.0
    else:
        start = time.time()
        refined, why, how = rewrite(full)
        seconds = round(time.time() - start, 1)
    log(msg=event["message_id"][:8], parts=event.get("index", 0) + 1, chars=len(full), out=len(refined or ""),
        seconds=seconds, via=how, outcome="refined" if refined else "fallback", reason=why, parts_missing=n_missing)
    if refined:
        return refined
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
        full, n_missing = read_parts(mdir, int(event.get("index", 0)))
        shutil.rmtree(mdir, ignore_errors=True)
        prune_stale()
        reply(finish(event, full, n_missing))
    except Exception as e:  # never leave the earlier (hidden) lines off screen
        log(msg=event["message_id"][:8], outcome="error", reason=repr(e)[-300:])
        reply(full if full is not None else event.get("delta", ""))


if __name__ == "__main__":
    main()
