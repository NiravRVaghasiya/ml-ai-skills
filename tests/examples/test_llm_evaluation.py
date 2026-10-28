"""Executable-example tests for llm-evaluation/SKILL.md.

All six Workflow blocks run. What is stubbed, and why:

* ``anthropic.Anthropic`` is replaced by a fake client (no API key, no network,
  no cost). Its ``messages.create`` first binds the call's kwargs against the
  *real* installed ``anthropic`` SDK signature — so a parameter the SDK rejects
  (e.g. ``temperature`` on SDK 2.x) fails here exactly as it would in production —
  and returns real ``anthropic.types.Message`` objects whose content starts with
  an empty ``thinking`` block, as current Claude models with thinking on return.
* ``transformers.pipeline`` is wrapped so the NLI block builds a *real*
  text-classification pipeline around a tiny, randomly initialised BERT with a
  word-level tokenizer (the real cross-encoder checkpoint is a large Hub
  download). Its classifier bias is set so the verdict is deterministic; this
  tests the plumbing (pair input, label lookup, aggregation), not NLI quality.
  The fake model uses lowercase labels: label spelling differs across NLI
  checkpoints, and the original code hard-coded ``"ENTAILMENT"``.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import math
import sys
import unittest
from unittest import mock

from examples import harness

SLUG = "llm-evaluation"
DEPS = ("anthropic", "rouge_score", "nltk", "transformers", "torch", "tokenizers")
GOOD_ANSWER = "Annual plans may be refunded within 30 days of purchase."


def _message(text: str | None, stop_reason: str = "end_turn"):
    import anthropic

    content = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    return anthropic.types.Message.model_validate({
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    })


class FakeAnthropic:
    """Records calls; answers SUT prompts with ``answer`` and judge prompts with ``verdict``."""

    def __init__(self, *args, answer=GOOD_ANSWER, verdict='{"score": 5, "reason": "correct"}',
                 answer_stop="end_turn", verdict_stop="end_turn", **kwargs):
        self.calls: list[dict] = []
        self.answer, self.verdict = answer, verdict
        self.answer_stop, self.verdict_stop = answer_stop, verdict_stop
        self.messages = self

    def create(self, **kwargs):
        from anthropic.resources.messages import Messages

        inspect.signature(Messages.create).bind(None, **kwargs)  # TypeError, like the real SDK
        self.calls.append(kwargs)
        if "You are grading" in kwargs["messages"][0]["content"]:
            return _message(self.verdict, self.verdict_stop)
        return _message(self.answer, self.answer_stop)


def _tiny_nli(label_bias):
    """A real HF text-classification pipeline over a tiny random BERT (labels lowercase)."""
    import torch
    from tokenizers import Tokenizer, models, pre_tokenizers, processors, trainers
    from transformers import BertConfig, BertForSequenceClassification, PreTrainedTokenizerFast
    from transformers import pipeline as real_pipeline

    tok = Tokenizer(models.WordLevel(unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.train_from_iterator([GOOD_ANSWER, "annual plans refunded days purchase"],
                            trainers.WordLevelTrainer(special_tokens=["[PAD]", "[UNK]", "[CLS]", "[SEP]"]))
    cls_id, sep_id = tok.token_to_id("[CLS]"), tok.token_to_id("[SEP]")
    tok.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]", pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", cls_id), ("[SEP]", sep_id)])
    hf_tok = PreTrainedTokenizerFast(tokenizer_object=tok, unk_token="[UNK]", pad_token="[PAD]",
                                     cls_token="[CLS]", sep_token="[SEP]", model_max_length=64)
    labels = {0: "contradiction", 1: "entailment", 2: "neutral"}
    cfg = BertConfig(vocab_size=tok.get_vocab_size(), hidden_size=16, num_hidden_layers=1,
                     num_attention_heads=2, intermediate_size=32, max_position_embeddings=64,
                     num_labels=3, id2label=labels, label2id={v: k for k, v in labels.items()})
    model = BertForSequenceClassification(cfg).eval()
    with torch.no_grad():
        model.classifier.weight.zero_()
        model.classifier.bias.copy_(torch.tensor(label_bias, dtype=torch.float))
    return real_pipeline("text-classification", model=model, tokenizer=hf_tok)


def _run(client: FakeAnthropic, label_bias=(0.0, 6.0, 0.0), indices=None):
    import anthropic

    nli = _tiny_nli(label_bias)
    # Patch the module object `from transformers import pipeline` will actually read: the
    # lazy `transformers` module can differ from the name bound by `import transformers`.
    with mock.patch.object(anthropic, "Anthropic", lambda *a, **k: client), \
         mock.patch.object(sys.modules["transformers"], "pipeline", lambda task, model=None, **k: nli):
        return harness.run_blocks(SLUG, indices)


@harness.requires(*DEPS)
class TestLLMEvaluationExamples(unittest.TestCase):
    def test_full_workflow_passes_gate_on_good_answers(self):
        client = FakeAnthropic()
        ns = _run(client)
        self.assertEqual(ns["incomplete"], [])
        self.assertEqual(ns["judge_failures"], 0)
        self.assertEqual(ns["avg_score"], 5)
        self.assertEqual(ns["hallucination_rate"], 0.0)
        self.assertEqual(set(ns["results"]), {"rouge1", "rouge2", "rougeL"})
        self.assertGreater(ns["results"]["rouge1"], 0.0)
        self.assertEqual(ns["report"]["n_cases"], len(ns["eval_set"]))
        # Regression: content[0] is a thinking block on current models; text must still be read.
        self.assertEqual(ns["outputs"][0]["output"], GOOD_ANSWER)
        # Regression: no sampling parameters reach the SDK (SDK 2.x rejects them).
        for call in client.calls:
            self.assertFalse({"temperature", "top_p", "top_k"} & set(call))
        judge_call = client.calls[-1]
        self.assertEqual(judge_call["output_config"]["format"]["type"], "json_schema")
        # Effort changes output and its default differs by model: it must be pinned and reported.
        for call in client.calls:
            self.assertIn(call["output_config"]["effort"], {"low", "medium", "high", "xhigh", "max"})
        self.assertEqual(ns["report"]["sut_effort"], client.calls[0]["output_config"]["effort"])
        self.assertEqual(ns["report"]["judge_effort"], judge_call["output_config"]["effort"])

    def test_gate_fails_on_hallucination(self):
        with self.assertRaises(SystemExit) as cm:
            _run(FakeAnthropic(), label_bias=(6.0, 0.0, 0.0))  # NLI says "contradiction"
        self.assertIn("hallucination_rate", str(cm.exception))

    def test_malformed_or_out_of_range_judge_reply_is_counted_not_crashing(self):
        for verdict in ("Sure! Here is my grade: 5", '{"score": 9, "reason": "x"}',
                        '{"score": true, "reason": "x"}', '[5]'):
            with self.subTest(verdict=verdict):
                ns = _run(FakeAnthropic(verdict=verdict), indices=[0, 1, 2, 3])
                self.assertEqual(ns["judge_failures"], 1)
                self.assertIsNone(ns["judged"][0]["score"])
                self.assertTrue(math.isnan(ns["avg_score"]))

    def test_refusal_is_reported_and_fails_gate(self):
        client = FakeAnthropic(answer=None, answer_stop="refusal")
        with self.assertRaises(SystemExit) as cm:
            _run(client)
        self.assertIn("1 incomplete generations", str(cm.exception))

    def test_refused_rows_are_not_judged_or_scored(self):
        # Review finding: step 4 sent refusals/blank answers to the (paid) judge and averaged
        # the resulting score in; ROUGE likewise averaged in a 0 for the refusal.
        client = FakeAnthropic(answer=None, answer_stop="refusal")
        ns = _run(client, indices=[0, 1, 2, 3, 4])
        self.assertEqual(ns["incomplete"], ["refund-001"])
        self.assertEqual(ns["answered"], [])
        self.assertEqual(ns["judged"], [])
        self.assertFalse(any("You are grading" in c["messages"][0]["content"] for c in client.calls))
        self.assertEqual(ns["results"], {})
        self.assertTrue(math.isnan(ns["avg_score"]))
        self.assertTrue(math.isnan(ns["hallucination_rate"]))

    def test_empty_end_turn_answer_counts_as_incomplete(self):
        ns = _run(FakeAnthropic(answer="   "), indices=[0, 1])
        self.assertEqual(ns["incomplete"], ["refund-001"])
        self.assertEqual(ns["answered"], [])

    def test_report_is_strict_json_when_nothing_was_scored(self):
        # Review finding: NaN metrics were written as a bare NaN token, which strict JSON
        # parsers (JSON.parse, jq) reject; json.loads accepts it unless parse_constant objects.
        ns = _run(FakeAnthropic(answer=None, answer_stop="refusal"), indices=[0, 1, 2, 3, 4])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            harness.run_blocks(SLUG, [5], namespace=ns)
        self.assertIn("avg_judge_score regressed", str(cm.exception))  # NaN still fails the gate

        def reject(token):
            raise ValueError(f"non-standard JSON constant {token}")

        report = json.loads(out.getvalue(), parse_constant=reject)
        self.assertIsNone(report["avg_judge_score"])
        self.assertIsNone(report["hallucination_rate"])
        self.assertIsNone(report["rouge1"])
        self.assertEqual(report["n_scored"], 0)

    def test_long_context_is_truncated_not_crashing(self):
        # Review finding: no case exercised truncation=True; the fake NLI model accepts 64
        # positions, so a ~200-token premise crashes unless the pipeline truncates.
        ns = _run(FakeAnthropic(), indices=[0, 1, 2, 3, 4])
        long_context = " ".join([GOOD_ANSWER] * 20)
        n_tokens = len(ns["nli"].tokenizer(long_context, GOOD_ANSWER)["input_ids"])
        self.assertGreater(n_tokens, ns["nli"].tokenizer.model_max_length)
        with self.assertRaises(RuntimeError):  # what is_grounded would hit without truncation
            ns["nli"]({"text": long_context, "text_pair": GOOD_ANSWER}, top_k=None)
        self.assertIsInstance(ns["is_grounded"](long_context, GOOD_ANSWER), bool)

    def test_eval_set_written_as_jsonl(self):
        ns = _run(FakeAnthropic(), indices=[0])
        self.assertEqual(ns["eval_set"][0]["id"], "refund-001")

    def test_report_flags_judge_grading_its_own_model(self):
        # Audit finding: the example judge is the SUT's own model, the self-preference setup
        # the Gotchas warn about; the report must say so, so a reader does not compare models
        # with it. Swapping the judge clears the flag.
        ns = _run(FakeAnthropic(), indices=[0, 1, 2, 3, 4])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness.run_blocks(SLUG, [5], namespace=ns)
        self.assertEqual(ns["JUDGE_MODEL"], ns["SUT_MODEL"])
        self.assertIs(json.loads(out.getvalue())["judge_same_as_sut"], True)
        ns["JUDGE_MODEL"] = "some-other-family-judge"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness.run_blocks(SLUG, [5], namespace=ns)
        self.assertIs(json.loads(out.getvalue())["judge_same_as_sut"], False)


class TestLLMEvaluationMetadata(unittest.TestCase):
    def test_risk_level_reflects_external_api_calls(self):
        # Audit finding: the Workflow sends eval prompts, contexts and answers to a hosted
        # API; docs/QUALITY-GATES.md requires risk_level medium or higher for that.
        text = (harness.REPO_ROOT / SLUG / "SKILL.md").read_text(encoding="utf-8")
        front = text.split("---", 2)[1]
        self.assertIn("messages.create(", text)
        risk = next(line.split(":", 1)[1].strip() for line in front.splitlines() if line.startswith("risk_level:"))
        self.assertIn(risk, {"medium", "high", "critical"})

    def test_prompt_engineering_sampling_advice_matches_this_skill(self):
        # Audit finding: prompt-engineering told readers to set a low temperature with only a
        # soft caveat, while this skill documents that the current Claude API and SDK 2.x
        # reject sampling parameters. The sibling card must scope the advice and say so.
        text = (harness.REPO_ROOT / "prompt-engineering" / "SKILL.md").read_text(encoding="utf-8")
        flat = " ".join(text.split())
        self.assertNotIn("at non-zero temperature, take a majority vote", flat)
        self.assertIn("Where they are accepted, the usual convention is low temperature", flat)
        self.assertIn("2026-10-21 rejects `temperature`/`top_p`/`top_k`", flat)
        self.assertIn("[[llm-evaluation]]", flat)


@harness.requires(*DEPS)
class TestLLMEvaluationRegressions(unittest.TestCase):
    """Behaviour the original skill code tripped over (and the Gotchas now cite)."""

    def test_sdk_rejects_temperature(self):
        # Original steps 2 and 4 passed temperature=0.
        import anthropic

        client = anthropic.Anthropic(api_key="test-key", base_url="http://127.0.0.1:9", max_retries=0)
        with self.assertRaises(TypeError):
            client.messages.create(model="claude-opus-5-5", max_tokens=8, temperature=0,
                                   messages=[{"role": "user", "content": "hi"}])

    def test_first_block_can_be_thinking(self):
        # Original code read response.content[0].text.
        msg = _message("answer")
        self.assertEqual(msg.content[0].type, "thinking")
        with self.assertRaises(AttributeError):
            msg.content[0].text  # noqa: B018

    def test_hardcoded_uppercase_label_breaks_on_lowercase_checkpoint(self):
        nli = _tiny_nli((0.0, 6.0, 0.0))
        result = nli({"text": GOOD_ANSWER, "text_pair": GOOD_ANSWER}, top_k=None)
        self.assertEqual({r["label"] for r in result}, {"contradiction", "entailment", "neutral"})
        with self.assertRaises(StopIteration):
            next(r["score"] for r in result if r["label"] == "ENTAILMENT")

    def test_rouge_default_tokenizer_drops_non_ascii(self):
        from rouge_score import rouge_scorer

        score = rouge_scorer.RougeScorer(["rouge1"]).score("東京は日本の首都です", "東京は日本の首都です")
        self.assertEqual(score["rouge1"].fmeasure, 0.0)


if __name__ == "__main__":
    unittest.main()
