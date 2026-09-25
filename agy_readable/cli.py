"""`agy-readable status | login | log [N] | samples | diff [N] | stop`: see what the hook and the daemon are doing,
compare kept rewrites with their originals, sign agy in."""
import difflib
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
  samples    the kept originals and rewrites, newest first
  diff [N]   what the rewrite changed in kept sample N (default 1, the newest)
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
        again = f"  다시 시도 {entry['retries']}" if entry.get("retries") else ""
        checked = f"  고치기: {entry['review']}" if entry.get("review") else ""
        if len(entry.get("stages") or []) > 1:  # e.g. 12.3+4.1s
            checked += f"  ({'+'.join(f'{s:g}' for s in entry['stages'])}s)"
        return (f"{when}  다듬음   {entry.get('chars')}→{entry.get('out')}자  {entry.get('seconds')}s  "
                f"({entry.get('via')}){again}{checked}")
    if entry.get("outcome") == "skipped":
        return f"{when}  건너뜀   {entry.get('chars')}자  ({entry.get('reason')})"
    if entry.get("outcome"):
        return f"{when}  원문     {entry.get('chars', '?')}자  {entry.get('seconds', '')}s  {entry.get('reason')}"
    return f"{when}  {json.dumps(entry, ensure_ascii=False)}"


def read_samples():
    """Kept samples, newest first."""
    d = config.samples_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d), reverse=True):
        try:
            with open(os.path.join(d, name), encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            pass
    return out


def samples():
    kept = read_samples()
    if not kept:
        print(f"저장된 글이 없습니다 ({config.samples_dir()}, keep {config.KEEP})")
        return
    for i, s in enumerate(kept, 1):
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(s.get("t", 0)))
        state = "다듬음" if s.get("outcome") == "refined" else f"버림({s.get('reason')})"
        print(f"{i:3d}  {when}  {s.get('model')}  시도 {s.get('attempt', 0) + 1}  "
              f"{len(s.get('before', ''))}→{len(s.get('after', ''))}자  {state}")


def diff(n):
    kept = read_samples()
    if not 1 <= n <= len(kept):
        print(f"{n}번 글이 없습니다 (저장된 글 {len(kept)}개)")
        return 1
    s = kept[n - 1]
    print(f"# {s.get('model')}  시도 {s.get('attempt', 0) + 1}  "
          f"{'다듬음' if s.get('outcome') == 'refined' else '버림: ' + str(s.get('reason'))}"
          + (f"  고치기: {s['review']}" if s.get("review") else ""))
    if s.get("masked"):
        print("# 버린 글은 agy가 쓴 그대로라, 코드·링크·경로가 ⟦숫자⟧ 표시로 남아 있습니다")
    lines = difflib.unified_diff(s["before"].splitlines(), s["after"].splitlines(), "원문", "다시 쓴 글", lineterm="")
    for line in lines:
        print(line)
    return 0


def status():
    agy = shutil.which(config.AGY)
    print(f"agy-readable {__version__}")
    print(f"  data dir   {config.data_dir()}")
    print(f"  agy        {agy or '찾을 수 없음 (' + config.AGY + ')'}")
    print(f"  model      {config.MODEL}   timeout {config.TIMEOUT:g}s   notes {'on' if config.NOTES else 'off'}"
          f"   review {'on' if config.REVIEW else 'off'}   retries {config.RETRIES}   keep {config.KEEP}")
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
    elif cmd == "samples":
        samples()
    elif cmd == "diff":
        return diff(int(args[1]) if len(args) > 1 else 1)
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
