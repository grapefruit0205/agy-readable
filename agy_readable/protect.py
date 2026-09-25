"""Keep what must not change out of the model's hands, and check what it has to handle.

Before an answer goes to agy, fenced code blocks, inline code, Markdown links, URLs and file paths are
replaced by placeholders (⟦0⟧, ⟦1⟧, ...). The rewritten text must carry every placeholder; a code block's
exactly once, alone on its line and in the original order, while an inline one may appear again (a summary
at the top may name the same file). They are then put back byte for byte, so these parts cannot change.
A ⟦n⟧ already in the answer (one explaining this plugin, say) is protected the same way, so it cannot be
taken for a placeholder.

Numbers stay in the text, since the sentences are written around them. Every number of the original, with
its sign, must appear at least as often as it does there, and no other number may appear; a summary may
repeat one. List and heading numbering is left out, as it may become bullets or new sections.

Marks that say how sure a statement is ("추정", "확인된 사실", ...) are counted the same way: a rewrite must
keep at least as many of each as the original has, so none can be dropped quietly.

Paths and URLs may contain Korean (~/문서/설정.json). Korean writes particles right after a word, so where a
path ends is not always clear: a particle after an English name or an extension ("README를", "설정.json에")
is left to the sentence, but after a Korean name ("~/문서에") it cannot be told from the name, and the path is
kept with it. Protecting a particle too costs a little wording; leaving part of a path to the model could
change it unnoticed.

What this cannot catch: two values of the same kind trading places ("A is 10, B is 20" -> "A is 20, B is 10"),
a number dropped in one place while the same number stays elsewhere, or a sentence whose meaning shifts
without any number, code, path or mark changing. The review step (review.py) looks for those.
"""
import re
from collections import Counter

TOKEN = "⟦{}⟧"
TOKEN_RE = re.compile(r"⟦(\d+)⟧")
ASIDE = "\ue000{}\ue001"  # a ⟦n⟧ of the answer's own, set aside while the placeholders are made
ASIDE_RE = re.compile("\ue000(\\d+)\ue001")
FENCED = re.compile(r"(?ms)^([ \t]*)(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\2[`~]*[ \t]*$|\Z)")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1)[^\n])+\1")
MD_LINK = re.compile(r"!?\[[^\]\n]*\]\([^)\s]+(?:\s+\"[^\"\n]*\")?\)|<https?://[^>\s]+>")
URL = re.compile(r"https?://[\w\-.~:/?#\[\]@!$&'()*+,;=%]+")  # \w: letters of any script, so Korean too
PATH = re.compile(r"(?<![\w/.~\-:])((?:~|\.{1,2})?/[\w.\-]+(?:/[\w.\-]+)*/?|[\w.\-]+(?:/[\w.\-]+)+/?)")
# a Korean particle or ending right after an English letter or a closing bracket: "README를", "app.conf에서"
PARTICLE = re.compile(r"(?<=[A-Za-z)\]])(?:이|가|을|를|은|는|의|에|에게|께|로|으로|와|과|도|만|나|이나|랑|이랑|하고|까지|부터|"
                      r"처럼|보다|라는|이라는|라고|이라고|이고|이며|이다|입니다|이에요|예요|인데|이면|이지만|이라|인|이죠)"
                      r"(?:는|도|만|서|요|의)?$")
NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)*")
LIST_NUMBER = re.compile(r"(?m)^[ \t]*(?:[-*+][ \t]+|#{1,6}[ \t]+)?\d+[.)][ \t]")
HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t].*$")
CHAPTER = re.compile(r"\d+(?=[ \t]?[장절])")  # "### 3장: 설계", in a heading: the original's own section numbers
HEDGES = ("추정", "확인된 사실", "확인한 사실", "일반적인")  # how sure a statement is; see hedges()


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
    if re.search(r"\.[A-Za-z][A-Za-z0-9]{0,7}/?$", s):  # a/b.ext
        return True
    # a/b/c; not "A/B", "TCP/IP", "1/2", "and/or", "HTTP/HTTPS/TLS", nor Korean word lists like "빌드/테스트/배포"
    if re.fullmatch(r"[A-Z][A-Z0-9]*(?:/[A-Z][A-Z0-9]*)+", s):
        return False
    return s.count("/") >= 2 and re.search(r"[A-Za-z0-9]", s) is not None


def _path_end(p):
    """Leave a trailing period or comma, and a particle after an English name, to the sentence."""
    while True:
        before = p
        p = PARTICLE.sub("", p)
        while p.endswith((".", ",")) and not p.endswith(("/.", "..")):
            p = p[:-1]
        if p == before:
            return p


def _url_end(url):
    """Leave trailing punctuation, an unbalanced closing bracket and a particle after it to the sentence."""
    while True:
        before = url
        url = PARTICLE.sub("", url)
        while url and url[-1] in ".,;:!?'\"":
            url = url[:-1]
        while url and url[-1] in ")]" and url.count(url[-1]) > url.count("(" if url[-1] == ")" else "["):
            url = url[:-1]
        if url == before:
            return url


def mask(text):
    """Returns (text with placeholders, spans)."""
    spans, own = [], []

    def aside(m):
        own.append(m.group(0))
        return ASIDE.format(len(own) - 1)

    # the answer's own ⟦n⟧ are set aside first, put back inside any code or link that holds them,
    # and protected themselves where they stand in the prose
    text = TOKEN_RE.sub(aside, text)

    def keep(original, block=False):
        spans.append(Span(ASIDE_RE.sub(lambda m: own[int(m.group(1))], original), block))
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
        p = _path_end(m.group(0))
        return keep(p) + m.group(0)[len(p):] if _is_path(p) else m.group(0)

    text = URL.sub(url, text)
    text = PATH.sub(path, text)
    text = ASIDE_RE.sub(lambda m: keep(own[int(m.group(1))]), text)
    return text, spans


def numbers(text):
    """Numbers outside placeholders, list numbering and section numbers in headings, with their sign;
    thousands separators dropped. A free rewrite may regroup the sections, and their numbers with them."""
    prose = HEADING.sub(lambda m: CHAPTER.sub("", m.group(0)), TOKEN_RE.sub(" ", text))
    prose = LIST_NUMBER.sub("", prose)
    found = Counter()
    for m in NUMBER.finditer(prose):
        i = m.start()
        minus = i > 0 and prose[i - 1] in "-−" and (i == 1 or not prose[i - 2].isalnum())
        found[("-" if minus else "") + m.group(0).replace(",", "")] += 1
    return found


def hedges(text):
    """How often each mark of certainty appears."""
    return Counter({h: text.count(h) for h in HEDGES if h in text})


def _short(items):
    return ", ".join(s if len(s) <= 30 else s[:30] + "…" for s in items[:3])


def clean(out):
    """The model's answer without surrounding blank lines, or the fence it wrapped the whole answer in."""
    out = out.strip()
    if out.startswith("```"):
        out = re.sub(r"^```[^\n]*\n|\n?```$", "", out).strip()
    return out


def restore(original, masked, spans, out):
    """Check the model's rewrite of `masked` and put the protected parts back.
    Returns (text, None), or (None, why it cannot be shown)."""
    out = clean(out)
    if "```" in out or "~~~" in out:
        return None, "원문에 없는 코드 블록이 생김"

    seen = Counter(TOKEN_RE.findall(out))
    ids = [str(i) for i in range(len(spans))]
    missing = [spans[int(i)].text for i in ids if not seen[i]]
    if missing:
        return None, "코드·링크·경로 일부가 빠짐: " + _short(missing)
    blocks = [i for i in ids if spans[int(i)].block]
    if any(seen[i] > 1 for i in blocks) or set(seen) - set(ids):
        return None, "코드·링크·경로 자리 표시가 중복되거나 바뀜"
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
    if set(has) - set(had):
        return None, "원문에 없는 숫자가 생김: " + _short(sorted(set(has) - set(had)))
    lost = hedges(masked) - hedges(out)
    if lost:
        return None, "단서가 빠짐: " + _short(sorted(lost))

    _, added = mask(TOKEN_RE.sub(" ", out))  # code, links, URLs or paths the model wrote itself
    invented = [s.text for s in added or [] if s.core() not in original]
    if invented:
        return None, "원문에 없는 코드·링크·경로가 생김: " + _short(invented)

    def put_back(m):  # a code block takes its whole line, so it keeps its own indentation
        i = int(m.group(1) or m.group(2))
        if m.group(1) and spans[i].block:
            return spans[i].text
        return m.group(0).replace(TOKEN.format(i), spans[i].text)

    # one pass, so a ⟦n⟧ inside what was put back (the answer's own) is left as it is
    return re.sub(r"(?m)^[ \t]*⟦(\d+)⟧[ \t]*$|⟦(\d+)⟧", put_back, out), None
