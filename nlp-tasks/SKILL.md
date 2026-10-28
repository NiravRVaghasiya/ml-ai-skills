---
name: nlp-tasks
display_name: NLP Tasks (Tokenization, NER, Classification, Summarization)
description: >
  Use when the user wants to tokenize text, run named entity recognition,
  classify documents, or summarize text using classical/transformer NLP
  pipelines. Trigger phrases: "tokenize this text", "extract entities from
  this document", "classify these support tickets", "summarize this
  article", "build an NER pipeline", "fine-tune a text classifier". NOT for
  open-ended generation or few-shot prompting of large LLMs (see
  prompt-engineering) or retrieval-augmented question answering (see
  rag-pipeline).
type: workflow
domain: specialized
level: intermediate
lifecycle: stable
risk_level: low
evidence_level: official-documentation
last_verified: 2026-10-27
capabilities:
  - word-subword-tokenization-comparison
  - named-entity-recognition-pipeline
  - transformer-text-classification-finetuning
  - long-document-summarization-chunking
  - nlp-task-metric-selection
requires:
conflicts:
related:
  - attention-mechanisms
  - rnn-sequence
  - prompt-engineering
  - rag-pipeline
inputs: Raw text documents (single strings or a corpus) needing tokenization, entity extraction, classification labels, or summarization.
outputs: A configured tokenizer/pipeline plus task-appropriate evaluation metrics (seqeval F1, macro-F1, ROUGE) for the chosen NLP task.
version_constraints:
  - "transformers>=5.21: token-classification pipeline (aggregation_strategy=), TrainingArguments(eval_strategy=), Trainer(processing_class=, compute_metrics=), AutoModelForSeq2SeqLM.generate; the summarization pipeline task raises KeyError on 5.21 (executed against transformers 5.21.0 on 2026-10-27 with tiny local models; older releases not tested)"
  - "datasets>=5.1: load_dataset / shuffle / select / map(batched=True) (executed against datasets 5.1.0 on 2026-10-27 with load_dataset stubbed; older releases not tested)"
  - "spacy>=3.8: spacy.load() Doc/Token attributes (executed against spacy 3.8.18 on 2026-10-27 with spacy.blank standing in for en_core_web_sm; older releases not tested)"
  - "seqeval>=1.2.2: seqeval.metrics f1_score / classification_report (executed against seqeval 1.2.2 on 2026-10-27)"
  - "rouge-score>=0.1.2: RougeScorer.score(target, prediction) (executed against rouge-score 0.1.2 on 2026-10-27)"
  - "scikit-learn>=1.9: f1_score(average=macro) (executed against scikit-learn 1.9.0 on 2026-10-27; older releases not tested)"
---

## Overview
Covers the four workhorse NLP tasks — tokenization, named entity recognition
(NER), text classification, and summarization — using spaCy and Hugging Face
Transformers pipelines. The focus is task-specific supervised/pretrained
models (encoder classifiers, seq2seq summarizers), not prompting a generative
LLM. The user walks away with a runnable pipeline for each task plus the
right metric to evaluate it.

## Workflow
1. **Tokenize and inspect.** Word-level (spaCy) and subword (Hugging Face)
   tokenization behave differently — check both before choosing a model,
   since NER label alignment depends on it.
   ```python
   import spacy
   from transformers import AutoTokenizer

   nlp = spacy.load("en_core_web_sm")  # install once: python -m spacy download en_core_web_sm
   doc = nlp("Amazon's new office opened in Seattle on Jan 5th.")
   print([(t.text, t.pos_) for t in doc])

   tokenizer = AutoTokenizer.from_pretrained("bert-base-uncased")
   enc = tokenizer("Amazon's new office opened in Seattle.", return_offsets_mapping=True)
   print(tokenizer.convert_ids_to_tokens(enc["input_ids"]))
   ```
2. **Named entity recognition.** Start with a pretrained pipeline; fall back
   to a transformer NER model for finer-grained or noisier text.
   ```python
   from transformers import pipeline

   ner = pipeline(
       "token-classification",
       model="dslim/bert-base-NER",
       aggregation_strategy="simple",
   )
   for ent in ner("Amazon's new office opened in Seattle on Jan 5th."):
       print(ent["word"], ent["entity_group"], round(ent["score"], 3))
   ```
3. **Text classification.** For domain-specific labels, fine-tune a small
   transformer encoder rather than relying on zero-shot. Shuffle *before*
   subsetting (a split stored sorted by label yields a single-class subset),
   pass the tokenizer so batches are padded dynamically, and report macro-F1.
   ```python
   import numpy as np
   from datasets import load_dataset
   from sklearn.metrics import f1_score
   from transformers import (
       AutoTokenizer, AutoModelForSequenceClassification,
       TrainingArguments, Trainer,
   )

   ds = load_dataset("imdb")
   train = ds["train"].shuffle(seed=42).select(range(min(2000, len(ds["train"]))))
   # Held-out set for per-epoch eval; if you pick epochs/hyperparameters on it,
   # carve a validation split from train and keep the test split for the final number.
   test = ds["test"].shuffle(seed=42).select(range(min(500, len(ds["test"]))))

   checkpoint = "distilbert-base-uncased"
   tokenizer = AutoTokenizer.from_pretrained(checkpoint)

   def tokenize(batch):
       return tokenizer(batch["text"], truncation=True, max_length=256)  # padding happens per batch

   train_tok, test_tok = train.map(tokenize, batched=True), test.map(tokenize, batched=True)
   model = AutoModelForSequenceClassification.from_pretrained(checkpoint, num_labels=2)

   def compute_metrics(eval_pred):
       logits, labels = eval_pred
       preds = np.argmax(logits, axis=-1)
       return {"macro_f1": f1_score(labels, preds, average="macro"),
               "accuracy": float((preds == labels).mean())}

   args = TrainingArguments(
       output_dir="clf-out", per_device_train_batch_size=16,
       num_train_epochs=2, eval_strategy="epoch",  # Transformers 5 rejects evaluation_strategy
   )
   trainer = Trainer(
       model=model, args=args,
       train_dataset=train_tok, eval_dataset=test_tok,
       processing_class=tokenizer,  # -> DataCollatorWithPadding; without it ragged batches fail
       compute_metrics=compute_metrics,
   )
   trainer.train()
   print(trainer.evaluate())
   ```
4. **Summarization.** Use a pretrained seq2seq summarizer; chunk long
   documents by *tokens* to stay under the model's max input length (1024 for
   BART), then summarize the joined partial summaries (map-reduce). Transformers
   5 removed the `"summarization"` pipeline task, so call `generate()` directly.
   ```python
   from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

   sum_checkpoint = "facebook/bart-large-cnn"
   sum_tokenizer = AutoTokenizer.from_pretrained(sum_checkpoint)
   sum_model = AutoModelForSeq2SeqLM.from_pretrained(sum_checkpoint).eval()

   def summarize(text: str, max_new_tokens: int = 120) -> str:
       inputs = sum_tokenizer(text, truncation=True, max_length=1024, return_tensors="pt")
       out = sum_model.generate(**inputs, num_beams=4, do_sample=False, max_new_tokens=max_new_tokens)
       return sum_tokenizer.decode(out[0], skip_special_tokens=True)

   def summarize_long(text: str, chunk_tokens: int = 900) -> str:
       # chunk_tokens < 1024 leaves room for special tokens; each pass shrinks the
       # text because a summary (<= 120 new tokens) is shorter than its chunk.
       ids = sum_tokenizer(text, add_special_tokens=False)["input_ids"]
       if not ids:
           return ""
       if len(ids) <= chunk_tokens:
           return summarize(text)
       chunks = [sum_tokenizer.decode(ids[i:i + chunk_tokens]) for i in range(0, len(ids), chunk_tokens)]
       return summarize_long(" ".join(summarize(c) for c in chunks), chunk_tokens)

   with open("article.txt", encoding="utf-8") as f:  # the long document to summarize
       long_article_text = f.read()
   print(summarize_long(long_article_text))
   ```
5. **Evaluate with task-appropriate metrics.** Accuracy alone is misleading
   for NER/classification with imbalanced labels; ROUGE is standard for
   summarization. These libraries compute locally; `evaluate.load(...)` would
   instead fetch each metric script from the Hugging Face Hub at run time.
   ```python
   from rouge_score import rouge_scorer
   from seqeval.metrics import classification_report, f1_score as entity_f1
   from sklearn.metrics import f1_score

   # NER: entity-level (span) scores over IOB2 tags, one list per sentence
   ner_true = [["B-ORG", "O", "O", "O", "O", "B-LOC", "O", "B-DATE", "I-DATE"]]
   ner_pred = [["B-ORG", "O", "O", "O", "O", "B-LOC", "O", "O", "O"]]
   print("entity F1:", entity_f1(ner_true, ner_pred))
   print(classification_report(ner_true, ner_pred, zero_division=0))

   # Classification: macro-F1 weights every class equally, so a minority class counts
   y_true, y_pred = [0, 0, 0, 0, 1, 1], [0, 0, 0, 0, 0, 1]
   print("macro-F1:", f1_score(y_true, y_pred, average="macro"))

   # Summarization: ROUGE against a reference summary
   scorer = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=True)
   ref_summary = "Amazon's new Seattle office opened on January 5."
   pred_summary = "Amazon opened a new office in Seattle."
   print(scorer.score(ref_summary, pred_summary))  # score(target, prediction)
   ```

## Gotchas
- **Subword/word label misalignment.** Transformer tokenizers split words
  into subword pieces, so word-level NER labels must be re-aligned using
  `tokenizer(...).word_ids()` — naively assigning one label per token
  corrupts the training signal.
- **Silent truncation on summarization inputs.** BART/T5 summarizers have a
  hard input limit (e.g., 1024 tokens for `facebook/bart-large-cnn`); with
  `truncation=True` text beyond it is silently dropped, and without it the
  model can error on over-long inputs. Chunk by tokens as above — character
  chunks (e.g. 3000 chars) usually fit English prose but can exceed the limit
  for code, URLs, or non-Latin scripts that tokenize into more pieces.
- **Class imbalance skews accuracy.** Support-ticket or spam classifiers are
  rarely balanced — report macro-F1 or per-class precision/recall, not raw
  accuracy, or a majority-class classifier will look "good."
- **Pretrained NER label sets are generic.** CoNLL-2003-style models such as
  `dslim/bert-base-NER` tag only PER/ORG/LOC/MISC, and spaCy's
  `en_core_web_sm` uses the 18 OntoNotes types (PERSON, ORG, GPE, DATE, MONEY,
  PRODUCT, ...) — domain entities (product SKUs, drug names) need a fine-tuned
  or gazetteer-augmented model. Map label names explicitly when comparing models.
- **Copied training/pipeline code breaks across major versions.** Transformers 5
  rejects `TrainingArguments(evaluation_strategy=...)` (use `eval_strategy`)
  and removed the `"summarization"` pipeline task (`KeyError`); `Trainer`
  without `processing_class=`/`data_collator=` falls back to a collator that
  cannot pad variable-length batches.
- **Tokenizer/model mismatch.** Loading a tokenizer from a different model
  family than the one you fine-tune (e.g., BERT tokenizer with a RoBERTa
  model) silently produces garbage inputs — always pair `AutoTokenizer` and
  `AutoModel*` from the same checkpoint name.

## References
- [Hugging Face Transformers: Pipelines](https://huggingface.co/docs/transformers/main_classes/pipelines) — canonical reference for the task pipelines used here (NER, classification); Transformers 5 removed the summarization task, so step 4 calls `generate()` instead.
- [spaCy 101](https://spacy.io/usage/spacy-101) — tokenization and linguistic annotation fundamentals.
- [Hugging Face: Token classification guide](https://huggingface.co/docs/transformers/tasks/token_classification) — the correct way to align subword tokens with NER labels.
- [Lewis et al., 2019 — BART: Denoising Sequence-to-Sequence Pre-training](https://arxiv.org/abs/1910.13461) — the seq2seq model family behind `facebook/bart-large-cnn` used for summarization in step 4.
