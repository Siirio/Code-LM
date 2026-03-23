"""One-time setup: download and convert all-MiniLM-L6-v2 to ONNX.

Run once per developer machine and once in CI before the PyInstaller build:

    python backend/scripts/setup_embedding_model.py

Requires:  pip install optimum[onnxruntime] sentence-transformers
At runtime the app only needs:  onnxruntime  tokenizers  numpy
(PyTorch / optimum / sentence-transformers are NOT bundled.)

Output: backend/models/all-MiniLM-L6-v2/
    model.onnx          — ONNX weights (~23 MB, quantised)
    tokenizer.json      — HuggingFace fast tokenizer
    tokenizer_config.json
    special_tokens_map.json
    vocab.txt
    config.json
"""
import sys
import shutil
from pathlib import Path

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
OUT_DIR = Path(__file__).parent.parent / "models" / "all-MiniLM-L6-v2"


def main():
    print(f"[setup] Exporting {MODEL_ID} → ONNX …")
    print(f"[setup] Output: {OUT_DIR}")

    # Validate dependencies early with clear messages
    try:
        from optimum.onnxruntime import ORTModelForFeatureExtraction
    except ImportError:
        sys.exit(
            "\n[setup] ERROR: optimum not installed.\n"
            "Run:  pip install 'optimum[onnxruntime]' sentence-transformers\n"
        )

    try:
        from transformers import AutoTokenizer
    except ImportError:
        sys.exit(
            "\n[setup] ERROR: transformers not installed.\n"
            "Run:  pip install transformers\n"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Export model to ONNX (downloads from HuggingFace if not cached)
    print("[setup] Downloading / converting model (this takes ~1 min on first run) …")
    model = ORTModelForFeatureExtraction.from_pretrained(
        MODEL_ID,
        export=True,
    )
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)

    # Save ONNX model + tokenizer files to OUT_DIR
    model.save_pretrained(str(OUT_DIR))
    tokenizer.save_pretrained(str(OUT_DIR))

    # Rename the exported model file to the expected name if needed
    # optimum may save it as model.onnx or ort_model.onnx depending on version
    for candidate in ("ort_model.onnx", "encoder_model.onnx"):
        src = OUT_DIR / candidate
        if src.exists() and not (OUT_DIR / "model.onnx").exists():
            src.rename(OUT_DIR / "model.onnx")
            print(f"[setup] Renamed {candidate} → model.onnx")
            break

    model_path = OUT_DIR / "model.onnx"
    if not model_path.exists():
        sys.exit(
            f"\n[setup] ERROR: model.onnx not found after export in {OUT_DIR}.\n"
            "Files present: " + ", ".join(p.name for p in OUT_DIR.iterdir())
        )

    size_mb = model_path.stat().st_size / 1_048_576
    print(f"[setup] Done — model.onnx ({size_mb:.1f} MB)")
    print(f"[setup] Tokenizer files saved to {OUT_DIR}")
    print("[setup] Embedding model is ready for use.")


if __name__ == "__main__":
    main()
