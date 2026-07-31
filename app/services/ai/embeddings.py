"""
Embedding service — singleton model, warm on startup.
"""
import logging
import asyncio
from typing import Optional, List
from sentence_transformers import SentenceTransformer

import threading

logger = logging.getLogger(__name__)

MODEL_NAME = "paraphrase-multilingual-mpnet-base-v2"
_model: Optional[SentenceTransformer] = None
_model_lock = threading.Lock()  # protects concurrent first-load across threads


def get_model() -> SentenceTransformer:
    global _model
    # Fast path — already loaded, no lock needed
    if _model is not None:
        return _model
    # Slow path — only ONE thread should actually load the model.
    # Concurrent callers block here instead of each starting their
    # own load (which was causing OOM crashes under concurrent uploads).
    with _model_lock:
        if _model is None:
            logger.info(f"Loading embedding model {MODEL_NAME}...")
            _model = SentenceTransformer(MODEL_NAME)
            logger.info("Embedding model loaded ✅")
    return _model


async def generate_embedding(text: str) -> Optional[List[float]]:
    """Generate embedding — runs in executor to avoid blocking."""
    if not text or not text.strip():
        return None
    try:
        loop = asyncio.get_event_loop()
        vector = await loop.run_in_executor(
            None,
            lambda: get_model().encode(text[:512], convert_to_numpy=True).tolist()
        )
        return vector
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        return None


async def generate_embeddings_batch(texts: List[str]) -> List[Optional[List[float]]]:
    """Batch embedding generation."""
    if not texts:
        return []
    try:
        loop = asyncio.get_event_loop()
        clean_texts = [t[:512] if t else "" for t in texts]
        vectors = await loop.run_in_executor(
            None,
            lambda: get_model().encode(clean_texts, convert_to_numpy=True).tolist()
        )
        return vectors
    except Exception as e:
        logger.error(f"Batch embedding failed: {e}")
        return [None] * len(texts)
