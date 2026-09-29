"""The conversation so far (context.py): what is read from the transcript, what is left out, and how it
reaches agy and the reviewing Claude."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from support import SAMPLE, clean_env, event, new_data_dir, read_jsonl, remove, run_hook  # noqa: E402

QUESTION = "web 서버를 늘리는 기준을 스레드 개수로 바꾸면 어떤가요?"


def entry(kind, content, **kw):
    msg = {"role": kind, "content": content}
    if kind == "assistant":
        msg["model"] = kw.pop("model", "claude-sonnet-5-5")
    return dict({"type": kind, "message": msg}, **kw)


def transcript(path, answer=""):
    rows = [
        entry("user", "처음 질문입니다. 대시보드를 봐 주세요."),
        entry("assistant", [{"type": "text", "text": "대시보드를 확인했습니다. `terraform plan` 결과는 변경 없음입니다."},
                            {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "cat secret.tfvars"}}]),
        entry("user", [{"type": "tool_result", "tool_use_id": "x", "content": "TOOL-OUTPUT-NEVER-SENT"}]),
        entry("user", "<system-reminder>REMINDER-NEVER-SENT</system-reminder>"),
        entry("user", "SUMMARY-NEVER-SENT", isCompactSummary=True),
        entry("user", "META-NEVER-SENT", isMeta=True),
        entry("assistant", [{"type": "text", "text": "SIDECHAIN-NEVER-SENT"}], isSidechain=True),
        entry("user", QUESTION + "\n<system-reminder>ALSO-NEVER-SENT</system-reminder>"),
        entry("assistant", [{"type": "text", "text": "<synthetic>"}], model="<synthetic>"),
    ]
    if answer:
        rows.append(entry("assistant", [{"type": "text", "text": answer}]))
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.data = new_data_dir()
        self.path = os.path.join(self.data, "t.jsonl")
        self.env = {k: os.environ.get(k) for k in ("AGY_READABLE_CONTEXT_CHARS", "AGY_READABLE_REVIEW_MODEL")}

    def tearDown(self):
        remove(self.data)

    def read(self, answer="", **env):
        import importlib
        from agy_readable import config, context
        old = dict(os.environ)
        os.environ.update(env)
        try:
            importlib.reload(config)
            return context.read(self.path, answer)
        finally:
            os.environ.clear()
            os.environ.update(old)
            importlib.reload(config)

    def test_only_the_conversation_text(self):
        transcript(self.path, answer=SAMPLE)
        ctx, model = self.read(SAMPLE)
        self.assertEqual(model, "claude-sonnet-5-5")
        self.assertIn("[사용자] 처음 질문입니다", ctx)
        self.assertIn("[사용자] " + QUESTION, ctx)
        self.assertIn("[Claude] 대시보드를 확인했습니다. [코드] 결과는", ctx)  # code masked, not numbered
        self.assertNotIn("⟦", ctx)
        for never in ("TOOL-OUTPUT", "secret.tfvars", "REMINDER", "SUMMARY", "META", "SIDECHAIN", "ALSO-NEVER",
                      "<synthetic>", SAMPLE[:40]):
            self.assertNotIn(never, ctx)
        self.assertTrue(ctx.index("처음 질문") < ctx.index(QUESTION))  # oldest first

    def test_newest_first_within_the_limit(self):
        transcript(self.path)
        ctx, _ = self.read(AGY_READABLE_CONTEXT_CHARS="80")
        self.assertIn(QUESTION, ctx)
        self.assertNotIn("처음 질문", ctx)

    def test_off(self):
        transcript(self.path)
        self.assertEqual(self.read(AGY_READABLE_CONTEXT_CHARS="0"), ("", "claude-sonnet-5-5"))

    def test_no_transcript(self):
        from agy_readable import context
        self.assertEqual(context.read(None), ("", None))
        self.assertEqual(context.read(os.path.join(self.data, "missing.jsonl")), ("", None))


class HookContextTest(unittest.TestCase):
    def setUp(self):
        self.data = new_data_dir()
        self.path = os.path.join(self.data, "t.jsonl")
        self.args = os.path.join(self.data, "claude-args.jsonl")
        self.prompts = os.path.join(self.data, "prompts.txt")
        self.claude_prompts = os.path.join(self.data, "claude-prompts.txt")
        self.env = clean_env(self.data, AGY_READABLE_DAEMON="0", FAKE_CLAUDE_ARGS=self.args,
                             FAKE_PROMPTS=self.prompts)
        transcript(self.path)

    def tearDown(self):
        remove(self.data)

    def hook(self, **env):
        return run_hook(dict(self.env, **env), event(SAMPLE, transcript_path=self.path))

    def prompts_sent(self):
        with open(self.prompts, encoding="utf-8") as f:
            return f.read().split("\n=====\n")

    def test_agy_and_claude_get_the_conversation(self):
        out = self.hook()
        self.assertTrue(out.startswith("[다듬음"))
        prompts = [p for p in self.prompts_sent() if p.strip()]
        self.assertGreaterEqual(len(prompts), 2)  # agy's rewrite and Claude's check
        for p in prompts:
            self.assertTrue(p.startswith("[앞선 대화"), p[:80])
            self.assertIn(QUESTION, p)
            self.assertNotIn("TOOL-OUTPUT", p)
        argv = read_jsonl(self.args)[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")  # the main conversation's model
        log = read_jsonl(os.path.join(self.data, "hook.log"))[-1]
        self.assertGreater(log["context"], 0)
        self.assertEqual(log["review_model"], "claude-sonnet-5-5")

    def test_a_set_review_model_wins(self):
        self.hook(AGY_READABLE_REVIEW_MODEL="haiku")
        argv = read_jsonl(self.args)[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")

    def test_context_off(self):
        self.hook(AGY_READABLE_CONTEXT_CHARS="0")
        for p in self.prompts_sent():
            self.assertNotIn("[앞선 대화", p)
        argv = read_jsonl(self.args)[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")


if __name__ == "__main__":
    unittest.main()
