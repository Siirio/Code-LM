"""Local embedding engine — ONNX runtime, no PyTorch, no network calls.

Uses all-MiniLM-L6-v2 converted to ONNX and stored at
backend/models/all-MiniLM-L6-v2/  (generated once by
scripts/setup_embedding_model.py).

The model directory is bundled into the PyInstaller binary via backend.spec
datas, so the packaged Windows app works fully offline.
"""
import asyncio
import logging
import sys
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_DIM = 384

# ── Model location ────────────────────────────────────────────────────────────

def _model_dir() -> Path:
    """Resolve model directory for both dev and PyInstaller bundle."""
    if getattr(sys, "frozen", False):
        # Running inside PyInstaller bundle — files live under _MEIPASS
        return Path(sys._MEIPASS) / "models" / "all-MiniLM-L6-v2"
    return Path(__file__).parent / "models" / "all-MiniLM-L6-v2"


# ── Singletons ────────────────────────────────────────────────────────────────

_session = None      # onnxruntime.InferenceSession
_tokenizer = None    # tokenizers.Tokenizer


# ── Synchronous loader (runs in thread pool) ──────────────────────────────────

def _load_sync():
    """Load ONNX session and tokenizer from disk.  CPU-only, no GPU needed."""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    mdir = _model_dir()
    model_path = mdir / "model.onnx"
    tokenizer_path = mdir / "tokenizer.json"

    if not model_path.exists():
        raise FileNotFoundError(
            f"ONNX model not found at {model_path}. "
            "Run:  python backend/scripts/setup_embedding_model.py"
        )
    if not tokenizer_path.exists():
        raise FileNotFoundError(
            f"Tokenizer not found at {tokenizer_path}. "
            "Run:  python backend/scripts/setup_embedding_model.py"
        )

    session = ort.InferenceSession(
        str(model_path),
        providers=["CPUExecutionProvider"],
    )
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    # Fixed-length padding/truncation — matches training config
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=128)
    tokenizer.enable_truncation(max_length=128)

    return session, tokenizer


# ── Public async initialiser ──────────────────────────────────────────────────

async def ensure_model() -> None:
    """Load the model into memory (non-blocking, runs in thread pool).

    Call once at startup.  Subsequent calls return immediately if already loaded.
    Raises RuntimeError with a clear message if model files are missing.
    """
    global _session, _tokenizer
    if _session is not None and _tokenizer is not None:
        return  # Already loaded

    logger.info("Loading embedding model from %s …", _model_dir())
    _session, _tokenizer = await asyncio.to_thread(_load_sync)
    logger.info("Embedding model ready (dim=%d)", EMBEDDING_DIM)


# ── Public embedding function ─────────────────────────────────────────────────

def embed(text: str) -> list[float]:
    """Return a normalised 384-dim vector for *text*.

    Raises RuntimeError if ensure_model() has not been awaited yet.
    """
    if _session is None or _tokenizer is None:
        raise RuntimeError(
            "Could not load needed dependency: embedding model not initialised. "
            "Run scripts/setup_embedding_model.py to download the model files."
        )

    encoded = _tokenizer.encode(text)
    input_ids = np.array([encoded.ids], dtype=np.int64)
    attention_mask = np.array([encoded.attention_mask], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

    outputs = _session.run(None, {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "token_type_ids": token_type_ids,
    })

    token_embeddings = outputs[0]  # (1, seq_len, 384)
    mask = attention_mask[:, :, np.newaxis].astype(np.float32)
    sum_emb = (token_embeddings * mask).sum(axis=1)   # (1, 384)
    sum_mask = mask.sum(axis=1).clip(min=1e-9)        # (1, 1)
    pooled = sum_emb / sum_mask                        # mean pooling

    # L2 normalise
    norm = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
    vector = (pooled / norm)[0]

    return vector.tolist()


def is_ready() -> bool:
    return _session is not None and _tokenizer is not None
