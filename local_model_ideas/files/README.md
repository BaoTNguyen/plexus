# Dynamic local model manager

Hot-swappable local LLM serving: discovers GGUF (llama.cpp) and HF
safetensors (vLLM) weights on disk, picks context size / parallel slots /
tensor-split from live VRAM and each model's own metadata, and exposes a
small HTTP control plane so agents always hit the same fixed port
regardless of which model is actually loaded underneath.

## Files

| File | Purpose |
|---|---|
| `model_registry.py` | Scans model directories, reads GGUF/HF metadata without loading weights |
| `param_calc.py` | Computes ctx-size / parallel / tensor-split from live `nvidia-smi` output + model size |
| `backends.py` | Builds the actual launch command for llama.cpp or vLLM |
| `model_manager.py` | Flask control plane: `/models`, `/load`, `/unload`, `/status` |
| `llm_client.py` | What agents import to call the model, with per-request reasoning toggle |
| `agents.py` | Example fixed-role agent pattern (1 reasoning agent + 2 fast agents) |
| `model-manager.service` | systemd unit for the manager process itself |

## Install

```bash
pip install gguf flask requests --break-system-packages
mkdir -p ~/llm-manager
cp *.py ~/llm-manager/
cp model-manager.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now model-manager.service
```

## Usage

```bash
# See what's available locally
curl http://127.0.0.1:9000/models

# Hot-swap to a specific model, 3 concurrent agent slots, 2 GPUs
curl -X POST http://127.0.0.1:9000/load \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen3.6-27B-Q4_K_M.gguf", "parallel": 3, "num_gpus": 2}'

# Check what's currently loaded
curl http://127.0.0.1:9000/status

# Unload without loading anything new
curl -X POST http://127.0.0.1:9000/unload
```

Agents always talk to `127.0.0.1:8001` (the fixed inference port) via
`llm_client.ask(...)` — they don't know or care which model is behind it.

## Memory caps on the model process itself

`model-manager.service` only caps the lightweight control-plane process.
The actual model process (llama-server / vLLM) is launched as a *child*
subprocess by the manager and does not inherit a systemd-managed memory
cgroup automatically. Two ways to cap it:

**Option A — wrap the launch in `systemd-run` from within `backends.py`**
so each model process gets its own transient scoped cgroup:
```python
cmd = ["systemd-run", "--user", "--scope",
       "-p", "MemoryMax=20G", "-p", "MemoryHigh=18G"] + build_llamacpp_cmd(...)
```

**Option B — set caps via a static systemd unit per model** if you're not
hot-swapping frequently and prefer the simpler original pattern from
earlier in this setup:
```ini
[Service]
MemoryHigh=18G
MemoryMax=20G
```
Size this to actual host RAM usage, not VRAM — with `--tensor-split` set
correctly, model weights and KV cache live on GPU, and host RAM usage for
the process itself is typically under 1-2 GB. A generous cap (4-6x
observed baseline) is a backstop against a misconfiguration silently
ballooning host RAM (as happened when `--tensor-split` was missing and a
27B model spilled ~27GB into system RAM), not a tight budget.

## Design notes / known limitations

- **Hot-swap = real reload, not in-place weight swap.** Neither llama.cpp
  nor vLLM support replacing weights without stopping and restarting the
  process. Expect normal load time (seconds for llama.cpp, up to ~90s for
  vLLM) as downtime during every swap.
- **`param_calc.py` produces a starting target, not a precise allocation.**
  llama.cpp's built-in `-fit` mechanism (on by default) shrinks the request
  further if it doesn't actually fit in free VRAM — KV-cache cost varies a
  lot by architecture (hybrid SSM/attention models are far cheaper per
  token of context than pure transformers), so trust `-fit`'s output over
  the heuristic, and refine the heuristic from observed startup logs.
- **vLLM's `--gpu-memory-utilization` claims a fraction of *total* VRAM
  up front**, unlike llama.cpp's incremental allocation — use
  `param_calc.vllm_mem_util()` to compute the fraction relative to
  currently-free VRAM (accounting for other processes already holding
  GPU memory), not total capacity, or vLLM will try to claim memory
  that's already in use.
- **Reasoning toggle is per-request, not per-model-load**, and only
  confirmed working via `chat_template_kwargs: {"enable_thinking": true}`
  against this llama.cpp build. vLLM's reasoning support varies by version
  and model family — verify the actual mechanism before assuming parity.
- **Always give reasoning-enabled calls a generous `max_tokens`** (4000+).
  A too-small budget can be entirely consumed by the reasoning trace,
  returning empty `content` with no error — silent truncation, not a
  clean failure. Check `finish_reason == "stop"` on every call.
- **Concurrent slot count (`--parallel`) divides `--ctx-size` evenly**
  across all slots in a single instance — there's no way to give one
  agent a bigger context window than another within the same server
  process. If genuinely asymmetric context is needed, run two separate
  instances on different ports instead of one shared instance.
