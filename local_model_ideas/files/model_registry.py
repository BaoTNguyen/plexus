"""
model_registry.py
Scans local model directories and reads metadata (size, native context,
architecture, backend type) without loading the model into memory.
"""
import os
import glob
import json

GGUF_MODELS_DIR = "/home/bao-tn/llama.cpp/models"
HF_MODELS_DIR = "/home/bao-tn/models/hf"  # vLLM-style model directories


def scan_gguf_models():
    """Read GGUF header metadata for every .gguf file found."""
    models = {}
    try:
        from gguf import GGUFReader
    except ImportError:
        print("gguf package not installed — run: pip install gguf --break-system-packages")
        return models

    for path in glob.glob(f"{GGUF_MODELS_DIR}/*.gguf"):
        try:
            reader = GGUFReader(path)
            arch = None
            n_ctx_train = None
            n_layer = None

            for key, field in reader.fields.items():
                if key == "general.architecture":
                    arch = str(field.parts[field.data[0]])
                if key.endswith("context_length"):
                    n_ctx_train = int(field.parts[field.data[0]][0])
                if key.endswith("block_count"):
                    n_layer = int(field.parts[field.data[0]][0])

            size_bytes = os.path.getsize(path)
            name = os.path.basename(path)
            models[name] = {
                "path": path,
                "backend": "llamacpp",
                "size_gb": round(size_bytes / (1024 ** 3), 2),
                "n_ctx_train": n_ctx_train or 32768,
                "n_layer": n_layer,
                "arch": arch or "unknown",
                "alias": os.path.splitext(name)[0],
            }
        except Exception as e:
            print(f"Skipping {path}: {e}")
    return models


def scan_hf_models():
    """Read config.json for every HF-style model directory (used by vLLM)."""
    models = {}
    if not os.path.isdir(HF_MODELS_DIR):
        return models

    for entry in os.scandir(HF_MODELS_DIR):
        cfg_path = os.path.join(entry.path, "config.json")
        if entry.is_dir() and os.path.exists(cfg_path):
            try:
                with open(cfg_path) as f:
                    cfg = json.load(f)
                # Rough size estimate: sum safetensors/bin file sizes on disk
                size_bytes = sum(
                    os.path.getsize(os.path.join(entry.path, f))
                    for f in os.listdir(entry.path)
                    if f.endswith((".safetensors", ".bin"))
                )
                models[entry.name] = {
                    "path": entry.path,
                    "backend": "vllm",
                    "size_gb": round(size_bytes / (1024 ** 3), 2),
                    "n_ctx_train": cfg.get("max_position_embeddings", 32768),
                    "n_layer": cfg.get("num_hidden_layers"),
                    "arch": cfg.get("architectures", ["unknown"])[0],
                    "alias": entry.name,
                }
            except Exception as e:
                print(f"Skipping {entry.path}: {e}")
    return models


def scan_models():
    """Combined registry of every locally available model, both backends."""
    models = scan_gguf_models()
    models.update(scan_hf_models())
    return models


if __name__ == "__main__":
    print(json.dumps(scan_models(), indent=2))
