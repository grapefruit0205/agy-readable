"""How the second pass's answer is put into the rewrite (review.apply), and what a deletion leaves behind."""
import unittest

from support import SAMPLE  # noqa: F401  (puts the repo on sys.path)

from agy_readable import review


def fix(draft, answer):
    lines = draft.split("\n")
    return review.apply(lines, review.units(lines), answer)


class SuspectTest(unittest.TestCase):
    ORIGINAL = ("서버는 최소 2대입니다. 최대 4대는 비용 상한입니다. 최대 4대의 이유는 일반적인 설명(추정)입니다.\n\n"
                "헬스 체크는 가벼운 파일로 합니다.")

    def flagged(self, draft):
        lines = draft.split("\n")
        us = review.units(lines)
        return review.unmarked(self.ORIGINAL, [lines[i][s:e] for i, s, e in us])

    def test_summary_without_the_mark(self):
        found = self.flagged("- 비용 상한 때문에 **최대 4대**입니다.\n\n서버는 최소 2대입니다.")
        self.assertEqual(list(found), [1])
        self.assertEqual(found[1][0], "추정")

    def test_copy_mark_nearby_and_question_are_left_alone(self):
        self.assertEqual(self.flagged("**최대 4대는 비용 상한입니다.** 최대 4대의 이유는 일반적인 설명(추정)입니다."), {})
        self.assertEqual(self.flagged("- 비용 상한이라서 4대입니다(추정)."), {})
        self.assertEqual(self.flagged("**Q. 비용 상한은 왜 4대인가요?**"), {})

    def test_missing_number_cites_the_sentence_that_lost_it(self):
        masked = "## 1. 개요\n\n일꾼 1명이 요청 1개를 처리합니다. 남은 1건은 DB 쪽입니다."
        draft = "## 개요\n\n일꾼 1명이 요청 1개를 처리합니다. 남은 건은 DB 쪽입니다."
        self.assertEqual(review.missing(masked, draft),
                         ["- 빠진 숫자 1. 원문 문장: 남은 1건은 DB 쪽입니다. / 일꾼 1명이 요청 1개를 처리합니다."])


class ApplyTest(unittest.TestCase):
    def test_edit_keeps_the_list_mark(self):
        out, done, bad = fix("- 첫 문장입니다. 둘째 문장입니다.", "[1] 고친 문장입니다.")
        self.assertEqual(out, "- 고친 문장입니다. 둘째 문장입니다.")
        self.assertEqual((done, bad), ({"고침": 1}, []))

    def test_deleted_summary_leaves_no_rule_or_blank_run(self):
        draft = "- 요약 하나입니다.\n- 요약 둘입니다.\n\n---\n\n## 1. 본문\n\n본문입니다."
        out, done, _ = fix(draft, "[1] (삭제)\n[2] (삭제)")
        self.assertEqual(out, "## 1. 본문\n\n본문입니다.")
        self.assertEqual(done, {"삭제": 2})

    def test_deleted_section_leaves_one_rule_between_two(self):
        draft = "## 가\n\n가입니다.\n\n---\n\n지울 문장입니다.\n\n---\n\n## 나\n\n나입니다."
        out, _, _ = fix(draft, "[3] (삭제)")
        self.assertEqual(out, "## 가\n\n가입니다.\n\n---\n\n## 나\n\n나입니다.")

    def test_rules_and_blank_lines_stay_without_a_deletion(self):
        draft = "## 가\n\n\n\n가입니다.\n\n---\n\n나입니다."
        out, _, _ = fix(draft, "[2] 가였습니다.")
        self.assertEqual(out, "## 가\n\n\n\n가였습니다.\n\n---\n\n나입니다.")

    def test_sentence_added_after_a_table_goes_below_it(self):
        draft = "| 가 | 나 |\n|---|---|\n| 1 | 2 |\n\n끝입니다."
        out, done, _ = fix(draft, "[2+] 넣은 문장입니다.")
        self.assertEqual(out, "| 가 | 나 |\n|---|---|\n| 1 | 2 |\n\n넣은 문장입니다.\n\n끝입니다.")
        self.assertEqual(done, {"넣음": 1})

    def test_code_blocks_already_there_are_not_added_again(self):
        draft = "설명입니다.\n\n⟦0⟧\n\n끝입니다."
        out, done, _ = fix(draft, "[1+] ⟦0⟧")
        self.assertEqual((out, done), (draft, {}))

    def test_a_missing_code_block_goes_on_its_own_line(self):
        out, done, _ = fix("설명입니다. 둘째입니다.\n\n끝입니다.", "[1+] ⟦0⟧")
        self.assertEqual(out, "설명입니다. 둘째입니다.\n\n⟦0⟧\n\n끝입니다.")
        self.assertEqual(done, {"넣음": 1})

    def test_the_prompt_shows_code_blocks_where_they_stand(self):
        masked = "명령은 이렇습니다.\n\n⟦0⟧\n\n새 세션을 여세요."
        draft = "명령은 이렇습니다.\n\n⟦0⟧\n\n**새 세션**을 반드시 여세요. 끝."
        prompt, *_ = review.build(masked, masked, draft)
        rewritten = prompt.split("\n[다시 쓴 글]\n", 1)[1].split("\n\n[의심 목록]\n", 1)[0]
        self.assertEqual(rewritten, "[1] 명령은 이렇습니다.\n⟦0⟧\n[2] **새 세션**을 반드시 여세요.\n[3] 끝.")

    def test_lines_it_cannot_read(self):
        _, done, bad = fix("한 문장입니다.", "설명입니다\n[9] 없는 번호\n없음")
        self.assertEqual((done, bad), ({}, ["설명입니다", "[9] 없는 번호"]))


if __name__ == "__main__":
    unittest.main()
