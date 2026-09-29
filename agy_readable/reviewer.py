"""The second pass by Claude (Opus by default) instead of agy: agy rewrites, Claude checks the rewrite against the
original and answers with only the lines to change (review.py), and the hook shows the result.

Claude runs headless (`claude -p`) with no tools, no settings sources (so no plugins, hooks or MCP servers:
this plugin's own hook never runs inside it), no CLAUDE.md, no auto-memory, no session saved, a short system
prompt of its own, and an empty working directory. It uses the Claude Code sign-in already on this computer.

The daemon keeps one such Claude waiting in stream-json mode (ClaudeWorker), which answers a short review in
about 2 s against 5 s for a fresh `claude -p`; each worker answers one request and is shut down, like agy's.
Without the daemon, one_shot runs `claude -p` once.
"""
import json
import os
import queue
import shutil
import subprocess
import threading
import time

from agy_readable import config, proc

SYSTEM = ("너는 다른 모델이 다시 쓰거나 번역한 한국어 글을 원문과 맞춰 보는 검수자다. 도구를 쓰지 말고, "
          "지시한 형식의 줄만 출력한다. 설명, 머리말, 코드 블록은 쓰지 않는다.")
AUTH_HINTS = ("not logged in", "/login", "invalid api key", "authentication", "oauth")


def command(stream):
    args = [shutil.which(config.CLAUDE) or config.CLAUDE, "-p", "--model", config.REVIEW_MODEL,
            "--tools", "", "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
            "--disable-slash-commands", "--system-prompt", SYSTEM]
    if config.REVIEW_EFFORT:
        args += ["--effort", config.REVIEW_EFFORT]
    if stream:
        args += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    return args


def env():
    # AGY_READABLE_INNER: should a hook of this plugin run in there after all, it passes the answer through
    return dict(os.environ, CLAUDE_CODE_DISABLE_CLAUDE_MDS="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
                AGY_READABLE_INNER="1")


def cwd():
    d = os.path.join(config.data_dir(), "claude_cwd")  # empty: nothing for Claude to pick up as context
    os.makedirs(d, exist_ok=True)
    return d


def signed_out(text):
    return any(h in (text or "").lower() for h in AUTH_HINTS)


def one_shot(prompt, timeout):
    """Returns (Claude's answer or None, reason when None)."""
    try:
        p = subprocess.Popen(command(False) + [prompt], cwd=cwd(), env=env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", **proc.detached())
    except OSError as e:
        return None, f"claude 실행 실패 ({e.strerror})"
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill_tree(p)
        p.communicate()
        return None, "claude 응답 시간 초과"
    if p.returncode != 0 or not out.strip():
        why = (err or out).strip()[-200:]
        return None, "claude 로그인 필요" if signed_out(why) else f"claude 오류 (종료 코드 {p.returncode}): {why}"
    return out, None


class ClaudeWorker:
    """One `claude -p` in stream-json mode, started ahead and used for one request. Claude prints nothing
    until it gets its first message, so a worker counts as ready once it has stayed up for a moment."""
    SETTLE = 1.5  # seconds: a claude that fails at start (no sign-in, bad model) has exited by then

    def __init__(self):
        self.born = time.time()
        self.events = queue.Queue()
        self.p = subprocess.Popen(command(True), cwd=cwd(), env=env(), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                  errors="replace", **proc.detached())
        self.err = []
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    @property
    def ready_at(self):
        return self.born + self.SETTLE if self.alive() and time.time() - self.born >= self.SETTLE else None

    def _pump(self):
        for line in self.p.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                continue
        self.events.put(None)

    def _drain_err(self):
        for line in self.p.stderr:
            self.err.append(line)
            del self.err[:-20]

    def alive(self):
        return self.p.poll() is None

    def ask(self, prompt, timeout):
        try:
            self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}},
                                          ensure_ascii=False) + "\n")
            self.p.stdin.flush()
        except OSError:
            pass  # claude already exited; the end of its output says why
        deadline = time.time() + timeout
        while True:
            try:
                ev = self.events.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                raise TimeoutError
            if ev is None:
                why = "".join(self.err).strip()[-200:]
                raise RuntimeError(("claude 로그인 필요" if signed_out(why) else "claude 종료됨") + (f": {why}" if why else ""))
            if ev.get("type") == "result":
                if ev.get("is_error") or ev.get("subtype") != "success":
                    why = str(ev.get("result") or ev.get("subtype") or "알 수 없는 오류")[:200]
                    raise RuntimeError("claude 로그인 필요" if signed_out(why) else f"claude 오류: {why}")
                return ev.get("result", "")

    def retire(self, grace=3.0):
        def run():
            try:
                self.p.stdin.close()
            except OSError:
                pass
            try:
                self.p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill_tree(self.p)
                self.p.wait()
        threading.Thread(target=run, daemon=True).start()
