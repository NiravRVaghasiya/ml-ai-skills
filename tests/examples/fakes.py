"""Deterministic, offline fakes shared by the example tests.

Each fake reproduces the *documented behaviour a skill relies on* — not the
quality of the real thing — so a SKILL.md block can be executed without
downloading a model or calling a paid API (see docs/EXAMPLE-TESTS.md):

* ``HashingEmbedder``  — ``sentence_transformers.SentenceTransformer`` stand-in:
  signed feature hashing of lowercase word tokens. Same text -> same vector on
  every run/platform (sha256, not Python's salted ``hash``); normalizes only
  when asked, so a skill that forgets ``normalize_embeddings=True`` is caught.
* ``FakeCrossEncoder`` — ``sentence_transformers.CrossEncoder`` stand-in:
  scores (query, passage) by token-overlap, deterministic.
* ``NumpyIndexFlatIP`` / ``NumpyIndexIDMap`` — FAISS semantics in numpy,
  including the parts that bite: ``search`` pads missing neighbours with id
  ``-1`` and score ``-3.4028235e+38`` (float32 lowest) when ``k > ntotal``,
  returns all ``-1`` on an empty index, and raises ``AssertionError`` on a
  dimension mismatch or ``k <= 0``. Observed against faiss-cpu 1.15.1.
  ``write_index``/``read_index`` round-trip both types (npz, no pickle).
* ``FakeAnthropicClient`` — records every ``messages.create`` call so tests
  can assert what reached the system prompt / tools channel.

Other test modules may import these read-only; keep them dependency-light
(numpy only) and deterministic.
"""
from __future__ import annotations

import hashlib
import re
import types
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Sequence

import numpy as np

FAISS_MISSING_ID = -1
FAISS_MISSING_SCORE = float(np.finfo(np.float32).min)  # -3.4028235e+38, what faiss-cpu pads IP results with

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _bucket_and_sign(token: str, dim: int) -> tuple[int, float]:
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little") % dim, (1.0 if digest[8] & 1 else -1.0)


# --------------------------------------------------------------------------- embedders


class HashingEmbedder:
    """Minimal ``SentenceTransformer`` look-alike (constructor + ``encode`` + dim)."""

    def __init__(self, model_name_or_path: str = "fake-hashing-embedder", *args: Any,
                 dim: int = 64, ignore_normalize: bool = False, **kwargs: Any):
        self.model_name = model_name_or_path
        self.init_kwargs = dict(kwargs)
        self.dim = dim
        self.ignore_normalize = ignore_normalize  # simulate a misconfigured wrapper
        self.encode_calls: list[dict[str, Any]] = []

    def get_embedding_dimension(self) -> int:  # sentence-transformers >= 6 name
        return self.dim

    def get_sentence_embedding_dimension(self) -> int:  # pre-6 name (deprecated alias in 6.x)
        return self.dim

    def _embed_one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float64)
        for tok in tokens(text):
            idx, sign = _bucket_and_sign(tok, self.dim)
            vec[idx] += sign
        if not vec.any():  # keep empty/punctuation-only text finite and non-zero
            vec[0] = 1.0
        return vec * 3.0  # deliberately NOT unit-norm unless normalization is requested

    def encode(self, sentences: str | Sequence[str], batch_size: int = 32, show_progress_bar: bool | None = None,
               convert_to_numpy: bool = True, normalize_embeddings: bool = False, **kwargs: Any):
        single = isinstance(sentences, str)
        items = [sentences] if single else list(sentences)
        self.encode_calls.append({"n": len(items), "batch_size": batch_size,
                                  "normalize_embeddings": normalize_embeddings})
        out = np.zeros((len(items), self.dim), dtype=np.float32)
        for start in range(0, len(items), max(1, int(batch_size))):  # honour batching like the real encoder
            for j, text in enumerate(items[start:start + batch_size], start=start):
                vec = self._embed_one(text)
                if normalize_embeddings and not self.ignore_normalize:
                    vec = vec / np.linalg.norm(vec)
                out[j] = vec.astype(np.float32)
        return out[0] if single else out


class FakeCrossEncoder:
    """Minimal ``CrossEncoder`` look-alike: score = |q ∩ p| / sqrt(|q|·|p|) over token sets."""

    def __init__(self, model_name: str = "fake-cross-encoder", *args: Any, **kwargs: Any):
        self.model_name = model_name
        self.predict_calls: list[int] = []

    def predict(self, sentences: Sequence[tuple[str, str]], batch_size: int = 32, **kwargs: Any) -> np.ndarray:
        pairs = list(sentences)
        self.predict_calls.append(len(pairs))
        scores = []
        for query, passage in pairs:
            q, p = set(tokens(query)), set(tokens(passage))
            scores.append(len(q & p) / np.sqrt(max(1, len(q)) * max(1, len(p))))
        return np.asarray(scores, dtype=np.float32)


# --------------------------------------------------------------------------- FAISS-semantics index


class NumpyIndexFlatIP:
    """Exact inner-product index with faiss-cpu's observable search semantics."""

    def __init__(self, d: int):
        self.d = int(d)
        self._xb = np.zeros((0, self.d), dtype=np.float32)

    @property
    def ntotal(self) -> int:
        return int(self._xb.shape[0])

    def add(self, x: np.ndarray) -> None:
        x = np.asarray(x, dtype=np.float32)
        n, d = x.shape
        assert d == self.d  # faiss's python wrapper asserts (bare AssertionError) on dim mismatch
        self._xb = np.vstack([self._xb, x])

    def search(self, x: np.ndarray, k: int):
        x = np.asarray(x, dtype=np.float32)
        n, d = x.shape
        assert d == self.d
        assert k > 0
        scores = np.full((n, k), FAISS_MISSING_SCORE, dtype=np.float32)
        ids = np.full((n, k), FAISS_MISSING_ID, dtype=np.int64)
        if self.ntotal:
            sims = x @ self._xb.T
            take = min(k, self.ntotal)
            for row in range(n):
                order = np.argsort(-sims[row], kind="stable")[:take]  # ties -> lower position first
                scores[row, :take] = sims[row, order]
                ids[row, :take] = order
        return scores, ids


class NumpyIndexIDMap:
    """``faiss.IndexIDMap`` look-alike: explicit int64 ids over a base index."""

    def __init__(self, index: NumpyIndexFlatIP):
        self.index = index
        self._ids = np.zeros(0, dtype=np.int64)

    @property
    def d(self) -> int:
        return self.index.d

    @property
    def ntotal(self) -> int:
        return self.index.ntotal

    def add(self, x):  # mirrors faiss: plain add is not allowed on an IDMap
        raise RuntimeError("add does not make sense with IndexIDMap, use add_with_ids")

    def add_with_ids(self, x: np.ndarray, ids: np.ndarray) -> None:
        ids = np.asarray(ids, dtype=np.int64)
        assert ids.shape[0] == np.asarray(x).shape[0]
        self.index.add(x)
        self._ids = np.concatenate([self._ids, ids])

    def search(self, x: np.ndarray, k: int):
        scores, pos = self.index.search(x, k)
        mapped = np.where(pos >= 0, self._ids[np.clip(pos, 0, None)] if self._ids.size else FAISS_MISSING_ID,
                          FAISS_MISSING_ID)
        return scores, mapped.astype(np.int64)


class PaddingIndex:
    """Wraps any index and keeps only its top ``keep`` hits, padding the rest FAISS-style.

    Simulates indexes (IVF with a small nprobe, HNSW, filtered search) that return
    fewer than ``k`` hits — padded with id -1 — even when ``k <= ntotal``.
    """

    def __init__(self, inner: Any, keep: int = 1):
        self.inner, self.keep = inner, keep

    @property
    def d(self) -> int:
        return self.inner.d

    @property
    def ntotal(self) -> int:
        return self.inner.ntotal

    def search(self, x, k):
        scores, ids = self.inner.search(x, k)
        scores, ids = scores.copy(), ids.copy()
        scores[:, self.keep:] = FAISS_MISSING_SCORE
        ids[:, self.keep:] = FAISS_MISSING_ID
        return scores, ids


# --------------------------------------------------------------------------- text splitter fallback


class SimpleRecursiveSplitter:
    """Fallback for ``langchain_text_splitters.RecursiveCharacterTextSplitter`` when it
    isn't installed: greedy merge on the first separator, then a hard character cap
    with overlap. NOT behaviour-equivalent to LangChain — only used to keep the
    numpy-only test path running without the optional dependency."""

    def __init__(self, chunk_size: int = 800, chunk_overlap: int = 0,
                 separators: Iterable[str] | None = None, **kwargs: Any):
        self.chunk_size, self.chunk_overlap = chunk_size, chunk_overlap
        self.separators = list(separators or ["\n\n", "\n", " ", ""])

    def split_text(self, text: str) -> list[str]:
        out: list[str] = []
        step = max(1, self.chunk_size - self.chunk_overlap)
        for para in (p.strip() for p in text.split(self.separators[0] or "\n\n")):
            if not para:
                continue
            for start in range(0, len(para), step):
                out.append(para[start:start + self.chunk_size])
                if start + self.chunk_size >= len(para):
                    break
        return out


# --------------------------------------------------------------------------- LLM client


class _FakeMessages:
    def __init__(self, owner: "FakeAnthropicClient"):
        self._owner = owner

    def create(self, **kwargs: Any):
        self._owner.calls.append(kwargs)
        text = self._owner.respond(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason=self._owner.stop_reason,
            model=kwargs.get("model"),
        )


def cite_first_document(kwargs: dict[str, Any]) -> str:
    """Default fake answer: cite S1 if the user turn contains a document, else abstain."""
    content = kwargs["messages"][-1]["content"]
    return "Annual plans can be refunded within 30 days [S1]." if 'id="S1"' in content else "I don't know."


class FakeAnthropicClient:
    def __init__(self, *args: Any, respond: Callable[[dict[str, Any]], str] = cite_first_document,
                 stop_reason: str = "end_turn", **kwargs: Any):
        self.init_kwargs = dict(kwargs)
        self.respond = respond
        self.stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []
        self.messages = _FakeMessages(self)


# --------------------------------------------------------------------------- module factories


def make_module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def fake_sentence_transformers(dim: int = 64) -> types.ModuleType:
    def _st(*args: Any, **kwargs: Any) -> HashingEmbedder:
        kwargs.setdefault("dim", dim)
        return HashingEmbedder(*args, **kwargs)
    return make_module("sentence_transformers", SentenceTransformer=_st, CrossEncoder=FakeCrossEncoder)


def write_index(index: NumpyIndexFlatIP | NumpyIndexIDMap, path: str) -> None:
    """``faiss.write_index`` stand-in (npz, no pickle); round-trips both fake index types."""
    flat = index.index if isinstance(index, NumpyIndexIDMap) else index
    ids = index._ids if isinstance(index, NumpyIndexIDMap) else np.zeros(0, dtype=np.int64)
    with open(path, "wb") as fh:  # a file object stops np.savez from appending ".npz"
        np.savez(fh, d=np.asarray(flat.d), xb=flat._xb, ids=ids, idmap=np.asarray(isinstance(index, NumpyIndexIDMap)))


def read_index(path: str) -> NumpyIndexFlatIP | NumpyIndexIDMap:
    with np.load(path, allow_pickle=False) as data:
        flat = NumpyIndexFlatIP(int(data["d"]))
        flat._xb = data["xb"].astype(np.float32)
        if not bool(data["idmap"]):
            return flat
        idmap = NumpyIndexIDMap(flat)
        idmap._ids = data["ids"].astype(np.int64)
        return idmap


def fake_faiss() -> types.ModuleType:
    return make_module("faiss", IndexFlatIP=NumpyIndexFlatIP, IndexIDMap=NumpyIndexIDMap,
                       write_index=write_index, read_index=read_index)


def fake_langchain_text_splitters() -> types.ModuleType:
    return make_module("langchain_text_splitters", RecursiveCharacterTextSplitter=SimpleRecursiveSplitter)


def fake_anthropic(created: list[FakeAnthropicClient] | None = None, **client_kwargs: Any) -> types.ModuleType:
    """Fake ``anthropic`` module; every constructed client is appended to ``created``."""
    created = created if created is not None else []

    def _client(*args: Any, **kwargs: Any) -> FakeAnthropicClient:
        client = FakeAnthropicClient(*args, **{**client_kwargs, **kwargs})
        created.append(client)
        return client
    return make_module("anthropic", Anthropic=_client)
