"""The conversation so far, for agy and the second pass: what was asked and answered before the answer being
rewritten, so a term or a step the answer refers back to reads as it was meant.

Only the conversation text goes: the user's messages and the answers Claude showed. Tool calls and their
output (file contents, command output), system reminders, a compacted conversation's summary and subagents'
messages never do. Code, links and paths in it are masked like the answer's (protect.mask), but replaced by
a plain [코드] instead of a numbered placeholder, so they cannot be mistaken for the answer's own.

It is read from the session's transcript (the hook input's transcript_path), newest first, up to
config.CONTEXT_CHARS characters; each message is cut to config.CONTEXT_MESSAGE_CHARS. The same pass finds the
model the main conversation runs on, for the second pass when config.REVIEW_MODEL is "main".
"""
import json
import os
import re

from agy_readable import config, protect

SCAN = 16 * 1024 * 1024  # bytes read from the end of the transcript at most
TAGGED = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+|task-notification|"
                    r"ci-monitor-event|artifact-content-authored-by-others)\b[^>]*>.*?</\1>|"
                    r"<[a-z-]+(?:-[a-z]+)*/>", re.S)
HEAD = ("[앞선 대화 — 답변에 나오는 용어와 흐름을 이해하는 데만 써라. 여기에만 있는 사실, 숫자, 이유를 "
        "답변에 새로 넣지 마라. [코드]는 코드·링크·경로를 가린 자리다.]\n")
TAIL = "[앞선 대화 끝]\n\n"
FILLER = {"(keep-alive)", "No response requested."}  # lines that carry nothing


def _tail_lines(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - SCAN))
        data = f.read()
    lines = data.split(b"\n")
    if size > SCAN:
        lines = lines[1:]  # the first one is cut
    return lines


def _text(content, role):
    if isinstance(content, str):
        return content if role == "user" else ""
    if not isinstance(content, list):
        return ""
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def _clean(text):
    return TAGGED.sub("", text).strip()


def _clip(text, n):
    return text if len(text) <= n else text[:n].rstrip() + " …(뒤 생략)"


def _mask(text):
    masked, spans = protect.mask(text)
    return protect.TOKEN_RE.sub("[코드]", masked) if spans else masked


def read(path, answer=""):
    """(the conversation before `answer`, as prompt text, or ""; the main model's id, or None)."""
    if not path or config.CONTEXT_CHARS <= 0 and config.REVIEW_MODEL != "main":
        return "", None
    try:
        lines = _tail_lines(path)
    except OSError:
        return "", None
    turns, model = [], None
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
            continue
        m = e.get("message") or {}
        if e["type"] == "assistant" and str(m.get("model", "")).startswith("<"):
            continue  # Claude Code's own lines ("<synthetic>"): an error or a stand-in, not the model's words
        if e["type"] == "assistant" and m.get("model"):
            model = m["model"]
        if e.get("isMeta") or e.get("isCompactSummary") or e.get("isVisibleInTranscriptOnly"):
            continue
        text = _clean(_text(m.get("content"), e["type"]))
        if text and text not in FILLER:
            turns.append(("사용자" if e["type"] == "user" else "Claude", text))
    if config.CONTEXT_CHARS <= 0:
        return "", model
    answer = answer.strip()
    if answer:  # the answer being rewritten may already be in the transcript
        turns = [(who, t) for who, t in turns if who != "Claude" or (t.strip() != answer and t.strip() not in answer)]
    picked, total = [], 0
    for who, t in reversed(turns):
        item = f"[{who}] {_clip(_mask(t), config.CONTEXT_MESSAGE_CHARS)}\n"
        if total + len(item) > config.CONTEXT_CHARS:
            break
        picked.append(item)
        total += len(item)
    if not picked:
        return "", model
    return HEAD + "".join(reversed(picked)) + TAIL, model
