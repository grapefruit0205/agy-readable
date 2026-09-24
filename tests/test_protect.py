"""What the model may and may not change: placeholders for code, links, URLs and paths, and the number check.
Each case gives the model's output for the masked text, the way agy would return it."""
import unittest

from support import SAMPLE  # noqa: F401  (puts the repo on sys.path)

from agy_readable import protect


def rewrite(original, edit):
    """Mask `original`, let `edit` play the model on the masked text, check and restore."""
    masked, spans = protect.mask(original)
    return protect.restore(original, masked, spans, edit(masked))


def tok(original, text):
    """The placeholder that stands for `text` in the masked `original`."""
    _, spans = protect.mask(original)
    return protect.TOKEN.format(next(i for i, s in enumerate(spans) if s.text == text))


RICH = """설정은 `config.load()`가 읽습니다. 설정 파일은 /etc/app/app.conf 이고, 문서는 https://example.com/docs/a. 입니다.
[설계 메모](docs/cache.md)를 보세요. 값은 1,024이고 최저 온도는 -3도입니다.

1. 첫째 단계
2. 둘째 단계

- 목록 안의 코드:
  ```sh
  rm -rf build
  ```

```python
print("x = 10")
```
A/B 테스트와 TCP/IP, 1/2 비율은 그대로 두고 src/app/main.py. 를 고칩니다.
"""


class MaskTest(unittest.TestCase):
    def test_round_trip_is_exact(self):
        masked, spans = protect.mask(RICH)
        self.assertEqual(protect.restore(RICH, masked, spans, masked), (RICH.strip(), None))

    def test_what_is_protected(self):
        _, spans = protect.mask(RICH)
        texts = [s.text for s in spans]
        for expected in ("`config.load()`", "/etc/app/app.conf", "https://example.com/docs/a",
                         "[설계 메모](docs/cache.md)", "src/app/main.py", "  ```sh\n  rm -rf build\n  ```"):
            self.assertIn(expected, texts)
        for kept_as_prose in ("A/B", "TCP/IP", "1/2"):
            self.assertFalse(any(kept_as_prose == t for t in texts), kept_as_prose)

    def test_code_numbers_are_not_prose_numbers(self):
        masked, _ = protect.mask(RICH)
        self.assertEqual(protect.numbers(masked), {"1024": 1, "-3": 1, "1": 1, "2": 1})  # 1/2 ratio; list numbers left out

    def test_signs(self):
        self.assertEqual(protect.numbers("gemini-3.8 모델, 3-5개, (-3), x=-2"),
                         {"3.8": 1, "3": 1, "5": 1, "-3": 1, "-2": 1})

    def test_text_with_placeholder_syntax_is_not_masked(self):
        self.assertEqual(protect.mask("이미 ⟦1⟧ 이 있음"), (None, None))

    def test_unclosed_fence_is_one_block(self):
        text = "설명입니다.\n```sh\nmake all\n"
        masked, spans = protect.mask(text)
        self.assertEqual(len(spans), 1)
        self.assertEqual(masked.strip(), "설명입니다.\n⟦0⟧")


class RejectTest(unittest.TestCase):
    """The cases the review found passing the old check, and the other ways a rewrite can go wrong."""

    def rejected(self, original, edit, why):
        out, reason = rewrite(original, edit)
        self.assertIsNone(out, f"should be rejected: {why}")
        self.assertIn(why, reason)

    def test_sign_dropped(self):
        self.rejected("오늘 최저 온도는 -3도이고 내일은 비가 옵니다.", lambda m: m.replace("-3", "3"), "숫자가 빠지거나 바뀜: -3")

    def test_repeated_number_dropped(self):
        self.rejected("재시도는 3회, 대기는 3초입니다.", lambda m: "재시도는 3회입니다.", "숫자가 빠지거나 바뀜: 3")

    def test_number_added(self):
        self.rejected("재시도는 3회입니다.", lambda m: "재시도는 3회, 대기는 5초입니다.", "원문에 없는 숫자가 생김: 5")

    def test_hangul_number_turned_into_digits(self):
        self.rejected("방법은 세 가지입니다.", lambda m: "방법은 3가지입니다.", "원문에 없는 숫자가 생김: 3")

    def test_path_written_differently(self):
        text = "설정은 /etc/nginx/nginx.conf 에 있습니다."
        self.rejected(text, lambda m: m.replace(tok(text, "/etc/nginx/nginx.conf"), "/etc/nginx/conf.d"),
                      "코드·링크·경로 일부가 빠짐: /etc/nginx/nginx.conf")

    def test_url_written_differently(self):
        text = "문서는 https://example.com/docs/a 를 참고하세요."
        self.rejected(text, lambda m: m.replace(tok(text, "https://example.com/docs/a"), "https://example.com/docs/b"),
                      "코드·링크·경로 일부가 빠짐")

    def test_short_inline_code_dropped(self):
        text = "`-f` 옵션을 쓰세요. 파일은 a-file 입니다."
        self.rejected(text, lambda m: "옵션 없이 쓰세요. 파일은 a-file 입니다.", "코드·링크·경로 일부가 빠짐: `-f`")

    def test_code_block_moved_into_a_sentence(self):
        text = "예:\n```sh\nrm -rf build\n```\n이렇게 지웁니다."
        self.rejected(text, lambda m: "예: ⟦0⟧ 를 실행해 지웁니다.", "코드 블록이 문장 속으로 옮겨짐")

    def test_code_blocks_reordered(self):
        text = "먼저:\n```sh\nmake\n```\n다음:\n```sh\nmake install\n```\n"
        self.rejected(text, lambda m: "다음:\n⟦1⟧\n먼저:\n⟦0⟧", "코드 블록 순서가 바뀜")

    def test_placeholder_repeated(self):
        text = "`make`를 실행하세요. 설명이 조금 더 있습니다."
        self.rejected(text, lambda m: m + " 다시 ⟦0⟧", "중복되거나 바뀜")

    def test_unknown_placeholder(self):
        text = "`make`를 실행하세요. 설명이 조금 더 있습니다."
        self.rejected(text, lambda m: m + " ⟦7⟧", "중복되거나 바뀜")

    def test_new_code_block(self):
        self.rejected("빌드하려면 make를 실행합니다.", lambda m: m + "\n```sh\nmake\n```", "원문에 없는 코드 블록이 생김")

    def test_invented_inline_code(self):
        self.rejected("빌드 폴더를 지우세요.", lambda m: "`rm -rf build`로 빌드 폴더를 지우세요.", "원문에 없는 코드·링크·경로가 생김")

    def test_invented_path(self):
        self.rejected("설정 파일을 고치세요.", lambda m: "설정 파일 /etc/app.conf 를 고치세요.", "원문에 없는 코드·링크·경로가 생김")

    def test_length_out_of_range(self):
        text = "이 답변은 길게 설명합니다. " * 10
        self.rejected(text, lambda m: "짧음", "결과 길이가 비정상")


class AllowTest(unittest.TestCase):
    def allowed(self, original, edit):
        out, reason = rewrite(original, edit)
        self.assertIsNone(reason)
        return out

    def test_list_numbering_to_bullets(self):
        self.allowed("순서:\n1. 빌드\n2. 배포\n", lambda m: m.replace("1. ", "- ").replace("2. ", "- "))

    def test_thousands_separator(self):
        self.allowed("크기는 1,024 KB입니다.", lambda m: "크기는 1024 KB입니다.")

    def test_inline_parts_reordered_and_restored_exactly(self):
        text = "`make`로 빌드하고 `make install`로 설치합니다."
        out = self.allowed(text, lambda m: "설치는 ⟦1⟧, 빌드는 ⟦0⟧로 합니다.")
        self.assertEqual(out, "설치는 `make install`, 빌드는 `make`로 합니다.")

    def test_backticks_around_an_existing_word(self):
        self.allowed("config 파일을 고치세요.", lambda m: "`config` 파일을 고치세요.")

    def test_whole_answer_wrapped_in_a_fence(self):
        text = "`make`를 실행하세요. 설명이 조금 더 있습니다."
        out = self.allowed(text, lambda m: "```markdown\n" + m + "\n```")
        self.assertEqual(out, text)

    def test_indented_block_keeps_its_indentation(self):
        text = "- 목록:\n  ```sh\n  make\n  ```\n"
        out = self.allowed(text, lambda m: m.replace("  ⟦0⟧", "⟦0⟧"))
        self.assertIn("\n  ```sh\n  make\n  ```", out)


class KnownLimitTest(unittest.TestCase):
    def test_same_kind_values_trading_places_pass(self):
        # a multiset of numbers cannot tell "A is 10, B is 20" from "A is 20, B is 10"; documented in README
        out, reason = rewrite("A는 10, B는 20입니다.", lambda m: "A는 20, B는 10입니다.")
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
