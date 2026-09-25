"""Keeps a pre-started agy (stream-json mode) waiting, so the hook skips agy's startup
(sign-in, quota, eligibility, conversation setup: ~5 s normally, 60 s+ when the backend is slow).

Each worker answers exactly one request and is then shut down, so nothing from one answer can
leak into the next one's context. A fresh spare is started as soon as one is taken.

agy's backend stalls (30-60 s waits during startup, sending, or mid-generation) hit single processes,
not all of them, so requests are hedged: a spare still not ready after STUCK_AFTER gets another one
started beside it; a request that finds no ready spare goes to a starting one and a fresh one at once;
and a request with no answer after HEDGE_AFTER (or whose workers all failed) is sent to one more.
The first answer wins and every worker involved is shut down.

Protocol: one JSON line each way over a unix socket.
  {"op": "ask", "prompt": str, "timeout": s, "model": str, "weight": n?} -> {"ok": true, "text": str, "warm": bool, ...}
  ("weight": how many prompt characters' worth of time the answer should take; default the prompt's length)
                                                             | {"ok": false, "error": str, "timed_out": bool}
  {"op": "ping"} -> {"ok": true, "pid": int, "model": str, "auth_required": bool, "spares": [...]}
  {"op": "login_start", "auto": bool} -> {"ok": true, "url": str, "fresh": bool, "left": s} | {"ok": true, "already": true}
                                        | {"ok": false, "cooldown": true} | {"ok": false, "error": str}
  {"op": "login_code", "code": str} -> {"ok": true} | {"ok": false, "error": "no_attempt"|"expired"|str, "pending": bool}
  {"op": "stop"} -> {"ok": true}

Signing in: agy signs in with the OAuth token Antigravity keeps in the OS keyring. Without one, a stream-json
agy just fails ("authentication required"), so the daemon notes that and, when asked, runs a one-shot
`agy -p` with a pseudo-terminal as stdin: agy then prints a Google sign-in URL and waits 60 s for the code
the sign-in page shows. The hook shows the URL, and the code the user pastes into Claude Code's prompt is
written to that terminal. agy itself exchanges the code and stores the token; the daemon never sees a token.
"""
import hashlib
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from agy_readable import __version__, config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPARE_MAX_AGE = 1200  # replace a spare that has waited this long; agy's sign-in token lives about an hour
BACKOFF = [5, 15, 60, 300]  # seconds before respawning after consecutive spare failures (e.g. backend 503)
MAX_STARTING = 3  # cap on spares starting at once while hedging against stuck ones
HEDGE_MIN_LEFT = 8  # don't add a worker that has less time than this to answer
MAX_RACERS = 3  # workers one request may use in total
AUTH_ERROR = re.compile(r"authenticat", re.I)  # agy: "authentication required" / "authentication failed or timed out"
AUTH_RECHECK = 60  # while signed out, try a spare this often, in case the user signed in some other way
LOGIN_WINDOW = 60  # agy waits this long for the sign-in code (fixed in agy)
LOGIN_COOLDOWN = 600  # an answer opens the sign-in page on its own at most this often
LOGIN_URL_WAIT = 10  # a signed-out agy prints its URL within a second; longer means it is still starting
SIGNIN_URL = re.compile(r"https://accounts\.google\.com/\S+")


def sock_path():
    # unix socket paths are limited to ~108 bytes, so the data directory is hashed into the name
    name = f"agy-readable-{hashlib.sha1(config.data_dir().encode()).hexdigest()[:8]}.sock"
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    if not base or len(os.path.join(base, name)) > 100:
        base = f"/tmp/agy-readable-{os.getuid()}"
    os.makedirs(base, mode=0o700, exist_ok=True)
    return os.path.join(base, name)


def log(**fields):
    config.log("daemon.log", **fields)


# ---------------------------------------------------------------- client side (hook and CLI)

def call(msg, timeout):
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(timeout)
        s.connect(sock_path())
        s.sendall((json.dumps(msg, ensure_ascii=False) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            buf += chunk
    return json.loads(buf)


def ensure_running(wait=0.0):
    """Start the daemon if its socket does not answer, or replace one left running by another version of
    the plugin (after an update the data directory, and so the socket, stays the same); optionally wait
    until it answers. Returns True if reachable."""
    deadline = time.time() + wait
    last_spawn = 0.0
    while True:
        try:
            version = call({"op": "ping"}, 2).get("version")
            if version == __version__:
                return True
            if not last_spawn:
                log(replacing=version or "0.1.0", by=__version__)
                call({"op": "stop"}, 2)
        except (OSError, ValueError):
            pass
        if time.time() - last_spawn > 1.0:  # again if a new daemon lost the lock to one still shutting down
            # own session + no inherited stdio: the hook must not wait on (or kill) the daemon
            env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
            subprocess.Popen([sys.executable, "-m", "agy_readable.daemon"], cwd=ROOT, env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            last_spawn = time.time()
        if time.time() >= deadline:
            return False
        time.sleep(0.05)


# ---------------------------------------------------------------- daemon side

class Worker:
    def __init__(self, model):
        cwd = os.path.join(config.data_dir(), "agy_cwd")  # empty, so agy has no files to pick up as context
        os.makedirs(cwd, exist_ok=True)
        self.born = time.time()
        self.ready_at = None
        self.early_error = None  # why agy gave up before it was ready, e.g. not signed in
        self.usage = {}
        self.events = queue.Queue()
        self.p = subprocess.Popen(
            [config.AGY, "--model", model, "--disable-slash-commands", "--input-format", "stream-json",
             "--output-format", "stream-json", "-p", ""],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event") == "init" and self.ready_at is None:
                self.ready_at = time.time()
                log(worker=self.p.pid, ready_after=round(self.ready_at - self.born, 1))
            if ev.get("event") == "result" and self.ready_at is None:
                self.early_error = ev.get("result", {}).get("error") or "agy 종료됨"
            self.events.put(ev)
        self.events.put(None)

    def alive(self):
        return self.p.poll() is None

    def ask(self, prompt, timeout):
        # a result that arrived before any request means agy gave up during startup (e.g. 503)
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev is None or ev.get("event") == "result":
                raise RuntimeError((ev or {}).get("result", {}).get("error") or "agy 종료됨")
        try:
            self.p.stdin.write(json.dumps({"event": "user", "message": {"role": "user", "content": prompt}},
                                          ensure_ascii=False) + "\n")
            self.p.stdin.flush()
        except OSError:
            pass  # agy already exited; its last events below say why
        deadline = time.time() + timeout
        while True:
            try:
                ev = self.events.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                raise TimeoutError
            if ev is None:
                raise RuntimeError(f"agy 종료됨 (코드 {self.p.wait()})")
            if ev.get("event") == "result":
                r = ev.get("result", {})
                self.usage = r.get("usage") or {}
                if r.get("status") != "SUCCESS":
                    raise RuntimeError(r.get("error") or r.get("status") or "알 수 없는 오류")
                return r.get("response", "")

    def retire(self, grace=3.0):
        """Close stdin so agy exits on its own; kill the whole group if it lingers."""
        def run():
            try:
                self.p.stdin.close()
            except OSError:
                pass
            try:
                self.p.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.p.wait()
        threading.Thread(target=run, daemon=True).start()


class Login:
    """One sign-in attempt: `agy -p` with a pseudo-terminal as stdin, which is when agy offers its sign-in flow
    (with a pipe it just fails). agy prints the sign-in URL on stderr and reads the code from the terminal."""

    def __init__(self, model, on_signed_in):
        import pty  # POSIX only, like the rest of the daemon
        import termios

        cwd = os.path.join(config.data_dir(), "agy_cwd")
        os.makedirs(cwd, exist_ok=True)
        self.on_signed_in = on_signed_in
        self.started = time.time()
        self.url = None
        self.error = None
        self.answered = False
        self.code_sent = False
        self.shown = False  # the URL has been handed out
        self.rc = None
        self.changed = threading.Condition()
        self.master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO  # the code is not echoed back anywhere
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        try:
            self.p = subprocess.Popen([config.AGY, "--model", model, "--disable-slash-commands", "-p", "ok"],
                                      cwd=cwd, stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, start_new_session=True)
        except OSError:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        for target, arg in ((self._read_err, self.p.stderr), (self._read_out, self.p.stdout), (self._drain, None)):
            threading.Thread(target=target, args=(arg,) if arg else (), daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _notify(self, **fields):
        with self.changed:
            for k, v in fields.items():
                setattr(self, k, v)
            self.changed.notify_all()

    def _read_err(self, stream):
        for line in stream:
            m = SIGNIN_URL.search(line)
            if m and not self.url:
                self._notify(url=m.group(0))
            elif line.startswith("Error:") and not self.error:
                self._notify(error=line[len("Error:"):].strip())

    def _read_out(self, stream):
        for line in stream:
            if line.strip():
                self._notify(answered=True)  # agy got past sign-in and answered the prompt

    def _drain(self):
        while True:
            try:
                if not os.read(self.master, 4096):
                    return
            except OSError:
                return

    def _wait(self):
        rc = self.p.wait()
        self._notify(rc=rc)
        try:
            os.close(self.master)
        except OSError:
            pass
        if rc == 0:
            self.on_signed_in()

    def state(self):
        if self.rc is None and not self.answered:
            if self.code_sent:
                return "checking"
            if not self.url:
                return "starting"
            return "waiting" if time.time() < self.started + LOGIN_WINDOW - 2 else "expired"
        if self.rc in (None, 0):
            return "ok"
        return "expired" if self.error and "timed out" in self.error else "failed"

    def wait_for(self, pred, timeout):
        with self.changed:
            self.changed.wait_for(pred, timeout)

    def submit(self, code):
        os.write(self.master, (code + "\r").encode())
        self._notify(code_sent=True)

    def kill(self):
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Daemon:
    def __init__(self):
        self.lock = threading.Lock()
        self.model = config.MODEL
        self.spares = []
        self.busy = set()
        self.fails = 0
        self.next_spawn = 0.0
        self.last_used = time.time()
        self.stopping = threading.Event()
        self.auth_required = False  # agy is not signed in to Antigravity
        self.login = None
        self.last_auto_login = 0.0

    def take(self):
        """The workers to send a request to: a ready spare, else the oldest starting one plus a fresh one."""
        with self.lock:
            self.last_used = time.time()
            live = [w for w in self.spares if w.alive()]
            ready = sorted((w for w in live if w.ready_at), key=lambda w: w.born)
            starting = sorted((w for w in live if not w.ready_at), key=lambda w: w.born)
            if ready:
                picked = ready[:1]
            elif starting:
                picked = [starting[0], Worker(self.model)]
            else:
                picked = [Worker(self.model), Worker(self.model)]
            for w in picked:
                if w in self.spares:
                    self.spares.remove(w)
                self.busy.add(w)
            return picked

    def take_one(self):
        """One more worker for a slow or failed request: a ready spare if there is one, else a fresh one."""
        with self.lock:
            ready = sorted((w for w in self.spares if w.alive() and w.ready_at), key=lambda w: w.born)
            w = ready[0] if ready else Worker(self.model)
            if w in self.spares:
                self.spares.remove(w)
            self.busy.add(w)
            return w

    def use_model(self, model):
        """A changed model option reaches the daemon with the next request; spares on the old model go."""
        with self.lock:
            if not model or model == self.model:
                return
            log(model_changed=model, was=self.model)
            self.model = model
            for w in self.spares:
                w.retire()
            self.spares = []
            self.next_spawn = 0.0

    @staticmethod
    def _run_one(w, prompt, timeout, results):
        try:
            results.put((w, {"ok": True, "text": w.ask(prompt, timeout)}))
        except TimeoutError:
            results.put((w, {"ok": False, "error": "agy 응답 시간 초과", "timed_out": True}))
        except Exception as e:
            results.put((w, {"ok": False, "error": f"agy 오류: {e}"[:300]}))

    def handle_ask(self, msg):
        start = time.time()
        deadline = start + float(msg.get("timeout", config.TIMEOUT))
        prompt = msg["prompt"]
        self.use_model(msg.get("model"))
        # the caller knows how long its answer should take (a long rewrite, or a short list of fixes)
        hedge_at = start + config.HEDGE_AFTER + float(msg.get("weight") or len(prompt)) / 400
        racers, failed, hedges, results = [], [], [], queue.Queue()
        winner, res = None, None

        def launch(w):
            racers.append(w)
            threading.Thread(target=self._run_one, args=(w, prompt, deadline - time.time(), results),
                             daemon=True).start()

        def can_add():
            return len(racers) < MAX_RACERS and deadline - time.time() > HEDGE_MIN_LEFT

        first = self.take()
        warm = len(first) == 1 and first[0].ready_at is not None
        for w in first:
            launch(w)
        while True:
            slow_hedge = not hedges and can_add()
            try:
                w, r = results.get(timeout=max(0.0, (hedge_at if slow_hedge else deadline + 1) - time.time()))
            except queue.Empty:
                if not slow_hedge:
                    break
                hedges.append(f"slow@{time.time() - start:.0f}s")
                launch(self.take_one())
                continue
            if r["ok"]:
                winner, res = w, r
                break
            failed.append(r)
            if AUTH_ERROR.search(r.get("error", "")):  # another agy would fail the same way
                self.signed_out()
                break
            if len(failed) == len(racers):
                if not can_add():
                    break
                hedges.append(f"failed@{time.time() - start:.0f}s")
                launch(self.take_one())
        if res is None:  # a real error says more than "timed out"
            res = sorted(failed, key=lambda r: r.get("timed_out", False))[0] if failed else \
                {"ok": False, "error": "agy 응답 시간 초과", "timed_out": True}
        for w in racers:
            w.retire()
        with self.lock:
            self.busy.difference_update(racers)
            if res["ok"]:
                self.fails = 0
                self.auth_required = False
            elif not res.get("timed_out") and not self.auth_required:
                self.fails += 1
        w = winner or racers[0]
        res = dict(res, warm=warm, racers=len(racers), hedges=hedges, auth_required=self.auth_required,
                   worker_age=round(start - w.born, 1), seconds=round(time.time() - start, 1))
        # input_tokens stays flat from request to request because every worker starts a fresh conversation
        log(op="ask", worker=w.p.pid, model=self.model, **{k: v for k, v in res.items() if k != "text"},
            chars_out=len(res.get("text", "")), input_tokens=w.usage.get("input_tokens"),
            thinking_tokens=w.usage.get("thinking_tokens"), output_tokens=w.usage.get("output_tokens"))
        return res

    def signed_out(self):
        with self.lock:
            if not self.auth_required:
                log(auth_required=True)
            self.auth_required = True
            self.next_spawn = time.time() + AUTH_RECHECK

    def signed_in(self):
        with self.lock:
            self.auth_required = False
            self.fails = 0
            self.next_spawn = 0.0  # start a spare right away

    def login_start(self, auto):
        """Start a sign-in attempt, or join the one under way, and return its URL.

        An attempt whose agy is still starting is always joined (a new agy would not start faster). One
        whose URL was already shown is reused by answers (auto) and replaced by `agy-readable login`.
        Answers start a new attempt at most every LOGIN_COOLDOWN. agy counts as signed in only once it
        has answered the test prompt or exited cleanly: no URL yet may just mean agy is slow to start."""
        with self.lock:
            self.last_used = time.time()
            cur = self.login
            state = cur.state() if cur else None
            if state == "waiting" and cur.shown and not auto:
                cur.kill()
                cur = None
            elif state not in ("starting", "waiting"):
                cur = None
            if cur is None:
                if auto and time.time() - self.last_auto_login < LOGIN_COOLDOWN:
                    return {"ok": False, "cooldown": True}
                try:
                    cur = self.login = Login(self.model, self.on_login_ok)
                except OSError as e:
                    return {"ok": False, "error": f"agy 실행 실패 ({e.strerror})"}
        cur.wait_for(lambda: cur.url or cur.rc is not None or cur.answered, LOGIN_URL_WAIT)
        if cur.rc == 0 or cur.answered:
            self.signed_in()
            return {"ok": True, "already": True}
        if cur.error or cur.rc is not None:
            return {"ok": False, "error": cur.error or f"agy 종료됨 (코드 {cur.rc})"}
        if not cur.url:
            log(login="still_starting", after=round(time.time() - cur.started, 1))
            return {"ok": False, "pending": True}
        with self.lock:
            fresh, cur.shown = not cur.shown, True
            if fresh and auto:
                self.last_auto_login = time.time()
        if fresh:
            log(login="started", auto=auto)
        return {"ok": True, "url": cur.url, "fresh": fresh, "left": round(cur.started + LOGIN_WINDOW - time.time())}

    def on_login_ok(self):
        log(login="ok")
        self.signed_in()

    def login_code(self, code):
        """Hand the code from the sign-in page to the waiting agy and wait for its verdict."""
        self.last_used = time.time()
        cur = self.login
        state = cur.state() if cur else None
        if state != "waiting":
            return {"ok": False, "error": {"expired": "expired", "ok": "already", "checking": "checking"}.get(
                state, "no_attempt")}
        cur.submit(code)
        cur.wait_for(lambda: cur.rc is not None or cur.error or cur.answered, 30)
        if cur.error or cur.rc not in (None, 0):
            log(login="failed", error=(cur.error or "")[:200])
            return {"ok": False, "error": cur.error or f"agy 종료됨 (코드 {cur.rc})"}
        if cur.rc == 0 or cur.answered:
            return {"ok": True}
        return {"ok": False, "error": "checking"}  # no verdict yet; on_login_ok follows if it succeeds

    def maintain(self):
        while not self.stopping.wait(1.0):
            now = time.time()
            with self.lock:
                if self.stopping.is_set():
                    return
                for w in list(self.spares):
                    if not w.alive():
                        self.spares.remove(w)
                        if w.early_error and AUTH_ERROR.search(w.early_error):
                            if not self.auth_required:
                                log(auth_required=True)
                            self.auth_required = True
                            self.next_spawn = now + AUTH_RECHECK
                            continue
                        self.fails += 1
                        delay = BACKOFF[min(self.fails, len(BACKOFF)) - 1]
                        self.next_spawn = now + delay
                        log(worker=w.p.pid, died_idle=True, code=w.p.returncode, retry_in=delay)
                    elif w.ready_at and now - w.born > SPARE_MAX_AGE:
                        self.spares.remove(w)
                        w.retire()
                        log(worker=w.p.pid, recycled=True)
                    elif w.ready_at:
                        self.fails = 0  # backend reachable again
                        self.auth_required = False
                ready = sorted((w for w in self.spares if w.ready_at), key=lambda w: w.born)
                starting = [w for w in self.spares if not w.ready_at]
                if len(ready) >= config.SPARES:  # hedges that lost the race, and any surplus, are let go
                    for w in starting + ready[:len(ready) - config.SPARES]:
                        self.spares.remove(w)
                        w.retire()
                        log(worker=w.p.pid, surplus=True)
                elif now >= self.next_spawn and (
                        len(starting) < config.SPARES - len(ready)
                        or len(starting) < MAX_STARTING and all(now - w.born > config.STUCK_AFTER for w in starting)):
                    try:
                        w = Worker(self.model)
                    except OSError as e:  # agy not installed or not runnable
                        self.fails += 1
                        self.next_spawn = now + BACKOFF[min(self.fails, len(BACKOFF)) - 1]
                        log(spawn_error=str(e))
                    else:
                        self.spares.append(w)
                        log(worker=w.p.pid, spawned=True, hedge=bool(starting))
                idle = not self.busy and now - self.last_used > config.IDLE_EXIT
            if idle:
                log(idle_exit=True)
                self.stop()

    def stop(self):
        self.stopping.set()
        try:
            os.unlink(sock_path())
        except OSError:
            pass
        self.lock_file.close()  # after the unlink, so a successor never has its fresh socket deleted by us
        with self.lock:
            for w in self.spares + list(self.busy):
                w.retire(grace=1.0)
            if self.login:
                self.login.kill()
        time.sleep(1.5)
        os._exit(0)

    def serve_conn(self, conn):
        with conn:
            try:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                msg = json.loads(buf)
                if msg.get("op") == "ask":
                    res = self.handle_ask(msg)
                elif msg.get("op") == "ping":
                    with self.lock:
                        res = {"ok": True, "pid": os.getpid(), "version": __version__, "model": self.model,
                               "fails": self.fails,
                               "busy": len(self.busy), "auth_required": self.auth_required,
                               "login": self.login.state() if self.login else None,
                               "spares": [{"pid": w.p.pid, "ready": bool(w.ready_at), "age": round(time.time() - w.born, 1)}
                                          for w in self.spares]}
                elif msg.get("op") == "login_start":
                    res = self.login_start(bool(msg.get("auto")))
                elif msg.get("op") == "login_code":
                    res = self.login_code(str(msg.get("code", "")).strip())
                elif msg.get("op") == "stop":
                    conn.sendall(b'{"ok": true}\n')
                    threading.Thread(target=self.stop).start()
                    return
                else:
                    res = {"ok": False, "error": "unknown op"}
                conn.sendall((json.dumps(res, ensure_ascii=False) + "\n").encode())
            except (OSError, ValueError):
                pass  # client gave up (hook timeout); the workers were already retired in handle_ask

    def run(self):
        import fcntl  # POSIX only; imported here so the hook can load this module anywhere

        path = sock_path()
        self.lock_file = open(path + ".lock", "w")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another daemon already serves this data directory
        try:
            os.unlink(path)  # left over from a daemon that was killed
        except OSError:
            pass
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        os.chmod(path, 0o600)
        srv.listen(16)
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=self.stop).start())
        log(started=os.getpid(), model=self.model, spares=config.SPARES)
        threading.Thread(target=self.maintain, daemon=True).start()
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self.serve_conn, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    Daemon().run()
