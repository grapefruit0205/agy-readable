"""Which answers need translating, and how a long one is cut for it.

An answer needs translating when any of its sentences, table cells or headings is in another language:
no Hangul, and either three or more Latin words with at least one everyday function word ("the", "is",
"and", "que", "und", ...), or two or more letters of a script other than Latin and Hangul (Japanese, Chinese,
Cyrillic, ...). Code, links, URLs and paths are masked first (protect.mask), so a Korean answer quoting an
English command in backticks is not taken for English. The function words keep a Korean answer with bare
names ("CloudFront + WAF", "Security & Access") or a command written without backticks
("terraform plan -target") in the Korean rewrite.

A long answer is cut at blank lines outside fenced code blocks into pieces of about config.CHUNK_CHARS, so
the pieces can be translated side by side; a table or a list with no blank line in it stays in one piece.
"""
import re

from agy_readable import protect

HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")
LATIN_WORD = re.compile(r"[A-Za-zÀ-ɏ]+(?:'[A-Za-z]+)?")
SEGMENT = re.compile(r"(?<=[.?!:;])\s+|\s*\|\s*")  # sentences, and table cells
FUNCTION_WORDS = {
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "and", "or", "but", "to", "of", "in", "on", "for",
    "with", "that", "this", "it", "its", "as", "at", "by", "from", "not", "no", "have", "has", "had", "will",
    "would", "can", "could", "should", "do", "does", "did", "you", "your", "we", "our", "they", "i", "i'm",
    "it's", "there", "here", "what", "which", "when", "if", "so", "than", "then", "only", "also", "into",
    # French, Spanish, Portuguese, Italian, German
    "le", "la", "les", "des", "du", "et", "est", "une", "el", "los", "las", "y", "que", "es", "una", "por",
    "para", "com", "não", "il", "di", "che", "der", "die", "das", "und", "ist", "nicht", "ein", "eine", "mit",
}


def _other_script(ch):
    return ch.isalpha() and not ch.isascii() and not "À" <= ch <= "ɏ" and not HANGUL.match(ch)


def foreign(text):
    """The pieces of `text` that are in another language, in order."""
    masked, _ = protect.mask(text)
    prose = protect.TOKEN_RE.sub(" ", masked)
    found = []
    for line in prose.splitlines():
        for seg in SEGMENT.split(line):
            if not seg.strip() or HANGUL.search(seg):
                continue
            words = [w.lower() for w in LATIN_WORD.findall(seg)]
            if len(words) >= 3 and any(w in FUNCTION_WORDS for w in words):
                found.append(seg.strip())
            elif sum(1 for c in seg if _other_script(c)) >= 2:
                found.append(seg.strip())
    return found


def needs_translation(text):
    return bool(foreign(text))


def chunks(text, size):
    """`text` cut at blank lines outside fenced code blocks into pieces of about `size` characters (one
    paragraph longer than that stays whole). Joined with a blank line, the pieces give the text back up to
    runs of blank lines between paragraphs."""
    paragraphs, cur, fence = [], [], None
    for line in text.strip("\n").split("\n"):
        m = re.match(r"^[ \t]*(`{3,}|~{3,})", line)
        if m:
            fence = None if fence and m.group(1).startswith(fence) else fence or m.group(1)
        if not line.strip() and fence is None:
            if cur:
                paragraphs.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        paragraphs.append("\n".join(cur))
    out = []
    for p in paragraphs:
        if out and len(out[-1]) + 2 + len(p) <= size:
            out[-1] += "\n\n" + p
        else:
            out.append(p)
    return out or [text]
