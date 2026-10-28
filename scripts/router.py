#!/usr/bin/env python3
"""Deterministic, metadata-driven skill router.

No embeddings, no LLM call, no network: a weighted lexical match of the task
text against each skill's metadata (display name/slug, `capabilities`, the
quoted trigger phrases and the rest of the `description`, the `## Overview`
paragraph), with

* Porter stemming plus a small, documented synonym map for paraphrases,
* IDF weighting, so words that occur in many skills ("model", "data") count
  for little and distinctive ones ("shap", "arima") count for a lot,
* phrase evidence: adjacent-word pairs and capability tags / trigger phrases
  scored by the *fraction* of their words the task contains,
* negative evidence: words in a negated or "already done" clause ("I don't
  need X", "my data is already cleaned") and words the skill's own `NOT for`
  clause hands to a sibling,
* a type (workflow/reference) bonus that only applies on top of lexical
  evidence, and
* explicit confidence gating: an absolute minimum score, a relative cutoff
  against the top hit, and a no-skill fallback that returns no hits plus a
  reason when nothing clears the bar.

See docs/ROUTING.md for the algorithm, thresholds, measured before/after
benchmark numbers (evals/routing/), known failure modes and the extension
point for semantic retrieval (not enabled; see that doc for why).

Usage:
    python scripts/router.py "Build a fraud detection model and evaluate it
        under severe class imbalance."
    python scripts/router.py --json "explain attention"
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills_lib as sl  # noqa: E402

# --------------------------------------------------------------------------- thresholds / weights
# Tuned on the benchmark's dev split only (evals/routing/benchmark.json,
# `python scripts/eval_router.py --split dev`); see docs/ROUTING.md.

#: A skill needs at least this score to be selected at all.
MIN_SCORE = 5.0
#: ...and at least this fraction of the top hit's score (multi-skill requests
#: keep their secondary skills; weak tag-alongs are dropped).
RELATIVE_CUTOFF = 0.6
#: When the task contains no ML-domain anchor (see STRONG_/WEAK_ANCHOR_WORDS) the
#: minimum score is multiplied by this: an off-topic request has to match a
#: skill much more strongly before it is routed.
NO_ANCHOR_FACTOR = 2.0

FIELD_WEIGHTS = {"name": 3.0, "capability": 3.0, "trigger": 2.5, "description": 1.5, "overview": 0.75}
BIGRAM_WEIGHT = 1.5          # per adjacent word pair shared with a name/tag/trigger phrase
CAPABILITY_TAG_WEIGHT = 2.5  # per capability tag, x the IDF-weighted fraction of its words present
TRIGGER_PHRASE_WEIGHT = 3.0  # per trigger phrase, x the fraction of its words present
PHRASE_MIN_FRACTION = 0.6    # tags/trigger phrases below this fraction contribute no phrase bonus
SYNONYM_WEIGHT = 0.7         # words added by the synonym map count for less than literal words
INTENT_BONUS = 0.15          # x(1 + bonus) when an explicit intent cue matches the skill's type
NEGATION_PENALTY = 1.0       # x the skill's score on the negated words, subtracted
NOT_FOR_PENALTY = 1.0        # x IDF of task words found only in the skill's NOT-for clause
NEGATION_FIELDS = ("name", "capability", "trigger")  # where a negated word must hit to cost a skill
RESIDUAL_MIN = 10.0          # a lower-ranked skill is kept if it scores this on words the others left uncovered
COVER_FIELDS = ("name", "capability", "trigger")  # a selected skill "covers" the task words it has here
MODIFIER_CONFLICT_FACTOR = 0.3  # x weight of a word the task uses in a compound this skill never uses

# --------------------------------------------------------------------------- normalisation

STOPWORDS = frozenset("""
a an the this that these those is are was were be been being am to of in on for and or nor with my me
mine i it its as at by from how do does did done can could should would will shall may might must
using use used into under over our ours we us you your yours they them their he she his her
what which who whom whose when where why there here than then so such very too also just only any
some all each every other another same own more most much many few lot lots one two three
not no don't dont doesn't didn't won't can't cannot isn't aren't wasn't weren't haven't hasn't
i'm im i've ive i'd i'll we're we've you're it's that's there's let's let
s t d ll re ve m
about after again against before between both during until while because if else
up down out off once yet still even
have has had having get gets got getting make makes made making want wants wanted need needs needed
please help like know tell give take put way ways thing things something anything stuff
good better best bad really actually maybe now right well sure
""".split())

# Domain anchors: an ML-skill library should only route a request that is
# about ML at all. Matched on stems; a task with none of these needs
# NO_ANCHOR_FACTOR x the usual minimum score.
# One STRONG anchor is enough; WEAK anchors are everyday English words too
# ("model railway", "train my puppy", "cluster headaches"), so two distinct
# ones are needed.
STRONG_ANCHOR_WORDS = (
    "ml", "ai", "neural", "llm", "dataset", "classifier", "classification", "regression", "embedding",
    "transformer", "hyperparameter", "overfitting", "pytorch", "tensorflow", "sklearn", "scikit", "numpy",
    "pandas", "gpu", "lora", "rag", "nlp", "inference", "epoch", "supervised",
    "unsupervised", "chatbot", "backpropagation", "dataframe", "calibration", "underfitting",
    "imputation", "quantization", "lstm", "rnn", "cnn", "convolution", "softmax", "logits", "shap",
    "xgboost", "lightgbm", "mlflow", "optuna", "arima", "sarima", "pca", "tsne", "kmeans", "dbscan",
    "bert", "gpt", "onnx", "peft", "qlora", "rlhf", "qlearning", "auc", "dataloader", "tokenizer",
)
WEAK_ANCHOR_WORDS = (
    "model", "learning", "network", "data", "train", "feature", "prediction", "predict", "algorithm",
    "deep", "label", "accuracy", "metric", "agent", "prompt", "retrieval", "tensor", "gradient", "cluster",
    "forecast", "statistical", "hypothesis", "probability", "fairness", "bias", "drift", "pipeline",
    "vision", "token", "experiment", "evaluate", "deploy", "tune", "reinforcement",
)

# Synonym / colloquialism map: phrase -> canonical words appended to the
# task (weighted SYNONYM_WEIGHT). Keys and values are stemmed at import time.
# Caveat (measured, see docs/ROUTING.md): the map was written after reading
# the benchmark's dev queries and is partly fitted to them - 30 keys each
# occur in exactly one dev query, and removing the whole map costs dev task
# accuracy 0.051 but nothing on the held-out test split. Treat entries as
# plausible general equivalences that are only validated on dev.
SYNONYMS: dict[str, str] = {
    # LLM / RAG / agents
    "language model": "llm", "chatbot": "llm", "chat bot": "llm", "bot": "llm", "gpt": "llm", "assistant": "llm",
    "retrieval augmented": "rag retrieval", "look up": "retrieval", "lookup": "retrieval",
    "vector store": "vector database", "vector db": "vector database",
    "function calling": "tool calling", "tool use": "tool calling",
    "hidden instructions": "prompt injection", "hide instructions": "prompt injection",
    "injected instructions": "prompt injection", "ignore previous instructions": "prompt injection",
    "ignore all previous instructions": "prompt injection", "jailbreak": "prompt injection",
    "make things up": "hallucination", "made up": "hallucination",
    "grade": "evaluate score", "grader": "evaluate judge", "grading": "evaluate score",
    # data / classical ML
    "blank": "missing", "blanks": "missing", "null": "missing", "nulls": "missing", "nan": "missing",
    "labelled": "supervised label", "labeled": "supervised label",
    "strip": "remove", "scrub": "remove", "redact": "remove",
    "spreadsheet": "tabular", "csv": "tabular", "excel": "tabular",
    "held out": "validation", "holdout": "validation", "folds": "cross validation",
    "machine learning": "ml",
    # statistics
    "by chance": "statistically significant",
    "random chance": "statistically significant", "a/b": "ab test",
    # deep learning training
    "half precision": "mixed precision", "fp16": "mixed precision", "bf16": "mixed precision",
    "step size": "learning rate", "warm up": "warmup", "ramp up": "warmup",
    "neural net": "neural network", "trial and error": "reinforcement reward",
    # deployment / monitoring / CI
    "http": "api endpoint", "docker": "container",
    "distribution shift": "drift", "shift": "drift",
    "pull request": "ci", "github actions": "ci",
    "shrink": "smaller", "compress": "smaller",
    # responsible AI / privacy
    "personal data": "pii", "personal information": "pii", "personally identifiable": "pii",
    "equity": "fairness", "discrimination": "fairness bias", "disparity": "fairness",
    "ethnic": "fairness group", "gender": "fairness group", "race": "fairness group",
    "protected attribute": "fairness", "attribution": "explain feature importance",
    "contributions": "explain feature importance",
    # vision
    "bounding box": "object detection", "bounding boxes": "object detection",
    "pixel by pixel": "segmentation", "per pixel": "segmentation", "pixel level": "segmentation",
}

# Words Porter maps to different stems although they name the same concept.
STEM_ALIASES = {"classif": "classifi", "privat": "privaci", "explan": "explain", "categor": "categori",
                "detector": "detect", "regressor": "regress", "predictor": "predict",
                "statistician": "statist", "built": "build", "imbalanc": "imbal", "bias": "bia"}

# Words Porter conflates with an unrelated concept ("customer" -> "custom",
# as in "custom Dataset"; "general" -> "gener", as in "generation").
STEM_EXCEPTIONS = {"customer": "customer", "customers": "customer", "general": "general",
                   "generally": "general"}
_BRITISH_ISE_RE = re.compile(r"is(e|ed|es|ing|ation|ations)$")

# Exclusion cues: words after one of these, up to the end of the clause, are
# negated ("I don't need X", "this is not X", "no X") or already done
# ("my data is already cleaned"). See _split_negated for the conditions.
#: Always exclusion cues (one or two words).
NEGATION_CUES = frozenset({"don't", "dont", "do not", "nor", "rather than", "instead of", "already"})
#: Exclusion cues unless they introduce an undesired outcome ("without leakage",
#: "with no data leakage" name the problem to prevent, i.e. the topic).
AVOIDANCE_CUES = frozenset({"no", "without"})
#: Negators that can also describe a *state* or symptom ("my network is not
#: training", "why isn't my LSTM learning"); exclusion only when they negate a
#: request - see _negates_request. "never" is deliberately not a cue at all:
#: in requests it describes a symptom or a requirement ("never predicts the
#: minority class", "so the two never drift apart"), not an unwanted skill.
STATE_NEGATORS = frozenset({"not", "isn't", "aren't", "wasn't", "weren't"})
#: A state negator negates a request when its subject is the speaker or the
#: request itself ("I'm not asking", "this is not a fairness audit", "..., not
#: a bias question") or it negates a noun phrase ("is not a transformer").
REQUEST_SUBJECTS = frozenset({"i", "i'm", "im", "am", "we", "we're", "this", "that", "it", "it's", "that's",
                              "do", "and", "or", "but", "please", "just", "also", ""})
BE_VERBS = frozenset({"is", "are", "was", "were", "am"})
DETERMINERS = frozenset({"a", "an", "the", "about", "for"})
#: Phrases that look negative but do not exclude anything ("I have no idea how to tune X").
NON_NEGATING = frozenset({"no idea", "no clue", "no luck", "no experience", "no longer", "no matter",
                          "not sure", "not certain", "don't know", "dont know", "don't understand",
                          "dont understand", "don't get", "dont get"})
#: Undesired outcomes the skills exist to prevent: "without <one of these>"
#: (or "no <x>" within the next two content words) is the topic, not an exclusion.
PROBLEM_WORDS = ("leakage", "leak", "leaking", "overfitting", "overfit", "underfitting", "bias", "biased",
                 "drift", "hallucination", "hallucinations", "hallucinating", "forgetting", "downtime")
CLAUSE_BREAKS = frozenset({".", ",", ";", ":", "!", "?", "(", ")", "but", "just", "only", "now",
                           "so", "then", "though", "however"})

REFERENCE_CUES = (
    "what is", "what's", "what are", "explain", "how does", "how do",
    "why does", "why do", "why is", "difference between", "understand",
    "intuition", "concept of", "meaning of",
)
WORKFLOW_CUES = (
    "build", "train", "evaluate", "deploy", "clean", "tune", "fine-tune",
    "finetune", "implement", "write", "create", "fix", "debug", "optimize",
    "preprocess", "compare", "calibrate", "ship", "monitor", "retrain",
    "set up", "design a pipeline", "reduce", "handle",
)


# --------------------------------------------------------------------------- Porter stemmer
# M.F. Porter, "An algorithm for suffix stripping", Program 14(3), 1980.
# Implemented from the published rules (incl. the later bli->ble / logi->log
# step-2 revisions); checked against the paper's examples in tests/test_router.py.

def _is_cons(w: str, i: int) -> bool:
    ch = w[i]
    if ch in "aeiou":
        return False
    if ch == "y":
        return i == 0 or not _is_cons(w, i - 1)
    return True


def _measure(stem: str) -> int:
    m, i, n = 0, 0, len(stem)
    while i < n and _is_cons(stem, i):
        i += 1
    while i < n:
        while i < n and not _is_cons(stem, i):
            i += 1
        if i >= n:
            break
        while i < n and _is_cons(stem, i):
            i += 1
        m += 1
    return m


def _has_vowel(stem: str) -> bool:
    return any(not _is_cons(stem, i) for i in range(len(stem)))


def _double_cons(w: str) -> bool:
    return len(w) >= 2 and w[-1] == w[-2] and _is_cons(w, len(w) - 1)


def _cvc(w: str) -> bool:
    return (len(w) >= 3 and _is_cons(w, len(w) - 3) and not _is_cons(w, len(w) - 2)
            and _is_cons(w, len(w) - 1) and w[-1] not in "wxy")


_STEP2 = (("ational", "ate"), ("tional", "tion"), ("enci", "ence"), ("anci", "ance"), ("izer", "ize"),
          ("bli", "ble"), ("alli", "al"), ("entli", "ent"), ("eli", "e"), ("ousli", "ous"),
          ("ization", "ize"), ("ation", "ate"), ("ator", "ate"), ("alism", "al"), ("iveness", "ive"),
          ("fulness", "ful"), ("ousness", "ous"), ("aliti", "al"), ("iviti", "ive"), ("biliti", "ble"),
          ("logi", "log"))
_STEP3 = (("icate", "ic"), ("ative", ""), ("alize", "al"), ("iciti", "ic"), ("ical", "ic"), ("ful", ""),
          ("ness", ""))
_STEP4 = ("al", "ance", "ence", "er", "ic", "able", "ible", "ant", "ement", "ment", "ent", "ion", "ou",
          "ism", "ate", "iti", "ous", "ive", "ize")


def _replace_longest(w: str, rules: tuple, min_m: int) -> str:
    for suf, rep in sorted(rules, key=lambda r: -len(r[0])):
        if w.endswith(suf):
            stem = w[: -len(suf)]
            return stem + rep if _measure(stem) > min_m else w
    return w


def porter_stem(w: str) -> str:
    if len(w) <= 2:
        return w
    if w.endswith("sses") or w.endswith("ies"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    flag = False
    if w.endswith("eed"):
        if _measure(w[:-3]) > 0:
            w = w[:-1]
    elif w.endswith("ed") and _has_vowel(w[:-2]):
        w, flag = w[:-2], True
    elif w.endswith("ing") and _has_vowel(w[:-3]):
        w, flag = w[:-3], True
    if flag:
        if w.endswith(("at", "bl", "iz")):
            w += "e"
        elif _double_cons(w) and w[-1] not in "lsz":
            w = w[:-1]
        elif _measure(w) == 1 and _cvc(w):
            w += "e"
    if w.endswith("y") and _has_vowel(w[:-1]):
        w = w[:-1] + "i"
    w = _replace_longest(w, _STEP2, 0)
    w = _replace_longest(w, _STEP3, 0)
    for suf in sorted(_STEP4, key=len, reverse=True):
        if w.endswith(suf):
            stem = w[: -len(suf)]
            if _measure(stem) > 1 and (suf != "ion" or stem.endswith(("s", "t"))):
                w = stem
            break
    if w.endswith("e"):
        stem = w[:-1]
        m = _measure(stem)
        if m > 1 or (m == 1 and not _cvc(stem)):
            w = stem
    if _measure(w) > 1 and _double_cons(w) and w.endswith("l"):
        w = w[:-1]
    return w


_STEM_CACHE: dict[str, str] = {}


def stem(word: str) -> str:
    """Porter stem + British->American -ise and a few concept aliases."""
    cached = _STEM_CACHE.get(word)
    if cached is None:
        if word in STEM_EXCEPTIONS:
            cached = STEM_EXCEPTIONS[word]
        else:
            w = _BRITISH_ISE_RE.sub(r"iz\1", word) if len(word) > 5 else word
            s = porter_stem(w)
            cached = STEM_ALIASES.get(s, s)
        _STEM_CACHE[word] = cached
    return cached


_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?|[.,;:!?()]")


_LETTER_COMPOUND_RE = re.compile(r"\b([a-z])-(?=[a-z]{2})")


def _words(text: str) -> list[str]:
    """Lower-cased words (apostrophes kept, so "don't" stays one word) and
    clause punctuation. "a/b" becomes "ab"; a single letter hyphenated to a
    word is joined to it ("q-learning" -> "qlearning", "k-means" -> "kmeans")
    because a bare "q" or "k" carries no meaning on its own."""
    text = text.lower().replace("\u2019", "'").replace("a/b", " abtest ")
    text = _LETTER_COMPOUND_RE.sub(r"\1", text)
    out = []
    for w in _WORD_RE.findall(text):
        if w.endswith("'s"):
            w = w[:-2]
        out.append("ab" if w == "abtest" else w)
    return out


def _is_content(w: str) -> bool:
    return w not in STOPWORDS and w not in CLAUSE_BREAKS and not w.isdigit() and len(w) > 1


def _content_stems(words: list[str]) -> list[str]:
    return [stem(w) for w in words if _is_content(w)]


def tokenize(text: str) -> set[str]:
    """Normalised content tokens of `text` (stopwords dropped, Porter-stemmed)."""
    return set(_content_stems(_words(text)))


def _seq(text: str) -> list[str]:
    return _content_stems(_words(text))


def _bigrams(seq: list[str]) -> set[tuple[str, str]]:
    return {(a, b) for a, b in zip(seq, seq[1:], strict=False) if a != b}


def _all_stems(words: list[str]) -> list[str]:
    """Stems of every word (stopwords kept) - used for synonym-phrase matching."""
    return [stem(w) for w in words if w not in CLAUSE_BREAKS]


_SYNONYMS_STEMMED: list[tuple[tuple[str, ...], tuple[str, ...]]] = sorted(
    {(tuple(_all_stems(_words(k))), tuple(_seq(v))) for k, v in SYNONYMS.items()}
)
_STRONG_ANCHORS = frozenset(stem(w) for w in STRONG_ANCHOR_WORDS)
_WEAK_ANCHORS = frozenset(stem(w) for w in WEAK_ANCHOR_WORDS)
_PROBLEM_STEMS = frozenset(stem(w) for w in PROBLEM_WORDS)


def _anchored(stems: set[str]) -> bool:
    return bool(stems & _STRONG_ANCHORS) or len(stems & _WEAK_ANCHORS) >= 2


# --------------------------------------------------------------------------- task analysis

@dataclass
class TaskAnalysis:
    text: str
    intent: str                   # 'workflow' | 'reference'
    explicit_intent: bool         # was any intent cue present?
    weights: dict[str, float]     # positive stem -> weight (1.0 literal, SYNONYM_WEIGHT synonym-only)
    negated: set[str]             # stems that only occur in negated / already-done clauses
    bigrams: set[tuple[str, str]]
    has_anchor: bool
    compounds: set[tuple[str, str]] = field(default_factory=set)  # directly adjacent content-word pairs
    synonyms_used: list[str] = field(default_factory=list)


_REFERENCE_CUE_RES = tuple(re.compile(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])") for c in REFERENCE_CUES)
_WORKFLOW_CUE_RES = tuple(re.compile(r"(?<![a-z])" + re.escape(c) + r"(?![a-z])") for c in WORKFLOW_CUES)


def _intent_counts(task_text: str) -> tuple[int, int]:
    """Whole-word cue matches (so "train" does not fire inside "constraint")."""
    t = task_text.lower().replace("\u2019", "'")
    return (sum(1 for r in _REFERENCE_CUE_RES if r.search(t)), sum(1 for r in _WORKFLOW_CUE_RES if r.search(t)))


def classify_intent(task_text: str) -> str:
    """Returns 'reference' or 'workflow'. Ties (or no cue at all) default to
    'workflow' because an agent given a plain topic name ("model calibration")
    with no imperative verb is more often about to *do* something with it.
    (The scorer only applies the type bonus when a cue was actually present.)"""
    ref_hits, wf_hits = _intent_counts(task_text)
    return "reference" if ref_hits > wf_hits else "workflow"


def _negates_request(words: list[str], i: int) -> bool:
    """Is the state negator at words[i] ("not", "isn't", ...) excluding
    something the user does not want, rather than describing a symptom?

    Exclusion: "I'm not asking about X", "this is not a fairness audit",
    "this isn't a one-off cleanup", "a privacy task, not a bias question",
    "my model is not a transformer". State (no exclusion): "my network is not
    training", "the loss is not improving", "why isn't my LSTM learning"."""
    nxt = words[i + 1] if i + 1 < len(words) else ""
    if nxt in DETERMINERS:
        return True
    prev = words[i - 1] if i > 0 else ""
    if words[i] == "not" and prev in BE_VERBS:  # "<subject> is not ..." -> look at the subject
        prev = words[i - 2] if i > 1 else ""
    return prev in REQUEST_SUBJECTS or prev in CLAUSE_BREAKS


def _avoids_problem(words: list[str], i: int) -> bool:
    """"without leakage", "with no data leakage": the next two content words
    name an undesired outcome, so the clause is the topic, not an exclusion."""
    following = [w for w in words[i + 1:i + 6] if w not in CLAUSE_BREAKS and _is_content(w)][:2]
    return any(stem(w) in _PROBLEM_STEMS for w in following)


def _split_negated(words: list[str]) -> tuple[list[str], list[str]]:
    """Partition words into (affirmed, negated) by exclusion-cue scope. A
    scope runs from the cue to the next clause break."""
    affirmed: list[str] = []
    negated: list[str] = []
    in_scope = False
    skip = 0
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        pair = f"{w} {nxt}"
        if skip:
            skip -= 1
            (negated if in_scope else affirmed).append(w)
            continue
        if w in CLAUSE_BREAKS:
            in_scope = False
        elif pair in NON_NEGATING:
            skip = 1  # "no idea", "not sure", "don't know": plain words, no scope change
            (negated if in_scope else affirmed).append(w)
            continue
        elif pair in NEGATION_CUES:
            in_scope, skip = True, 1
            continue
        elif w in NEGATION_CUES:
            in_scope = True
        elif w in AVOIDANCE_CUES:
            if not _avoids_problem(words, i):
                in_scope = True
        elif w in STATE_NEGATORS:
            if _negates_request(words, i):
                in_scope = True
        elif in_scope:
            negated.append(w)
        else:
            affirmed.append(w)
    return affirmed, negated


def analyze_task(task_text: str) -> TaskAnalysis:
    words = _words(task_text)
    affirmed, negated_words = _split_negated(words)
    aff_seq = _content_stems(affirmed)
    weights: dict[str, float] = {s: 1.0 for s in aff_seq}
    full_seq = _content_stems(words)
    aff_all = _all_stems(affirmed)
    used = []
    for key, value in _SYNONYMS_STEMMED:
        n = len(key)
        if n and any(tuple(aff_all[i:i + n]) == key for i in range(len(aff_all) - n + 1)):
            used.append(" ".join(key))
            for s in value:
                weights[s] = max(weights.get(s, 0.0), SYNONYM_WEIGHT)
    negated = set(_content_stems(negated_words)) - set(aff_seq)
    ref_hits, wf_hits = _intent_counts(task_text)
    return TaskAnalysis(
        text=task_text,
        intent="reference" if ref_hits > wf_hits else "workflow",
        explicit_intent=(ref_hits + wf_hits) > 0 and ref_hits != wf_hits,
        weights=weights,
        negated=negated,
        bigrams=_bigrams(aff_seq),
        compounds={(stem(a), stem(b)) for a, b in zip(words, words[1:], strict=False)
                   if _is_content(a) and _is_content(b)},
        has_anchor=_anchored(set(full_seq) | set(weights)),
        synonyms_used=used,
    )


# --------------------------------------------------------------------------- skill index

_NOT_SPLIT_RE = re.compile(r"\bNOT\b")
_SEE_RE = re.compile(r"\(see ([a-z0-9, -]+)\)")
_QUOTE_RE = re.compile(r"\"([^\"]+)\"")


def _overview(body: str) -> str:
    m = re.search(r"^## Overview\s*\n(.*?)(?=^## |\Z)", body, flags=re.M | re.S)
    return m.group(1) if m else ""


@dataclass
class SkillProfile:
    slug: str
    type: str
    domain: str
    field_of: dict[str, str]                       # stem -> strongest field it occurs in
    tags: list[tuple[str, list[str]]]              # (tag, stems)
    triggers: list[tuple[str, list[str]]]          # (phrase, stems)
    bigrams: set[tuple[str, str]]
    not_only: set[str]                             # stems found only in the NOT-for clause


def _profile(skill: sl.Skill) -> SkillProfile:
    fm = skill.frontmatter
    desc = " ".join(str(fm.get("description") or "").split())
    parts = _NOT_SPLIT_RE.split(desc, maxsplit=1)
    positive, not_clause = parts[0], (parts[1] if len(parts) > 1 else "")
    triggers = [(p, _seq(p)) for p in _QUOTE_RE.findall(positive)]
    positive_plain = _QUOTE_RE.sub(" ", positive)
    tags = [(c, _seq(c)) for c in (fm.get("capabilities") or [])]
    name_seq = _seq(f"{fm.get('display_name', '')} {skill.slug.replace('-', ' ')}")
    overview = " ".join([_overview(sl.strip_code(skill.body) if skill.body else ""),
                         str(fm.get("inputs") or ""), str(fm.get("outputs") or "")])

    field_of: dict[str, str] = {}
    ranked = sorted(FIELD_WEIGHTS, key=lambda f: -FIELD_WEIGHTS[f])
    seqs = {"name": [name_seq], "capability": [s for _, s in tags], "trigger": [s for _, s in triggers],
            "description": [_seq(positive_plain)], "overview": [_seq(overview)]}
    for f in reversed(ranked):  # stronger fields overwrite weaker ones
        for seq in seqs[f]:
            for s in seq:
                field_of[s] = f
    bigrams: set[tuple[str, str]] = set()
    for seq in [name_seq, *seqs["capability"], *seqs["trigger"], *seqs["description"]]:
        bigrams |= _bigrams(seq)
    not_seq = _seq(_SEE_RE.sub(" ", not_clause))
    return SkillProfile(slug=skill.slug, type=str(fm.get("type") or ""), domain=str(fm.get("domain") or ""),
                        field_of=field_of, tags=tags, triggers=triggers, bigrams=bigrams,
                        not_only=set(not_seq) - set(field_of))


@dataclass
class SkillIndex:
    profiles: list[SkillProfile]
    idf: dict[str, float]

    def idf_of(self, s: str) -> float:
        return self.idf.get(s, self.max_idf)

    @property
    def max_idf(self) -> float:  # IDF of a word no skill uses (df = 0)
        return math.log(1 + (len(self.profiles) + 0.5) / 0.5)


def build_index(skills: list[sl.Skill]) -> SkillIndex:
    profiles = [_profile(s) for s in sorted(skills, key=lambda s: s.slug)]
    n = len(profiles)
    df: dict[str, int] = {}
    for p in profiles:
        for s in p.field_of:
            df[s] = df.get(s, 0) + 1
    # BM25 (Lucene) IDF: always > 0; ~3.3 for a word in 1 of 38 skills, ~0.01 for one in all 38.
    idf = {s: math.log(1 + (n - d + 0.5) / (d + 0.5)) for s, d in sorted(df.items())}
    return SkillIndex(profiles=profiles, idf=idf)


_INDEX_CACHE: dict[tuple, SkillIndex] = {}


def _index_for(skills: list[sl.Skill]) -> SkillIndex:
    key = tuple((s.slug, s.raw) for s in skills)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        _INDEX_CACHE.clear()
        idx = _INDEX_CACHE[key] = build_index(skills)
    return idx


# --------------------------------------------------------------------------- scoring

@dataclass
class RouteHit:
    slug: str
    score: float
    reasons: list[str]
    type: str
    domain: str
    confidence: float = 0.0  # score / (score + threshold); 0.5 = exactly at threshold. Not a probability.


def _lexical(profile: SkillProfile, index: SkillIndex, weights: dict[str, float],
             bigrams: set[tuple[str, str]], reasons: list[str] | None,
             fields: tuple[str, ...] = tuple(FIELD_WEIGHTS),
             compounds: set[tuple[str, str]] = frozenset()) -> float:
    score = 0.0
    by_field: dict[str, list[str]] = {}
    # Compound-sense conflict: the task says "<unknown word> <t>" ("fraud
    # detection", "real-estate agent") while this skill only uses t in other
    # compounds ("object detection", "LLM agent") - likely a different sense.
    conflicts: dict[str, str] = {}
    for n, t in sorted(compounds):
        if n not in index.idf and t in profile.field_of and (n, t) not in profile.bigrams \
                and any(b == t for _, b in profile.bigrams):
            conflicts.setdefault(t, f"{n} {t}")
    for s in sorted(weights):
        f = profile.field_of.get(s)
        if f is None or f not in fields:
            continue
        factor = MODIFIER_CONFLICT_FACTOR if s in conflicts else 1.0
        score += index.idf_of(s) * FIELD_WEIGHTS[f] * weights[s] * factor
        by_field.setdefault(f, []).append(s)

    shared = sorted(b for b in bigrams & profile.bigrams if b[0] not in conflicts and b[1] not in conflicts)
    for a, b in shared:
        score += BIGRAM_WEIGHT * (index.idf_of(a) + index.idf_of(b)) / 2

    def coverage(stems: list[str]) -> float:
        total = sum(index.idf_of(s) for s in stems)
        got = sum(index.idf_of(s) * min(1.0, weights.get(s, 0.0)) for s in stems)
        return got / total if total else 0.0

    tag_hits = []
    for tag, stems in profile.tags:
        frac = coverage(stems)
        matched = sum(1 for s in set(stems) if s in weights)
        if stems and matched >= min(2, len(set(stems))) and frac >= PHRASE_MIN_FRACTION:
            score += CAPABILITY_TAG_WEIGHT * frac * (1 + 0.5 * (len(stems) > 1))
            tag_hits.append(tag)
    trig_hits = []
    for phrase, stems in profile.triggers:
        frac = coverage(stems)
        matched = sum(1 for s in set(stems) if s in weights)
        if len(set(stems)) >= 2 and matched >= 2 and frac >= PHRASE_MIN_FRACTION:
            score += TRIGGER_PHRASE_WEIGHT * frac
            trig_hits.append(phrase)

    if reasons is not None:
        if tag_hits:
            reasons.append(f"capability match: {', '.join(tag_hits)}")
        if trig_hits:
            reasons.append(f"trigger phrase match: {'; '.join(repr(t) for t in trig_hits)}")
        for f in ("name", "capability", "trigger", "description", "overview"):
            if by_field.get(f):
                reasons.append(f"{f} terms: {', '.join(by_field[f])}")
        if shared:
            reasons.append(f"phrase overlap: {', '.join(a + ' ' + b for a, b in shared)}")
        if conflicts:
            reasons.append(f"different compound sense: {', '.join(conflicts[t] for t in sorted(conflicts))}"
                           f" (x{MODIFIER_CONFLICT_FACTOR})")
    return score


def score_profile(profile: SkillProfile, index: SkillIndex, task: TaskAnalysis) -> RouteHit:
    reasons: list[str] = []
    score = _lexical(profile, index, task.weights, task.bigrams, reasons, compounds=task.compounds)
    if score > 0 and task.explicit_intent:
        if profile.type == task.intent:
            score *= 1 + INTENT_BONUS
            reasons.append(f"type matches classified intent ({task.intent})")
    if task.negated:
        neg = _lexical(profile, index, {s: 1.0 for s in task.negated}, set(), None, NEGATION_FIELDS)
        if neg > 0:
            score -= NEGATION_PENALTY * neg
            reasons.append(f"negated/already-done terms: {', '.join(sorted(task.negated & set(profile.field_of)))}"
                           f" (-{NEGATION_PENALTY * neg:.1f})")
    hits_not = sorted(set(task.weights) & profile.not_only)
    if hits_not:
        pen = NOT_FOR_PENALTY * sum(index.idf_of(s) * task.weights[s] for s in hits_not)
        score -= pen
        reasons.append(f"terms from its NOT-for clause: {', '.join(hits_not)} (-{pen:.1f})")
    return RouteHit(slug=profile.slug, score=round(score, 4), reasons=reasons, type=profile.type,
                    domain=profile.domain)


def score_skill(skill: sl.Skill, task_tokens: set[str] | str, intent: str = "workflow",
                index: SkillIndex | None = None) -> RouteHit:
    """Score one skill. `task_tokens` may be raw task text (preferred) or a
    token set from tokenize() (kept for backward compatibility). Without an
    `index`, IDF is computed over this one skill, so only relative comparisons
    between calls that share an index are meaningful."""
    index = index or build_index([skill])
    if isinstance(task_tokens, str):
        task = analyze_task(task_tokens)
    else:
        toks = sorted(task_tokens)
        task = TaskAnalysis(text=" ".join(toks), intent=intent, explicit_intent=True,
                            weights={t: 1.0 for t in toks}, negated=set(), bigrams=set(),
                            has_anchor=True)
    profile = next((p for p in index.profiles if p.slug == skill.slug), None) or _profile(skill)
    return score_profile(profile, index, task)


def expand_with_requires(skills_by_slug: dict[str, sl.Skill], ordered_slugs: list[str]) -> list[str]:
    """Prepend hard prerequisites (`requires`) that weren't already selected,
    preserving relative order and avoiding duplicates."""
    result: list[str] = []
    seen: set[str] = set()

    def visit(slug: str) -> None:
        if slug in seen or slug not in skills_by_slug:
            return
        for req in skills_by_slug[slug].frontmatter.get("requires") or []:
            visit(req)
        if slug not in seen:
            seen.add(slug)
            result.append(slug)

    for slug in ordered_slugs:
        visit(slug)
    return result


@dataclass
class RouteResult:
    intent: str
    hits: list[RouteHit]
    confidence: float             # of the top hit; 0.0 when nothing was selected
    threshold: float              # the absolute minimum score that applied to this task
    fallback_reason: str | None   # set when no skill was selected
    candidates: list[RouteHit]    # best-scoring skills before gating (for --json / debugging)


def _select(scored: list[RouteHit], index: SkillIndex, task: TaskAnalysis, threshold: float,
            relative_cutoff: float, top_k: int) -> list[RouteHit]:
    """Greedy selection. The top hit needs `threshold`. Each later hit needs
    `threshold` AND either `relative_cutoff` x the top score, or a *residual*
    score >= RESIDUAL_MIN on the task words that no already-selected skill
    covers - i.e. it must explain a different part of the request (the
    second half of "deploy it and then monitor it"), not just re-match the
    words the first skill already explained."""
    if not scored or scored[0].score < threshold or top_k < 1:
        return []
    profiles = {p.slug: p for p in index.profiles}
    chosen = [scored[0]]
    covered = {s for s in task.weights if profiles[scored[0].slug].field_of.get(s) in COVER_FIELDS}
    for h in scored[1:]:
        if len(chosen) >= top_k or h.score < threshold:
            break
        residual_w = {s: w for s, w in task.weights.items() if s not in covered}
        residual_bi = {b for b in task.bigrams if b[0] not in covered or b[1] not in covered}
        residual = _lexical(profiles[h.slug], index, residual_w, residual_bi, None, compounds=task.compounds)
        if h.score >= relative_cutoff * scored[0].score or residual >= RESIDUAL_MIN:
            chosen.append(h)
            if residual >= RESIDUAL_MIN and h.score < relative_cutoff * scored[0].score:
                h.reasons.append(f"covers other parts of the request (residual score {residual:.1f})")
            covered |= {s for s in task.weights if profiles[h.slug].field_of.get(s) in COVER_FIELDS}
    return chosen


def route_details(task_text: str, top_k: int = 5, min_score: float | None = None,
                  relative_cutoff: float | None = None) -> RouteResult:
    relative_cutoff = RELATIVE_CUTOFF if relative_cutoff is None else relative_cutoff
    skills = sl.load_all_skills()
    skills_by_slug = sl.by_slug(skills)
    index = _index_for(skills)
    task = analyze_task(task_text)

    base = MIN_SCORE if min_score is None else float(min_score)
    threshold = base if task.has_anchor else base * NO_ANCHOR_FACTOR
    scored = sorted((score_profile(p, index, task) for p in index.profiles), key=lambda h: (-h.score, h.slug))
    for h in scored:
        h.confidence = round(max(0.0, h.score) / (max(0.0, h.score) + threshold), 4) if threshold > 0 else 1.0
    candidates = [h for h in scored if h.score > 0][:max(top_k, 5)]

    top_score = scored[0].score if scored else 0.0
    top = _select(scored, index, task, threshold, relative_cutoff, top_k)

    if not top:
        if top_score <= 0:
            why = "no skill's metadata matched the task's terms"
        else:
            why = (f"best match {scored[0].slug} scored {top_score:.1f}, below the minimum {threshold:.1f}"
                   + ("" if task.has_anchor else " (raised: the task has no ML-domain words)"))
        return RouteResult(task.intent, [], 0.0, threshold, f"low confidence: {why}", candidates)

    expanded_order = expand_with_requires(skills_by_slug, [h.slug for h in top])
    hits_by_slug = {h.slug: h for h in top}
    final = [hits_by_slug.get(slug) or RouteHit(slug, 0.0, ["pulled in as a hard prerequisite"],
                                                 skills_by_slug[slug].frontmatter.get("type", ""),
                                                 skills_by_slug[slug].frontmatter.get("domain", ""))
             for slug in expanded_order]
    return RouteResult(task.intent, final, top[0].confidence, threshold, None, candidates)


def route(task_text: str, top_k: int = 5, min_score: float | None = None) -> tuple[str, list[RouteHit]]:
    """Backward-compatible interface: (intent, hits). Returns no hits when no
    skill clears the confidence bar; use route_details() for the reason."""
    res = route_details(task_text, top_k=top_k, min_score=min_score)
    return res.intent, res.hits


def _hit_json(h: RouteHit) -> dict:
    return {"slug": h.slug, "score": h.score, "confidence": h.confidence, "type": h.type, "domain": h.domain,
            "reasons": h.reasons}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", help="free-text description of what the user wants to do")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--min-score", type=float, default=None,
                    help=f"absolute minimum score (default {MIN_SCORE}; x{NO_ANCHOR_FACTOR} without ML words)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = route_details(args.task, top_k=args.top_k, min_score=args.min_score)

    if args.json:
        print(json.dumps({
            "task": args.task, "intent": res.intent, "confidence": res.confidence,
            "threshold": res.threshold, "fallback_reason": res.fallback_reason,
            "route": [_hit_json(h) for h in res.hits],
            "candidates": [_hit_json(h) for h in res.candidates],
        }, indent=2))
        return 0

    print(f"intent: {res.intent}")
    if not res.hits:
        print(f"no skill selected ({res.fallback_reason}) - this task may be outside the library, "
              "or need a skill that doesn't exist yet")
        return 0
    print(f"confidence: {res.confidence:.2f} (threshold {res.threshold:.1f})")
    for h in res.hits:
        print(f"  [{h.type or '?':9s}] {h.slug:28s} score={h.score:<6.1f} {'; '.join(h.reasons)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
