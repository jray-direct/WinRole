"""Local embeddings for skills the catalog does not know.

The model loads on first use. If fastembed is not installed, embed_phrases
returns None and scoring continues without similar-skill matches.
"""

from __future__ import annotations

import math
import threading
from typing import Any

MODEL_NAME = "BAAI/bge-small-en-v1.5"
SIMILAR_THRESHOLD = 0.78

_lock = threading.Lock()
_model: Any = None
_unavailable = False


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for x_val, y_val in zip(left, right):
        dot += x_val * y_val
        norm_left += x_val * x_val
        norm_right += y_val * y_val
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


def best_similar_pairs(
    job_phrases: list[str],
    resume_phrases: list[str],
    vectors: dict[str, list[float]],
    threshold: float = SIMILAR_THRESHOLD,
) -> list[tuple[str, str, float]]:
    """Best resume phrase for each unknown job phrase at or above the threshold."""
    pairs: list[tuple[str, str, float]] = []
    for job_phrase in job_phrases:
        job_vec = vectors.get(job_phrase)
        if not job_vec:
            continue
        best_sim = threshold
        best_resume = ""
        for resume_phrase in resume_phrases:
            if resume_phrase.casefold() == job_phrase.casefold():
                continue
            resume_vec = vectors.get(resume_phrase)
            if not resume_vec:
                continue
            sim = cosine(job_vec, resume_vec)
            if sim >= best_sim:
                best_sim = sim
                best_resume = resume_phrase
        if best_resume:
            pairs.append((job_phrase, best_resume, round(best_sim, 3)))
        if len(pairs) >= 8:
            break
    return pairs


def embed_phrases(phrases: list[str]) -> dict[str, list[float]] | None:
    """Vectors for these phrases. None means the local model cannot run."""
    global _model, _unavailable
    cleaned = [phrase for phrase in phrases if phrase]
    if not cleaned:
        return {}
    if _unavailable:
        return None
    with _lock:
        if _unavailable:
            return None
        if _model is None:
            try:
                from fastembed import TextEmbedding

                _model = TextEmbedding(model_name=MODEL_NAME)
            except Exception:
                _unavailable = True
                return None
        try:
            vectors = list(_model.embed(cleaned))
        except Exception:
            return None
    out: dict[str, list[float]] = {}
    for phrase, vec in zip(cleaned, vectors):
        try:
            out[phrase] = [float(x) for x in vec]
        except TypeError:
            continue
    return out
