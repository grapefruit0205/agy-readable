"""`agy-readable status | login | log [N] | stop`: see what the hook and the daemon are doing, sign agy in."""
import getpass
import json
import os
import shutil
import sys
import time

from agy_readable import __version__, config, daemon, login

USAGE = """usage: agy-readable <command>

  status     settings, agy, daemon and the last answers' outcomes
  login      sign agy in to Antigravity (opens the Google sign-in page)
  log [N]    the last N hook log lines (default 20)
  stop       stop the daemon and its pre-started agy (it restarts with the next answer)
"""


def read_log(name, n):
    path = os.path.join(config.data_dir(), name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def ping():
    try:
        return daemon.call({"op": "ping"}, 3)
    except (OSError, ValueError):
        return None


def describe(entry):
    when = time.strftime("%m-%d %H:%M:%S", time.localtime(entry.get("t", 0)))
    if entry.get("outcome") == "refined":
        return f"{when}  다듬음   {entry.get('chars')}→{entry.get('out')}자  {entry.get('seconds')}s  ({entry.get('via')})"
    if entry.get("outcome") == "skipped":
        return f"{when}  건너뜀   {entry.get('chars')}자  ({entry.get('reason')})"
    if entry.get("outcome"):
        return f"{when}  원문     {entry.get('chars', '?')}자  {entry.get('seconds', '')}s  {entry.get('reason')}"
    return f"{when}  {json.dumps(entry, ensure_ascii=False)}"


def status():
    agy = shutil.which(config.AGY)
    print(f"agy-readable {__version__}")
    print(f"  data dir   {config.data_dir()}")
    print(f"  agy        {agy or '찾을 수 없음 (' + config.AGY + ')'}")
    print(f"  model      {config.MODEL}   timeout {config.TIMEOUT:g}s   notes {'on' if config.NOTES else 'off'}")
    p = ping()
    if p and p.get("auth_required"):
        print("  sign-in    Antigravity 로그인 필요: `agy-readable login`")
    if p:
        spares = ", ".join(f"{s['pid']} {'ready' if s['ready'] else 'starting'} {s['age']:.0f}s" for s in p["spares"])
        print(f"  daemon     pid {p['pid']}  model {p.get('model')}  spares [{spares}]  busy {p['busy']}  fails {p['fails']}")
    else:
        print("  daemon     not running (starts with the next answer)")
    entries = [e for e in read_log("hook.log", 200) if e.get("outcome")][-10:]
    if entries:
        print("\nrecent answers")
        for e in entries:
            print("  " + describe(e))


def sign_in():
    if not shutil.which(config.AGY):
        print(f"agy를 찾을 수 없습니다 ({config.AGY}). Antigravity CLI를 먼저 설치하세요.")
        return 1
    r = login.start(auto=False)
    if not r.get("ok") or r.get("already"):
        print(login.note(r, markdown=False))
        return 0 if r.get("already") else 1
    if not sys.stdin.isatty():  # Claude's Bash tool: the code goes into Claude Code's prompt instead
        print(login.note(r, markdown=False))
        return 0
    print(("브라우저에 Google 로그인 페이지를 열었습니다.\n" if r["opened"] else "") + f"Google 로그인 페이지:\n{r['url']}\n")
    code = getpass.getpass(f"로그인 후 페이지에 나온 코드를 {r['left']}초 안에 붙여넣고 Enter: ").strip()
    try:
        res = daemon.call({"op": "login_code", "code": code}, 45)
    except (OSError, ValueError) as e:
        res = {"ok": False, "error": str(e)}
    if res.get("ok"):
        print("로그인했습니다. 다음 답변부터 다듬습니다.")
        return 0
    print(f"로그인하지 못했습니다 ({res.get('error')}). `agy-readable login`을 다시 실행하세요.")
    return 1


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else "status"
    if cmd == "status":
        status()
    elif cmd == "log":
        n = int(args[1]) if len(args) > 1 else 20
        for e in read_log("hook.log", n):
            print(describe(e))
    elif cmd == "login":
        return sign_in()
    elif cmd == "stop":
        if ping():
            daemon.call({"op": "stop"}, 5)
            print("daemon stopped")
        else:
            print("daemon was not running")
    else:
        sys.stdout.write(USAGE)
        return 0 if cmd in ("-h", "--help", "help") else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
