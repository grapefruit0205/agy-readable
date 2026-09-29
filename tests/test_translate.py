"""Answers with sentences in another language: found (lang.py), translated into Korean, and never shown
as the original once agy has written a translation."""
import os
import time
import unittest

from support import clean_env, encode, event, new_data_dir, read_jsonl, remove, run_hook

from agy_readable import daemon, lang, protect

ENGLISH = """I fixed the final deck and saved it as a new file. The original was only read, not changed.

- The deck passes the file check, and 29 slides were rendered.
- `fix_final.py` changed 1,024 lines in [the notes](docs/notes.md).

```bash
python3 fix_final.py --out ~/deck/fixed.pptx
```

If you want me to fix the handoff lines too, tell me which slides.
"""


class LangTest(unittest.TestCase):
    def test_finds_other_languages(self):
        for text in ("The image exported, and I'm checking that the new labels fit.",
                     "지금 확인 중입니다. The deck passes the file check.",
                     "| Slide | What changed |\n|---|---|\n| 2 | The title fits on one line |",
                     "スライドを確認しています", "Проверяю слайды сейчас", "Je vérifie les diapositives et la note"):
            self.assertTrue(lang.needs_translation(text), text)

    def test_leaves_korean_with_names_and_commands(self):
        for text in ("(keep-alive)", "Done.", "보안 · 관리 접속 (Security & Access / Monitoring & Alerts)",
                     "| web | t3.small | 2 vCPU | 2 GiB |", "CloudFront + WAF 를 거칩니다.",
                     "`terraform plan -target=aws_instance.web` 을 돌려 보세요.",
                     "terraform plan -target aws_instance.web", "에러는 \"Too many connections\" 였습니다.",
                     "```\nThis is code, not an answer to translate.\n```"):
            self.assertFalse(lang.needs_translation(text), text)

    def test_chunks_keep_code_blocks_and_everything(self):
        block = "```\nline one\n\nline two after a blank line\n```"
        text = "\n\n".join(["para %d " % i + "word " * 30 for i in range(12)] + [block, "end"])
        pieces = lang.chunks(text, 400)
        self.assertGreater(len(pieces), 3)
        self.assertTrue(all(len(p) <= 400 or "\n\n" not in p.replace(block, "") for p in pieces))
        self.assertIn(block, pieces[-1] if block in pieces[-1] else "".join(pieces))
        self.assertEqual("\n\n".join(pieces), text)

    def test_restore_loose_keeps_every_part(self):
        masked, spans = protect.mask(ENGLISH)
        out = masked.replace("⟦0⟧", "", 1)  # a placeholder lost
        shown = protect.restore_loose(spans, out)
        for s in spans:
            self.assertIn(s.text, shown)


class TranslateHookTest(unittest.TestCase):
    def setUp(self):
        self.data = new_data_dir()
        self.env = clean_env(self.data, AGY_READABLE_DAEMON="0")

    def tearDown(self):
        remove(self.data)

    def hook(self, text=ENGLISH, **env):
        return run_hook(dict(self.env, **env), event(text))

    def last_log(self):
        return read_jsonl(os.path.join(self.data, "hook.log"))[-1]

    def test_english_is_translated_with_protected_parts_back(self):
        out = self.hook()
        self.assertTrue(out.startswith(encode("[번역 model=gemini-3.8-flash-low]")))
        self.assertEqual(out.split("\n", 1)[1], ENGLISH.strip())
        with open(os.path.join(self.data, "rec.txt"), encoding="utf-8") as f:
            sent = f.read()
        self.assertNotIn("fix_final.py", sent)
        self.assertEqual((self.last_log()["outcome"], self.last_log()["mode"]), ("translated", "both"))

    def test_short_english_is_translated_too(self):
        out = self.hook("The image exported, and I'm checking that the new labels fit.")
        self.assertTrue(out.startswith("[번역"))
        self.assertEqual(self.last_log()["mode"], "translate")  # a progress line is translated, not rebuilt

    def test_mixed_answer_is_translated_not_rewritten(self):
        out = self.hook("설정을 바꿨습니다. " * 30 + "\n\nThe deck passes the file check.")
        self.assertTrue(out.startswith("[번역"))

    def test_rejected_twice_shows_the_translation_not_the_original(self):
        out = self.hook(FAKE_MODE="dropnum")
        self.assertTrue(out.startswith("[번역"))
        self.assertIn("번역이 검사에 걸렸지만 번역본 표시", out)
        self.assertIn("`fix_final.py`", out)
        self.assertEqual(self.last_log()["outcome"], "translated_loose")

    def test_lost_placeholder_is_put_back_at_the_end(self):
        out = self.hook(FAKE_MODE="droptoken")
        self.assertTrue(out.startswith("[번역"))
        self.assertIn("번역에서 자리를 잃은", out)
        for kept in ("`fix_final.py`", "[the notes](docs/notes.md)", "~/deck/fixed.pptx"):
            self.assertIn(kept, out)

    def test_code_block_moved_into_a_sentence_stays_a_block(self):
        out = self.hook(FAKE_MODE="blocksentence")
        self.assertIn("\n```bash\npython3 fix_final.py --out ~/deck/fixed.pptx\n```\n", out)

    def test_agy_failing_shows_the_original_with_a_note(self):
        out = self.hook(FAKE_MODE="fail")
        self.assertTrue(out.startswith(ENGLISH.rstrip()))
        self.assertIn("번역 생략", out)

    def test_long_answer_goes_in_pieces(self):
        prompts = os.path.join(self.data, "prompts.txt")
        text = "\n\n".join(f"Step {i}: the deck was checked and the file was saved." for i in range(40))
        out = self.hook(text, AGY_READABLE_CHUNK_CHARS="600", AGY_READABLE_MAX_CHARS="1000", FAKE_PROMPTS=prompts)
        with open(prompts, encoding="utf-8") as f:
            sent = [p for p in f.read().split("\n=====\n") if p.strip() and "[다시 쓴 글]" not in p]
        self.assertGreater(len(sent), 3)
        self.assertEqual(out.count("[번역"), len(sent))
        for i in range(40):
            self.assertIn(f"Step {i}:", out)
        self.assertEqual(self.last_log()["mode"], "translate_pieces")
        self.assertTrue(self.last_log()["review"].startswith(f"{len(sent)}조각"))

    def test_translate_off(self):
        text = "The image exported, and I'm checking that the new labels fit."
        self.assertEqual(self.hook(text, AGY_READABLE_TRANSLATE="0"), text)

    def test_too_long_is_left(self):
        text = "The deck was checked and saved. " * 50
        self.assertEqual(self.hook(text, AGY_READABLE_TRANSLATE_MAX="100"), text)


class TranslateDaemonTest(unittest.TestCase):
    """Pieces of a long answer go to the daemon at once, each to its own single-use agy."""

    def setUp(self):
        self.data = new_data_dir()
        self.env = clean_env(self.data, FAKE_STARTUP="0.5", FAKE_GEN="1.0")
        self.old = os.environ.get("CLAUDE_PLUGIN_DATA")
        os.environ["CLAUDE_PLUGIN_DATA"] = self.data  # so daemon.call below reaches this test's daemon

    def tearDown(self):
        try:
            daemon.call({"op": "stop"}, 5)
        except (OSError, ValueError):
            pass
        if self.old is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = self.old
        remove(self.data)

    def test_pieces_run_side_by_side(self):
        text = "\n\n".join(f"Step {i}: the deck was checked and the file was saved." for i in range(24))
        start = time.time()
        out = run_hook(dict(self.env, AGY_READABLE_CHUNK_CHARS="400", AGY_READABLE_MAX_CHARS="1000"), event(text))
        took = time.time() - start
        pieces = out.count("[번역")
        self.assertGreater(pieces, 3)
        for i in range(24):
            self.assertIn(f"Step {i}:", out)
        self.assertLess(took, pieces * 1.0)  # one after another would take at least FAKE_GEN per piece


if __name__ == "__main__":
    unittest.main()
