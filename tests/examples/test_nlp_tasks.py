"""Executable-example tests for nlp-tasks/SKILL.md.

All five Workflow blocks run against the real spaCy / datasets / transformers /
seqeval / rouge-score / scikit-learn APIs. What is stubbed, and why (every real
checkpoint and dataset is a Hugging Face Hub download):

* ``spacy.load("en_core_web_sm")`` -> ``spacy.blank("en")`` (the trained pipeline
  is a separate download; the blank one tokenizes but has no tagger, so ``pos_``
  is empty).
* ``AutoTokenizer.from_pretrained`` -> offline word-level tokenizers with the
  checkpoint family's special-token template (BERT-style ``[CLS] .. [SEP]`` or
  BART-style ``<s> .. </s>``).
* ``AutoModelFor*.from_pretrained`` / ``transformers.pipeline`` -> tiny randomly
  initialised BERT / BART models wrapped in the *real* pipeline / Trainer /
  ``generate`` code paths. Outputs are meaningless; the plumbing is what's tested.
* ``datasets.load_dataset("imdb")`` -> a synthetic DatasetDict whose splits are
  stored **sorted by label** (the failure mode the subsetting fix addresses).
* Block 2: ``num_train_epochs=2`` -> ``max_steps=2`` (speed only).
"""
from __future__ import annotations

import inspect
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from examples import harness

SLUG = "nlp-tasks"
DEPS = ("spacy", "transformers", "torch", "tokenizers", "datasets", "seqeval", "rouge_score", "sklearn", "numpy")

POS = ["good", "great", "wonderful", "fine", "loved"]
NEG = ["bad", "awful", "poor", "boring", "hated"]
FILLER = ["the", "movie", "film", "plot", "acting", "was", "really", "quite", "and", "a", "story",
          "amazon", "new", "office", "opened", "in", "seattle", "on", "jan", "5th"]
VOCAB = POS + NEG + FILLER


def _tokenizer(style: str, model_max_length: int):
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors, trainers
    from transformers import PreTrainedTokenizerFast

    if style == "bert":
        specials = {"pad_token": "[PAD]", "unk_token": "[UNK]", "cls_token": "[CLS]", "sep_token": "[SEP]",
                    "mask_token": "[MASK]"}
        bos, eos = "[CLS]", "[SEP]"
    else:  # bart
        specials = {"pad_token": "<pad>", "unk_token": "<unk>", "bos_token": "<s>", "eos_token": "</s>",
                    "mask_token": "<mask>"}
        bos, eos = "<s>", "</s>"
    tok = Tokenizer(models.WordLevel(unk_token=specials["unk_token"]))
    tok.normalizer = normalizers.Lowercase()
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.train_from_iterator([" ".join(VOCAB)], trainers.WordLevelTrainer(special_tokens=list(specials.values())))
    tok.post_processor = processors.TemplateProcessing(
        single=f"{bos} $A {eos}", pair=f"{bos} $A {eos} $B:1 {eos}:1",
        special_tokens=[(bos, tok.token_to_id(bos)), (eos, tok.token_to_id(eos))])
    return PreTrainedTokenizerFast(tokenizer_object=tok, model_max_length=model_max_length, **specials)


def _fake_imdb(n_train=2100, n_test=1200, seed=0):
    from datasets import Dataset, DatasetDict

    rng = random.Random(seed)

    def split(n):
        rows = []
        for label in (0, 1):  # stored sorted by label
            for _ in range(n // 2):
                words = rng.choices(FILLER, k=rng.randint(3, 25)) + rng.choices(POS if label else NEG, k=2)
                rng.shuffle(words)
                rows.append({"text": " ".join(words), "label": label})
        return Dataset.from_list(rows)

    return DatasetDict(train=split(n_train), test=split(n_test))


def _tiny_bert(cls, tokenizer, **cfg_kwargs):
    from transformers import BertConfig

    cfg = BertConfig(vocab_size=len(tokenizer), hidden_size=16, num_hidden_layers=1, num_attention_heads=2,
                     intermediate_size=32, max_position_embeddings=512, pad_token_id=tokenizer.pad_token_id,
                     **cfg_kwargs)
    return cls(cfg)


def _tiny_bart(tokenizer):
    from transformers import BartConfig, BartForConditionalGeneration

    cfg = BartConfig(vocab_size=len(tokenizer), d_model=16, encoder_layers=1, decoder_layers=1,
                     encoder_attention_heads=2, decoder_attention_heads=2, encoder_ffn_dim=32, decoder_ffn_dim=32,
                     max_position_embeddings=1024, pad_token_id=tokenizer.pad_token_id,
                     bos_token_id=tokenizer.bos_token_id, eos_token_id=tokenizer.eos_token_id,
                     decoder_start_token_id=tokenizer.eos_token_id, forced_bos_token_id=None,
                     forced_eos_token_id=None)
    return BartForConditionalGeneration(cfg).eval()


CONLL_LABELS = ["O", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC", "B-MISC", "I-MISC"]


@harness.requires(*DEPS)
class TestNLPTasksExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import datasets
        import spacy
        import torch
        import transformers
        from transformers import BertForSequenceClassification, BertForTokenClassification
        from transformers import pipeline as real_pipeline

        torch.manual_seed(0)
        cls._tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.workdir = Path(cls._tmp.name)
        rng = random.Random(1)
        (cls.workdir / "article.txt").write_text(" ".join(rng.choices(FILLER, k=2500)), encoding="utf-8")

        cls.bert_tok = _tokenizer("bert", 512)
        cls.bart_tok = _tokenizer("bart", 1024)
        cls.imdb = _fake_imdb()
        cls.generate_input_lengths: list[int] = []

        def fake_tokenizer(name, *a, **k):
            return cls.bart_tok if "bart" in name else cls.bert_tok

        def fake_seq2seq(name, *a, **k):
            model = _tiny_bart(cls.bart_tok)
            real_generate = model.generate

            def recording_generate(*ga, **gk):
                cls.generate_input_lengths.append(gk["input_ids"].shape[-1])
                return real_generate(*ga, **gk)

            model.generate = recording_generate
            return model

        ner_model = _tiny_bert(BertForTokenClassification, cls.bert_tok, num_labels=len(CONLL_LABELS),
                               id2label=dict(enumerate(CONLL_LABELS)),
                               label2id={lab: i for i, lab in enumerate(CONLL_LABELS)})

        def fake_pipeline(task, model=None, **kwargs):
            return real_pipeline(task, model=ner_model, tokenizer=cls.bert_tok, **kwargs)

        patches = [
            mock.patch.object(spacy, "load", lambda name, *a, **k: spacy.blank("en")),
            mock.patch.object(transformers.AutoTokenizer, "from_pretrained", fake_tokenizer),
            mock.patch.object(transformers.AutoModelForSequenceClassification, "from_pretrained",
                              lambda name, num_labels=2, **k: _tiny_bert(BertForSequenceClassification,
                                                                        cls.bert_tok, num_labels=num_labels)),
            mock.patch.object(transformers.AutoModelForSeq2SeqLM, "from_pretrained", fake_seq2seq),
            # Patch the module object `from transformers import ...` will actually read: the
            # lazy `transformers` module can differ from the name bound by `import transformers`.
            mock.patch.object(sys.modules["transformers"], "pipeline", fake_pipeline),
            mock.patch.object(datasets, "load_dataset", lambda name, *a, **k: cls.imdb),
        ]
        for p in patches:
            p.start()
            cls.addClassCleanup(p.stop)

        cls.ns = harness.run_blocks(SLUG, workdir=cls.workdir, replace={
            2: {"num_train_epochs=2,": "max_steps=2,"},
        })

    def test_all_blocks_are_executable(self):
        blocks = harness.python_blocks(SLUG)
        self.assertEqual(len(blocks), 5)
        self.assertFalse(any(harness.is_illustrative(b) for b in blocks))

    def test_tokenization_block(self):
        ns = self.ns
        self.assertGreater(len(ns["doc"]), 5)
        self.assertIn("offset_mapping", ns["enc"])

    def test_ner_pipeline_returns_aggregated_entities(self):
        ents = self.ns["ner"]("Amazon's new office opened in Seattle on Jan 5th.")
        self.assertIsInstance(ents, list)
        for ent in ents:
            self.assertTrue({"word", "entity_group", "score"} <= set(ent))

    def test_classification_eval_subset_has_both_classes(self):
        # Regression: original code took ds["test"].select(range(500)) from a label-sorted split.
        ns = self.ns
        self.assertEqual(set(ns["test"]["label"]), {0, 1})
        self.assertEqual(set(ns["train"]["label"]), {0, 1})
        original_eval = self.imdb["test"].select(range(500))
        self.assertEqual(len(set(original_eval["label"])), 1, "fixture should reproduce the sorted-split defect")

    def test_classification_trained_and_reports_macro_f1(self):
        trainer = self.ns["trainer"]
        self.assertEqual(trainer.state.global_step, 2)
        metrics = trainer.evaluate()
        self.assertIn("eval_macro_f1", metrics)
        self.assertTrue(0.0 <= metrics["eval_macro_f1"] <= 1.0)

    def test_summarization_chunks_stay_under_model_limit(self):
        # summarize() tokenizes with truncation=True, max_length=1024, so post-truncation
        # lengths are always <= 1024 and cannot reveal an oversized chunk. Measure what
        # summarize() is *given*, before truncation, and check the chunks cover the article.
        ns = self.ns
        tok = ns["sum_tokenizer"]
        limit = tok.model_max_length
        chunk_tokens = inspect.signature(ns["summarize_long"]).parameters["chunk_tokens"].default
        self.assertGreaterEqual(len(self.generate_input_lengths), 3)  # >=2 chunks + reduce pass
        self.assertLessEqual(max(self.generate_input_lengths), chunk_tokens + 2)  # + <s> and </s>

        seen: list[str] = []
        real_summarize = ns["summarize"]

        def recording_summarize(text, *a, **k):
            seen.append(text)
            return real_summarize(text, *a, **k)

        ns["summarize"] = recording_summarize  # summarize_long resolves it from this namespace
        try:
            ns["summarize_long"](ns["long_article_text"])
        finally:
            ns["summarize"] = real_summarize

        article_ids = tok(ns["long_article_text"], add_special_tokens=False)["input_ids"]
        self.assertGreater(len(article_ids), limit, "fixture must need chunking")
        full_lengths = [len(tok(t)["input_ids"]) for t in seen]  # untruncated, with special tokens
        self.assertTrue(all(n <= limit for n in full_lengths), full_lengths)
        n_chunks = math.ceil(len(article_ids) / chunk_tokens)
        map_ids = [i for t in seen[:n_chunks] for i in tok(t, add_special_tokens=False)["input_ids"]]
        self.assertEqual(map_ids, article_ids, "map pass dropped or reordered article tokens")

        self.assertIsInstance(ns["summarize_long"](ns["long_article_text"][:200]), str)
        self.assertEqual(ns["summarize_long"](""), "")

    def test_metrics_block_values(self):
        ns = self.ns
        self.assertAlmostEqual(ns["entity_f1"](ns["ner_true"], ns["ner_pred"]), 0.8)
        self.assertAlmostEqual(ns["f1_score"](ns["y_true"], ns["y_pred"], average="macro"), (8 / 9 + 2 / 3) / 2)
        rouge = ns["scorer"].score(ns["ref_summary"], ns["pred_summary"])
        self.assertTrue(0.0 < rouge["rouge1"].fmeasure < 1.0)


@harness.requires(*DEPS)
class TestNLPTasksRegressions(unittest.TestCase):
    """Library behaviour the original skill code tripped over (and the Gotchas now cite)."""

    def test_training_arguments_rejects_evaluation_strategy(self):
        from transformers import TrainingArguments

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(TypeError):
            TrainingArguments(output_dir=tmp, evaluation_strategy="epoch")

    def test_summarization_pipeline_task_removed(self):
        import transformers
        from transformers import pipeline

        if int(transformers.__version__.split(".")[0]) < 5:
            self.skipTest("summarization pipeline still exists before Transformers 5")
        with self.assertRaises(KeyError):
            pipeline("summarization")  # task lookup fails before any model download

    def test_trainer_without_tokenizer_cannot_batch_ragged_inputs(self):
        # Original step 3 built Trainer(...) with no tokenizer/collator.
        from transformers import BertForSequenceClassification, Trainer, TrainingArguments

        tok = _tokenizer("bert", 512)
        ds = _fake_imdb(n_train=64, n_test=2)["train"].shuffle(seed=0)
        ds = ds.map(lambda b: tok(b["text"], truncation=True, max_length=256), batched=True)
        model = _tiny_bert(BertForSequenceClassification, tok, num_labels=2)
        with tempfile.TemporaryDirectory() as tmp:
            args = TrainingArguments(output_dir=tmp, per_device_train_batch_size=16, max_steps=1)
            with self.assertRaises((ValueError, RuntimeError)):
                Trainer(model=model, args=args, train_dataset=ds).train()


if __name__ == "__main__":
    unittest.main()
