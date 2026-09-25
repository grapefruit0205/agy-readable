"""The second pass: agy checks its own free rewrite against the original and fixes only what is wrong.

A free rewrite (headings, a summary, questions and answers) reads better but may add a reason, an example
or a stronger word the original never had. Asked to "fix the mistakes", agy finds few of its own; pointed at
the sentences, it fixes most. So the rewrite is cut into numbered sentences, each sentence with a word,
number or code the original lacks is listed under it as a suspect, and so is one that repeats a claim the
original marks as an estimate (protect.HEDGES) without the mark; with them go the numbers, placeholders and
marks the rewrite lost, and agy answers with only the lines to change:

    [12] the whole sentence, fixed        [12] (삭제)        [12+] a sentence to add after 12

Words new to the original are only candidates: most are the plainer words the rewrite was asked for, and agy
is told to leave those alone. Everything here works on masked text (protect.mask), so the prompt carries no
code, link or path, and the edited text goes through protect.restore like any rewrite.
"""
import collections
import os
import re

from agy_readable import protect

PROMPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt_review_ko.txt")
HANGUL = re.compile(r"[가-힣]{2,}")
LATIN = re.compile(r"[A-Za-z][A-Za-z0-9\-]+")
WORD = re.compile(r"[가-힣]{2,}|[A-Za-z][A-Za-z0-9\-]+|\d+(?:[.,]\d+)*")
PREFIX = re.compile(r"(?:#{1,6}\s+|>\s*)?(?:[-*+]\s+|\d+[.)]\s+)?")
# a sentence ends at . ? ! (with closing ** " ) after it) before a space; not after one capital, as in "Q. ..."
SENT = re.compile(r".+?(?:(?<!\b[A-Z])[.?!][*_\"'”’)\]]*(?=\s)|[.?!][*_\"'”’)\]]*$|$)")
SKIP = re.compile(r"^\s*(?:⟦\d+⟧\s*)?$|^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")  # blank, a lone placeholder, |---|
BARE = re.compile(r"^(?:#{1,6}|>|[-*+]|\d+[.)])?$")  # what is left of a line whose sentences were all deleted
RULE = r"[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*"  # a horizontal rule
EDIT = re.compile(r"^\s*\[(\d+)(\+?)\]\s?(.*)$")
DELETE = ("(삭제)", "삭제")
# endings and linking words: new to the original without adding anything to what it says
STOP = {"중입니다", "위함입니다", "때문입니다", "현재", "해당", "통해", "위한", "위해", "함께", "발생", "경우", "이를", "이는",
        "또한", "따라서", "그래서", "그리고", "하지만", "관련", "통한", "대한", "대해", "위하여", "등의", "등을", "요약"}
MARKUP = re.compile(r"[*_`>#|]+|^\s*(?:[-+]\s+|\d+[.)]\s+)", re.M)


def units(lines):
    """The rewrite's sentences as (line, start, end). A heading or table row is one unit; the first sentence
    of a line keeps its list mark or heading level, so agy sees and rewrites it with it."""
    out, fence = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence or SKIP.match(line):
            continue
        indent = len(line) - len(line.lstrip())
        body = line[indent:]
        if body.startswith(("#", "|")):
            out.append((i, indent, len(line)))
            continue
        first = True
        for m in SENT.finditer(body, PREFIX.match(body).end()):
            if m.group(0).strip():
                lead = len(m.group(0)) - len(m.group(0).lstrip())
                out.append((i, indent if first else indent + m.start() + lead, indent + m.end()))
                first = False
    return out


def _known(tok, original):
    # Korean puts particles and endings on the word: known if a leading part of 2+ letters is in the original
    return any(tok[:n] in original for n in range(len(tok), 1, -1))


def novel(text, original):
    """Words in `text` the original does not have."""
    low = original.lower()
    words = [t for t in HANGUL.findall(text) if t not in STOP and not _known(t, original)]
    words += [t for t in LATIN.findall(text) if t.lower() not in low]
    return list(dict.fromkeys(words))


def sentences(text):
    """The original's sentences, line by line, without list or heading numbers."""
    out = []
    for line in text.split("\n"):
        line = protect.LIST_NUMBER.sub("", line).strip()
        out += [x.strip() for x in re.split(r"(?<=[.?!])\s+", line) if x.strip()]
    return out


def _flat(text):
    return " ".join(MARKUP.sub("", text).split())


def _pairs(text):
    """Neighbouring words, Korean ones cut to their first two letters (particles and endings vary)."""
    w = [x[:2] if x[0] >= "가" else x.lower() for x in WORD.findall(protect.TOKEN_RE.sub(" ", text)) if x not in STOP]
    return {(a, b) for a, b in zip(w, w[1:]) if not (a[0].isdigit() and b[0].isdigit())}


def unmarked(masked, texts):
    """Sentences that repeat a claim the original marks (protect.HEDGES) without the mark, as a summary does:
    {sentence number: (mark, the original sentences)}. A claim is two neighbouring words found in at most two
    of the original's sentences, the marked one or the one before it; a question, a sentence with the mark
    next to it, and one copied from the original as it was are left alone."""
    sents = sentences(masked)
    freq = collections.Counter(p for t in sents for p in _pairs(t))
    marked = []
    for k, t in enumerate(sents):
        hs = [h for h in protect.HEDGES if h in t]
        if hs:
            ctx = " ".join(sents[max(0, k - 1):k + 1])
            marked.append((hs, ctx, {p for p in _pairs(ctx) if freq[p] <= 2}))
    flat, found = _flat(masked), {}
    for n, u in enumerate(texts):
        if _flat(u) in flat or _flat(u).endswith("?"):
            continue
        near = " ".join(texts[max(0, n - 1):n + 2])
        for hs, ctx, claim in marked:
            if not any(h in near for h in hs) and claim & _pairs(u):
                found[n + 1] = (hs[0], ctx)
                break
    return found


def suspects(original, masked, lines, us):
    """Per sentence, what the original lacks: {sentence number: [words, numbers, code, a mark]}."""
    known_numbers = set(protect.numbers(masked))
    texts = [lines[i][s:e] for i, s, e in us]
    bare = unmarked(masked, texts)
    found = {}
    for n, u in enumerate(texts, 1):
        f = novel(u, masked)[:8]
        f += [f"숫자 {x}" for x in protect.numbers(u) if x not in known_numbers]
        _, added = protect.mask(protect.TOKEN_RE.sub(" ", u))
        f += [f"코드 모양 {x.text} (원문에서는 코드가 아님)" for x in added or [] if x.core() not in original]
        if n in bare:
            h, ctx = bare[n]
            f.append(f"원문에서 '{h}' 단서가 붙은 내용인데 단서가 없음. 원문: {ctx}")
        if f:
            found[n] = f
    return found


def missing(masked, draft):
    """What the rewrite lost: numbers, placeholders and marks of certainty, each with the original sentences
    it is in, those the rewrite did not keep as they were first."""
    rows, sents, flat = [], sentences(masked), _flat(draft)

    def where(has, n):
        found = [t for t in sents if has(t)]
        return " / ".join(sorted(found, key=lambda t: _flat(t) in flat)[:n])

    for x in sorted((protect.numbers(masked) - protect.numbers(draft)).keys()):
        rows.append(f"- 빠진 숫자 {x}. 원문 문장: " + where(lambda t: x in protect.numbers(t), 3))
    for t in sorted(set(protect.TOKEN_RE.findall(masked)) - set(protect.TOKEN_RE.findall(draft)), key=int):
        tok = protect.TOKEN.format(t)
        rows.append(f"- 빠진 표시 {tok}. 원문 문장: " + where(lambda s: tok in s, 1))
    had, has = protect.hedges(masked), protect.hedges(draft)
    for h in sorted(had - has):
        rows.append(f"- 빠진 단서 '{h}' (원문 {had[h]}번, 다시 쓴 글 {has[h]}번). 원문 문장: "
                    + where(lambda s: h in s, had[h]))
    return rows


def build(original, masked, draft):
    """(prompt, the draft's lines, its sentences, how many suspects), or (None, ...) when nothing is suspect."""
    lines = draft.split("\n")
    us = units(lines)
    listed = [f"- [{n}] " + ", ".join(f) for n, f in suspects(original, masked, lines, us).items()]
    listed += missing(masked, draft)
    if not listed:
        return None, lines, us, 0
    with open(PROMPT, encoding="utf-8") as f:
        base = f.read()
    numbered = "\n".join(f"[{n}] {lines[i][s:e].strip()}" for n, (i, s, e) in enumerate(us, 1))
    prompt = (base + "[원문]\n" + masked + "\n\n[다시 쓴 글]\n" + numbered + "\n\n[의심 목록]\n"
              + "\n".join(listed) + "\n")
    return prompt, lines, us, len(listed)


def apply(lines, us, answer):
    """Put agy's fixes into the rewrite. Returns (text, {"고침": n, "삭제": n, "넣음": n}, lines not understood)."""
    edits, bad = [], []
    for raw in answer.splitlines():
        m = EDIT.match(raw)
        if not m:
            if raw.strip() and raw.strip() != "없음":
                bad.append(raw.strip())
            continue
        n, plus, text = int(m.group(1)), m.group(2) == "+", m.group(3).strip()
        if not 1 <= n <= len(us) or not text:
            bad.append(raw.strip())
            continue
        edits.append((n, plus, text))
    was = [bool(x.strip()) for x in lines]
    lines = list(lines)
    after = collections.defaultdict(list)  # new lines after a heading or table row
    done = collections.Counter()
    # from the end backwards, so an edit never moves the place of one still to come
    for n, plus, text in sorted(edits, key=lambda x: (us[x[0] - 1][0], us[x[0] - 1][1], x[1]), reverse=True):
        i, s, e = us[n - 1]
        whole = lines[i][s:e].lstrip()[:1] in ("#", "|") and e == len(lines[i])
        if plus:
            done["넣음"] += 1
            if whole and lines[i].lstrip().startswith("|") and not text.startswith("|"):
                while i + 1 < len(lines) and lines[i + 1].lstrip().startswith("|"):
                    i += 1  # after the whole table, or the sentence would become a row
                after[i][:0] = ["", text]
            elif whole:
                after[i].insert(0, text)
            else:
                lines[i] = lines[i][:e] + " " + text + lines[i][e:]
        elif text in DELETE:
            done["삭제"] += 1
            lines[i] = (lines[i][:s] + lines[i][e:]).rstrip()
        else:
            done["고침"] += 1
            mark = PREFIX.match(lines[i][s:e]).group(0)
            if mark.strip() and not text.startswith(mark.strip()):
                text = mark + text  # keep the list mark or heading level agy left off
            lines[i] = lines[i][:s] + text + lines[i][e:]
    out = []
    for i, line in enumerate(lines):
        if not (was[i] and BARE.match(line.strip())):
            out.append(line)
        out.extend(after.get(i, []))
    text = "\n".join(out)
    if done["삭제"]:  # what a deleted block leaves: runs of blank lines, rules with nothing between or before them
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(rf"(?m)^{RULE}\n(?:[ \t]*\n)*(?={RULE}$)", "", text)
        text = re.sub(rf"^(?:[ \t]*\n|{RULE}\n)+", "", text)
        text = re.sub(rf"(?:\n[ \t]*|\n{RULE})+$", "", text)
    return text, dict(done), bad
