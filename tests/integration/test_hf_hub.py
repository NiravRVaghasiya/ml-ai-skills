"""Opt-in: the pretrained-model and Hub-dataset facts that the offline example tests fake.

Gate: ``ML_AI_SKILLS_INTEGRATION_HF=1`` (downloads from the Hugging Face Hub, ~1.5 GB on the
first run: cross-encoder/nli-deberta-v3-base, dslim/bert-base-NER, the facebook/bart-large-cnn
tokenizer and config, and the IMDB dataset; set ``HF_HUB_OFFLINE=1`` to use a warm cache).
``ML_AI_SKILLS_INTEGRATION_SPACY=1`` separately checks ``en_core_web_sm`` (installed with
``python -m spacy download en_core_web_sm``; it is not on the Hub).

Each test pins one assumption that tests/examples bakes into a fake:

* llm-evaluation step 5 runs verbatim against the real NLI checkpoint: the entailment label
  lookup finds a label, an entailed answer is grounded and a contradicted one is not
  (tests/examples/test_llm_evaluation.py uses a tiny model with lowercase labels).
* nlp-tasks step 2 runs verbatim against ``dslim/bert-base-NER``: its ``id2label`` is the
  CoNLL-2003 IOB2 set the Gotcha names (PER/ORG/LOC/MISC), and the aggregated
  ``entity_group`` values come from that set (the offline test assumes ``CONLL_LABELS``).
* nlp-tasks step 4 with the real BART tokenizer (the 1.6 GB model is replaced by a stub whose
  ``generate`` records its input length): the 1024-token limit the Gotcha quotes, and that
  every chunk ``summarize_long`` makes still fits in 1024 tokens with special tokens, also for
  text that tokenizes into many pieces (code, URLs, non-Latin script).
* The IMDB splits on the Hub are stored sorted by label, which is why step 3 shuffles before
  subsetting (the offline fixture reproduces that order; this checks it is real).

Not covered: model quality beyond the two hand-picked NLI pairs, GPU inference, the real
summarization output, fine-tuning on IMDB.
"""
from __future__ import annotations

import os
import random
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from examples import harness
from integration import gate

CONLL_LABELS = {"O", "B-PER", "I-PER", "B-ORG", "I-ORG", "B-LOC", "I-LOC", "B-MISC", "I-MISC"}
CONTEXT = "The Eiffel Tower is a wrought-iron lattice tower in Paris, France, completed in 1889."


def hub_reachable() -> str | None:
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        return None   # the cache must already hold the files; a miss fails inside the test
    try:
        socket.getaddrinfo("huggingface.co", 443)
    except OSError as exc:
        return f"huggingface.co does not resolve ({exc}); fix DNS or warm the cache and set HF_HUB_OFFLINE=1"
    return None


@gate.opt_in("HF", what="pretrained checkpoints and datasets from the Hugging Face Hub",
             modules=("transformers", "torch", "datasets", "huggingface_hub", "tokenizers"),
             precondition=hub_reachable)
class TestPretrainedHubAssumptions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.workdir = Path(self._tmp.name)

    def test_nli_checkpoint_label_lookup_and_grounding(self):
        idx = gate.block_index("llm-evaluation", "cross-encoder/nli-deberta-v3-base")
        answered = [{"context": CONTEXT, "output": "The Eiffel Tower is in Paris."},
                    {"context": CONTEXT, "output": "The Eiffel Tower is in Berlin, Germany."}]
        ns = harness.run_blocks("llm-evaluation", [idx], namespace={"answered": answered}, workdir=self.workdir)
        id2label = ns["nli"].model.config.id2label
        self.assertIn(ns["ENTAILMENT"], id2label.values(), id2label)
        self.assertEqual(ns["ENTAILMENT"].lower(), "entailment", id2label)
        self.assertEqual(ns["grounding"], [True, False], f"id2label={id2label}")
        self.assertAlmostEqual(ns["hallucination_rate"], 0.5)

    def test_bert_base_ner_uses_the_conll_label_set(self):
        idx = gate.block_index("nlp-tasks", 'model="dslim/bert-base-NER"')
        ns = harness.run_blocks("nlp-tasks", [idx], workdir=self.workdir)
        labels = set(ns["ner"].model.config.id2label.values())
        self.assertEqual(labels, CONLL_LABELS)
        ents = ns["ner"]("Amazon's new office opened in Seattle on Jan 5th.")
        groups = {e["entity_group"] for e in ents}
        self.assertTrue(groups <= {"PER", "ORG", "LOC", "MISC"}, ents)
        self.assertIn(("Seattle", "LOC"), {(e["word"], e["entity_group"]) for e in ents}, ents)

    def test_bart_large_cnn_limit_and_chunks_fit_after_reencoding(self):
        import transformers

        idx = gate.block_index("nlp-tasks", '"facebook/bart-large-cnn"')
        config = transformers.AutoConfig.from_pretrained("facebook/bart-large-cnn")
        self.assertEqual(config.max_position_embeddings, 1024)
        rng = random.Random(0)
        pieces = ["The quarterly report was published on schedule.", "https://example.com/a/b?c=1&d=2",
                  "def f(x): return {k: v for k, v in x.items()}", "東京の新しいオフィスが開設された。",
                  "Числа выросли на 12,5 % за квартал."]
        (self.workdir / "article.txt").write_text(" ".join(rng.choice(pieces) for _ in range(900)), encoding="utf-8")
        seen: list[int] = []

        class RecordingModel:   # stands in for the 1.6 GB checkpoint; returns a short "summary"
            def eval(self):
                return self

            def generate(self, input_ids, **kwargs):
                seen.append(int(input_ids.shape[-1]))
                return input_ids[:, :40]   # keeps BOS; decode() turns it back into text

        with mock.patch.object(transformers.AutoModelForSeq2SeqLM, "from_pretrained",
                               lambda *a, **k: RecordingModel()):
            ns = harness.run_blocks("nlp-tasks", [idx], workdir=self.workdir)
        tok = ns["sum_tokenizer"]
        self.assertEqual(tok.model_max_length, 1024)
        self.assertGreater(len(seen), 1, "the article should need more than one chunk")
        self.assertTrue(all(n <= 1024 for n in seen), seen)
        # truncation=True would hide an overflow, so re-encode each chunk the way step 4 builds it
        text = (self.workdir / "article.txt").read_text(encoding="utf-8")
        ids = tok(text, add_special_tokens=False)["input_ids"]
        chunks = [tok.decode(ids[i:i + 900]) for i in range(0, len(ids), 900)]
        longest = max(len(tok(c)["input_ids"]) for c in chunks)
        self.assertLessEqual(longest, 1024, "a decoded chunk re-tokenizes past the model limit")

    def test_imdb_splits_are_stored_sorted_by_label(self):
        import datasets

        ds = datasets.load_dataset("imdb")
        for split in ("train", "test"):
            labels = ds[split]["label"]
            self.assertEqual(sorted(set(labels)), [0, 1], split)
            self.assertEqual(list(labels), sorted(labels), f"{split} is not stored sorted by label")
        self.assertEqual(len(set(ds["test"].select(range(500))["label"])), 1)   # the pre-fix subset
        shuffled = ds["test"].shuffle(seed=42).select(range(min(500, len(ds["test"]))))   # step 3's fix
        self.assertEqual(set(shuffled["label"]), {0, 1})


@gate.opt_in("SPACY", what="spaCy's en_core_web_sm entity label set", modules=("spacy", "en_core_web_sm"))
class TestSpacySmallModelLabels(unittest.TestCase):
    def test_en_core_web_sm_has_the_18_ontonotes_entity_types(self):
        import spacy

        labels = set(spacy.load("en_core_web_sm").get_pipe("ner").labels)
        self.assertEqual(len(labels), 18, sorted(labels))
        self.assertTrue({"PERSON", "ORG", "GPE", "DATE", "MONEY", "PRODUCT"} <= labels, sorted(labels))


if __name__ == "__main__":
    unittest.main()
