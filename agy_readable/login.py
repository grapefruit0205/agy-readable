"""Signing agy in to Antigravity from inside Claude Code.

When agy is not signed in, the daemon runs agy's own sign-in flow (see daemon.Login) and the hook shows
the Google sign-in URL under the answer, opening it in the browser. The sign-in page then shows a code;
the user pastes it into Claude Code's prompt, where this module's UserPromptSubmit hook takes it, hands it
to the waiting agy and blocks the prompt, so the code never reaches the model.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from agy_readable import config, daemon

CODE = re.compile(r"4/[0-9A-Za-z_\-]{20,}")  # a Google authorization code, as the sign-in page shows it
COMMANDS = ("/agy-readable:login", "/login-agy")
RETRY = "입력창에 `/agy-readable:login`을 입력하거나, 터미널에서 `agy`를 실행해 로그인하세요."


def open_browser(url):
    """Open the URL in the user's browser if this machine has one; True if a browser was started."""
    setting = config.BROWSER.strip()
    if setting == "none":
        return False
    if setting:
        argv = shlex.split(setting) + [url]
    elif sys.platform == "darwin":
        argv = ["open", url]
    elif (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("xdg-open"):
        argv = ["xdg-open", url]
    else:
        return False
    try:
        subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


def start(auto):
    """Ask the daemon for a sign-in attempt; opens the browser for a new one. Returns the daemon's reply
    plus "opened"."""
    try:
        daemon.ensure_running(wait=3.0)
        r = daemon.call({"op": "login_start", "auto": auto}, 20)
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"agy-readable 데몬에 연결하지 못함 ({e.__class__.__name__})"}
    r["opened"] = bool(r.get("fresh")) and open_browser(r["url"])
    return r


def note(r, markdown=True):
    """What to tell the user about a sign-in attempt from start()."""
    if r.get("already"):
        return "Antigravity에 이미 로그인되어 있습니다. 다음 답변부터 다듬습니다."
    if r.get("cooldown"):
        return "Antigravity 로그인이 필요합니다. " + RETRY
    if r.get("pending"):
        return ("Antigravity 로그인이 필요한데, agy 시작이 늦어져 로그인 주소를 아직 받지 못했습니다. "
                "잠시 뒤 입력창에 `/agy-readable:login`을 입력하세요.")
    if not r.get("ok"):
        return f"Antigravity 로그인을 시작하지 못했습니다 ({r.get('error')}). " + RETRY
    url, left = r["url"], r.get("left", 60)
    if not r.get("fresh"):
        head, link_label = "앞에서 연 Google 로그인 페이지에서 로그인한 뒤", "로그인 페이지"
    elif r.get("opened"):
        head, link_label = "브라우저에 Google 로그인 페이지를 열었습니다. 로그인한 뒤", "페이지가 열리지 않았다면 이 주소를 여세요"
    else:
        head, link_label = "아래 Google 로그인 페이지를 열어 로그인한 뒤", "로그인 페이지"
    text = (f"Antigravity 로그인이 필요합니다. {head}, 페이지에 나온 코드를 {left}초 안에 이 입력창에 그대로 "
            f"붙여넣으세요. 코드는 Claude에게 가지 않고 agy에만 전달됩니다.")
    if not r.get("opened"):  # no browser here (e.g. over SSH): agy's own sign-in in a terminal also works
        text += " 주소를 열기 어렵다면 터미널에서 `agy`를 한 번 실행해 로그인해도 됩니다."
    # the URL goes last: it is several hundred characters long
    return text + (f" [{link_label}]({url})" if markdown else f"\n\n{link_label}:\n{url}")


def submit(code):
    """Hand a pasted code to the waiting agy. Returns the message for the user."""
    try:
        r = daemon.call({"op": "login_code", "code": code}, 45)
    except (OSError, ValueError):
        r = {"ok": False, "error": "no_attempt"}
    if r.get("ok"):
        return "Antigravity 로그인이 끝났습니다. 다음 답변부터 agy-readable이 답변을 다듬습니다."
    err = r.get("error") or ""
    if err == "already":
        return "Antigravity에 이미 로그인되어 있습니다."
    if err == "checking":
        return "코드를 agy에 넘겼고 확인을 기다리는 중입니다. 잠시 뒤 `agy-readable status`로 결과를 볼 수 있습니다."
    if err == "no_attempt":
        return "진행 중인 Antigravity 로그인이 없어서 이 코드는 쓰지 않았습니다. " + RETRY
    if err == "expired":
        why = "코드를 기다리는 60초가 지나 이 코드는 쓸 수 없습니다."
    elif "invalid_grant" in err:
        why = "코드가 맞지 않거나 이미 쓴 코드입니다."
    else:
        why = f"로그인에 실패했습니다 ({err[:120]})."
    retry = start(auto=False)
    return why + " 새로 시도합니다. " + note(retry, markdown=False)


def prompt_hook():
    """UserPromptSubmit: take a pasted sign-in code (or /agy-readable:login) before it reaches the model."""
    event = json.load(sys.stdin)
    if os.name == "nt":
        return
    prompt = (event.get("prompt") or "").strip()
    if CODE.fullmatch(prompt):
        reason = submit(prompt)
    elif prompt in COMMANDS:
        reason = note(start(auto=False), markdown=False)
    else:
        return
    config.log("hook.log", login_prompt=True, code=bool(CODE.fullmatch(prompt)))
    print(json.dumps({"decision": "block", "reason": "agy-readable: " + reason}, ensure_ascii=False))


if __name__ == "__main__":
    prompt_hook()
