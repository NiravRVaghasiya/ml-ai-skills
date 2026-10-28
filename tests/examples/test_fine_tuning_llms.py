"""Executable-example tests for fine-tuning-llms/SKILL.md.

All five Workflow blocks run against the real ``datasets`` / ``transformers`` /
``peft`` / ``trl`` APIs. What is stubbed, and why:

* ``AutoTokenizer.from_pretrained`` / ``AutoModelForCausalLM.from_pretrained``
  are patched to return a tiny, randomly initialised Llama and a word-level
  tokenizer built offline — the real checkpoint is a gated multi-GB download and
  4-bit loading needs CUDA + bitsandbytes. The fake ``from_pretrained`` returns a
  *fresh copy of the same pristine weights* on every call, as a real reload would,
  and records its kwargs so the test can check how the merge base is loaded.
* Block 3 swaps ``bf16=True`` (needs a bf16-capable GPU) for ``bf16=False`` and
  ``num_train_epochs=3`` for ``max_steps=2`` (speed only).

Regression tests pin the defects found in the original skill: the ``input``
field was silently dropped, ``SFTConfig(max_seq_length=...)`` raises on current
TRL, and merging by wrapping the already-wrapped ``PeftModel`` again fails.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from examples import harness

SLUG = "fine-tuning-llms"
DEPS = ("torch", "transformers", "peft", "trl", "datasets", "tokenizers")

ROWS = [
    {"instruction": f"Add {i} and {i + 1}", "input": f"numbers {i} {i + 1}" if i % 2 else "",
     "output": f"The sum is {2 * i + 1}"}
    for i in range(20)
]


def _make_tokenizer():
    from tokenizers import Tokenizer, models, pre_tokenizers, trainers
    from transformers import PreTrainedTokenizerFast

    corpus = ["### Instruction : Input : Response :"] + [" ".join(r.values()) for r in ROWS]
    tok = Tokenizer(models.WordLevel(unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.train_from_iterator(corpus, trainers.WordLevelTrainer(special_tokens=["<unk>", "<s>", "</s>"]))
    # No pad token on purpose: the skill's `pad_token = pad_token or eos_token` must handle it.
    return PreTrainedTokenizerFast(tokenizer_object=tok, unk_token="<unk>", bos_token="<s>", eos_token="</s>")


def _make_llama(tokenizer):
    from transformers import LlamaConfig, LlamaForCausalLM

    cfg = LlamaConfig(
        vocab_size=len(tokenizer), hidden_size=32, intermediate_size=64, num_hidden_layers=2,
        num_attention_heads=2, num_key_value_heads=2, max_position_embeddings=128,
        bos_token_id=tokenizer.bos_token_id, eos_token_id=tokenizer.eos_token_id,
    )
    return LlamaForCausalLM(cfg)


@harness.requires(*DEPS)
class TestFineTuningLLMsExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import torch
        import transformers

        torch.manual_seed(0)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.workdir = Path(cls._tmp.name)
        with open(cls.workdir / "train.jsonl", "w", encoding="utf-8") as fh:
            for row in ROWS:
                fh.write(json.dumps(row) + "\n")

        cls.tokenizer = _make_tokenizer()
        cls.pristine = _make_llama(cls.tokenizer)
        cls.load_calls: list[dict] = []

        def fake_model_from_pretrained(*args, **kwargs):
            cls.load_calls.append(kwargs)
            return copy.deepcopy(cls.pristine)

        patches = [
            mock.patch.object(transformers.AutoTokenizer, "from_pretrained", lambda *a, **k: cls.tokenizer),
            mock.patch.object(transformers.AutoModelForCausalLM, "from_pretrained", fake_model_from_pretrained),
        ]
        for p in patches:
            p.start()
            cls.addClassCleanup(p.stop)
        cls.addClassCleanup(cls._tmp.cleanup)

        ns = harness.run_blocks(SLUG, [0, 1, 2, 3], workdir=cls.workdir, replace={
            3: {"bf16=True,": "bf16=False,", "num_train_epochs=3,": "max_steps=2,"},
        })
        # Two optimizer steps at lr=2e-4 barely move lora_B away from its zero init, so a
        # broken merge would still match the base model. Give the adapter a visible,
        # deterministic delta before block 4 saves and merges it.
        with torch.no_grad():
            for name, param in ns["model"].named_parameters():
                if "lora_B" in name:
                    param.copy_(torch.randn_like(param) * 0.5)
        # run_blocks copies `namespace`, so `ns` keeps the trained model/trainer that block 4
        # deletes from its own namespace before reloading the 16-bit base.
        cls.train_ns = ns
        cls.ns = harness.run_blocks(SLUG, [4], namespace=ns, workdir=cls.workdir)

    def test_all_blocks_are_executable(self):
        blocks = harness.python_blocks(SLUG)
        self.assertEqual(len(blocks), 5)
        self.assertFalse(any(harness.is_illustrative(b) for b in blocks))

    def test_dataset_keeps_input_field_and_holds_out_eval_split(self):
        # Regression: the original to_text() ignored example["input"] entirely.
        ns = self.ns
        both = list(ns["train_ds"]) + list(ns["eval_ds"])
        self.assertEqual(len(both), len(ROWS))
        self.assertEqual(set(ns["train_ds"].column_names), {"prompt", "completion"})
        self.assertGreater(len(ns["eval_ds"]), 0)
        for rec in both:
            src = next(r for r in ROWS if rec["prompt"].startswith(f"### Instruction:\n{r['instruction']}\n"))
            self.assertEqual(rec["completion"], src["output"])
            self.assertTrue(rec["prompt"].endswith("### Response:\n"))
            if src["input"]:
                self.assertIn(f"### Input:\n{src['input']}\n", rec["prompt"])
            else:
                self.assertNotIn("### Input:", rec["prompt"])

    def test_trainer_trained_with_completion_only_loss_and_eval_split(self):
        trainer = self.train_ns["trainer"]
        self.assertEqual(trainer.state.global_step, 2)
        self.assertIsNotNone(trainer.eval_dataset)
        # prompt/completion columns -> TRL masks prompt tokens out of the loss (-100 labels)
        # and appends EOS to the completion, so the model learns to stop.
        sample = trainer.train_dataset[0]
        ids, labels = sample["input_ids"], sample["labels"]
        self.assertEqual(ids[-1], self.tokenizer.eos_token_id)
        self.assertEqual(labels[-1], self.tokenizer.eos_token_id)
        self.assertEqual(labels[0], -100)
        n_completion = sum(1 for lab in labels if lab != -100)
        self.assertGreater(n_completion, 0)
        self.assertLess(n_completion, len(ids))

    def test_tokenizer_got_a_pad_token(self):
        self.assertIsNotNone(self.ns["tokenizer"].pad_token)

    def test_merge_reloads_unquantized_base_and_matches_adapter(self):
        import torch
        from peft import PeftModel

        ns = self.ns
        merged, peft_model = ns["merged"], self.train_ns["model"]
        self.assertNotIsInstance(merged, PeftModel)
        self.assertFalse(any("lora" in n for n, _ in merged.named_modules()))
        # step 2 loads with a 4-bit quantization_config; the merge base must not.
        self.assertIn("quantization_config", self.load_calls[0])
        self.assertNotIn("quantization_config", self.load_calls[-1])

        ids = self.tokenizer("### Instruction:\nAdd 3 and 4\n\n### Response:\n", return_tensors="pt")["input_ids"]
        merged.eval(), peft_model.eval(), self.pristine.eval()
        with torch.no_grad():
            got = merged(input_ids=ids).logits
            want = peft_model(input_ids=ids).logits
            base = self.pristine(input_ids=ids).logits
        self.assertTrue(torch.allclose(got, want, atol=1e-4), "merged weights do not reproduce the adapter")
        self.assertFalse(torch.allclose(got, base, atol=1e-4), "merge produced the untouched base model")

    def test_merge_step_releases_training_objects_before_reload(self):
        # Review finding: the 16-bit reload ran while the 4-bit model, trainer and optimizer
        # state were still referenced, which can OOM a single training GPU.
        self.assertNotIn("trainer", self.ns)
        self.assertNotIn("model", self.ns)
        code = harness.python_blocks(SLUG)[4]
        self.assertLess(code.index("del trainer, model"), code.index("base = AutoModelForCausalLM.from_pretrained"))

    def test_artifacts_written(self):
        adapter = self.workdir / "lora-out" / "adapter"
        merged = self.workdir / "lora-out" / "merged-model"
        self.assertTrue((adapter / "adapter_config.json").is_file())
        self.assertTrue((merged / "config.json").is_file())
        self.assertTrue((merged / "tokenizer_config.json").is_file())


@harness.requires(*DEPS)
class TestFineTuningLLMsRegressions(unittest.TestCase):
    """Library behaviour the original skill code tripped over (and the Gotchas now cite)."""

    def test_sft_config_rejects_max_seq_length(self):
        from trl import SFTConfig

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(TypeError):
            SFTConfig(output_dir=tmp, max_seq_length=1024)

    def test_wrapping_a_peft_model_again_fails(self):
        # The original step 5 did PeftModel.from_pretrained(<already-wrapped model>, adapter_dir).
        from peft import LoraConfig, PeftModel, get_peft_model

        tok = _make_tokenizer()
        model = get_peft_model(_make_llama(tok), LoraConfig(r=2, target_modules=["q_proj"], task_type="CAUSAL_LM"))
        with tempfile.TemporaryDirectory() as tmp:
            model.save_pretrained(tmp)
            # peft 0.21.2: from_pretrained silently nests the wrappers; the merge then raises.
            with self.assertRaises(AttributeError):
                PeftModel.from_pretrained(model, tmp).merge_and_unload()


if __name__ == "__main__":
    unittest.main()
