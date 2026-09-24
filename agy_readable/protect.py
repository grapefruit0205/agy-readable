"""Keep what must not change out of the model's hands, and check what it has to handle.

Before an answer goes to agy, fenced code blocks, inline code, Markdown links, URLs and file paths are
replaced by placeholders (⟦0⟧, ⟦1⟧, ...). The rewritten text must carry every placeholder exactly once
(a code block's alone on its line, code blocks in their original order); they are then put back byte for
byte, so these parts cannot change at all.

Numbers stay in the text, since the sentences are written around them. They are compared as a multiset:
every number of the original, with its sign, as often as it appears there, and nothing more. List
numbering is left out, as it may become bullets.

What this cannot catch: two values of the same kind trading places ("A is 10, B is 20" -> "A is 20, B is 10"),
or a sentence whose meaning shifts without any number, code or path changing.
"""
import re
from collections import Counter

TOKEN = "⟦{}⟧"
TOKEN_RE = re.compile(r"⟦(\d+)⟧")
FENCED = re.compile(r"(?ms)^([ \t]*)(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\2[`~]*[ \t]*$|\Z)")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1)[^\n])+\1")
MD_LINK = re.compile(r"!?\[[^\]\n]*\]\([^)\s]+(?:\s+\"[^\"\n]*\")?\)|<https?://[^>\s]+>")
URL = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
PATH = re.compile(r"(?<![\w/.~\-:])((?:~|\.{1,2})?/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*/?"
                  r"|[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?)")
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)*")
LIST_NUMBER = re.compile(r"(?m)^[ \t]*(?:[-*+][ \t]+)?\d+[.)][ \t]")


class Span:
    def __init__(self, text, block):
        self.text = text
        self.block = block  # a fenced code block (with its indentation): its placeholder stays alone on its line

    def core(self):
        """What a model-written span must be taken from: code without its backticks, a link's target."""
        if self.text.startswith("`"):
            return self.text.strip("`").strip()
        m = re.search(r"\]\(([^)\s]+)", self.text)
        return m.group(1) if m else self.text.strip("<>")


def _is_path(s):
    if s.startswith(("/", "~/", "./", "../")):
        return True
    # relative: a/b/c, or a/b.ext; not "A/B", "TCP/IP", "1/2" or "and/or"
    return s.count("/") >= 2 or re.search(r"\.[A-Za-z][A-Za-z0-9]{0,7}/?$", s) is not None


def _url_end(url):
    """Leave trailing punctuation (and an unbalanced closing bracket) to the sentence."""
    while url and url[-1] in ".,;:!?'\"":
        url = url[:-1]
    while url and url[-1] in ")]" and url.count(url[-1]) > url.count("(" if url[-1] == ")" else "["):
        url = url[:-1]
    return url


def mask(text):
    """Returns (text with placeholders, spans), or (None, None) if the text already has ⟦n⟧ in it."""
    if TOKEN_RE.search(text):
        return None, None
    spans = []

    def keep(original, block=False):
        spans.append(Span(original, block))
        return TOKEN.format(len(spans) - 1)

    def fenced(m):
        block = m.group(0).rstrip("\n")
        return m.group(1) + keep(block, block=True) + m.group(0)[len(block):]

    text = FENCED.sub(fenced, text)
    text = INLINE_CODE.sub(lambda m: keep(m.group(0)), text)
    text = MD_LINK.sub(lambda m: keep(m.group(0)), text)

    def url(m):
        u = _url_end(m.group(0))
        return keep(u) + m.group(0)[len(u):]

    def path(m):
        p = m.group(0)
        while p.endswith((".", ",")) and not p.endswith(("/.", "..")):  # the sentence's period, not the path's
            p = p[:-1]
        return keep(p) + m.group(0)[len(p):] if _is_path(p) else m.group(0)

    text = URL.sub(url, text)
    text = PATH.sub(path, text)
    return text, spans


def numbers(text):
    """Numbers outside placeholders and list numbering, with their sign; thousands separators dropped."""
    prose = LIST_NUMBER.sub("", TOKEN_RE.sub(" ", text))
    found = Counter()
    for m in NUMBER.finditer(prose):
        i = m.start()
        minus = i > 0 and prose[i - 1] in "-−" and (i == 1 or not prose[i - 2].isalnum())
        found[("-" if minus else "") + m.group(0).replace(",", "")] += 1
    return found


def _short(items):
    return ", ".join(s if len(s) <= 30 else s[:30] + "…" for s in items[:3])


def restore(original, masked, spans, out):
    """Check the model's rewrite of `masked` and put the protected parts back.
    Returns (text, None), or (None, why it cannot be shown)."""
    out = out.strip()
    if out.startswith("```"):  # the model wrapped its whole answer in a fence
        out = re.sub(r"^```[^\n]*\n|\n?```$", "", out).strip()
    if "```" in out or "~~~" in out:
        return None, "원문에 없는 코드 블록이 생김"

    seen = Counter(TOKEN_RE.findall(out))
    ids = [str(i) for i in range(len(spans))]
    missing = [spans[int(i)].text for i in ids if not seen[i]]
    if missing:
        return None, "코드·링크·경로 일부가 빠짐: " + _short(missing)
    if any(seen[i] > 1 for i in ids) or set(seen) - set(ids):
        return None, "코드·링크·경로 자리 표시가 중복되거나 바뀜"
    blocks = [i for i in ids if spans[int(i)].block]
    lines = {}
    for i in blocks:
        m = re.search(r"(?m)^[ \t]*" + re.escape(TOKEN.format(i)) + r"[ \t]*$", out)
        if not m:
            return None, "코드 블록이 문장 속으로 옮겨짐"
        lines[i] = m.start()
    if [lines[i] for i in blocks] != sorted(lines[i] for i in blocks):
        return None, "코드 블록 순서가 바뀜"

    before, after = len(TOKEN_RE.sub("", masked).strip()), len(TOKEN_RE.sub("", out).strip())
    if before >= 100 and not 0.5 <= after / before <= 2.0:
        return None, f"결과 길이가 비정상 ({before}→{after}자)"

    had, has = numbers(masked), numbers(out)
    if had - has:
        return None, "숫자가 빠지거나 바뀜: " + _short(sorted((had - has).elements()))
    if has - had:
        return None, "원문에 없는 숫자가 생김: " + _short(sorted((has - had).elements()))

    _, added = mask(TOKEN_RE.sub(" ", out))  # code, links, URLs or paths the model wrote itself
    invented = [s.text for s in added or [] if s.core() not in original]
    if invented:
        return None, "원문에 없는 코드·링크·경로가 생김: " + _short(invented)

    for i in blocks:  # the whole line, so the block keeps its own indentation
        out = re.sub(r"(?m)^[ \t]*" + re.escape(TOKEN.format(i)) + r"[ \t]*$", lambda m, s=spans[int(i)]: s.text, out)
    out = TOKEN_RE.sub(lambda m: spans[int(m.group(1))].text, out)
    return out, None
