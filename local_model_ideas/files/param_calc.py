"""
param_calc.py
Given a model's characteristics and current live GPU memory, pick a sensible
starting context size / parallel slot count / memory caps.

This is a STARTING TARGET, not an exact allocation. llama.cpp's own -fit
mechanism (active by default) will shrink the request further at load time
if it doesn't actually fit in free VRAM, so this function only needs to be
"reasonable," not perfectly precise. Real KV-cache cost varies significantly
by architecture (hybrid SSM/attention models are far cheaper than pure
transformers at the same context size) — treat this as a heuristic and
refine from observed startup logs, not as ground truth.
"""
import subprocess


def get_free_vram_mib():
    """Returns a list of free VRAM (MiB) per GPU, in device order."""
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"]
    ).decode()
    return [int(x.strip()) for x in out.strip().split("\n")]


def get_total_vram_mib():
    """Returns a list of total VRAM (MiB) per GPU, in device order."""
    out = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]
    ).decode()
    return [int(x.strip()) for x in out.strip().split("\n")]


def pick_params(model_info, num_gpus=2, target_parallel=3, safety_margin_gib=2):
    """
    Compute a starting --ctx-size / --parallel / --tensor-split configuration
    for llama.cpp, sized to the model and currently free VRAM.

    target_parallel: desired number of concurrent agent slots.
    safety_margin_gib: headroom to leave per GPU beyond the model+KV estimate,
        as a buffer against compute-buffer growth under real concurrent load
        (compute buffers scale with context size and batch activity, and can
        be meaningfully larger under real traffic than at idle startup).
    """
    free = get_free_vram_mib()
    usable_per_gpu = [f - (safety_margin_gib * 1024) for f in free[:num_gpus]]
    total_usable_mib = sum(usable_per_gpu)

    model_mib = model_info["size_gb"] * 1024
    remaining_for_kv = total_usable_mib - model_mib

    if remaining_for_kv <= 0:
        raise RuntimeError(
            f"Model {model_info['path']} ({model_info['size_gb']} GB) does not fit "
            f"in available VRAM ({total_usable_mib:.0f} MiB usable after safety margin)."
        )

    # Don't request more context than the model was trained on.
    max_ctx = model_info.get("n_ctx_train") or 32768

    # Starting target: enough total context for `target_parallel` agents at
    # the model's native length, capped at a sane ceiling. llama.cpp's -fit
    # will shrink this automatically if it doesn't fit; increasing
    # target_parallel or ctx_size beyond what's needed just gives -fit more
    # to trim, it won't cause a hard failure on its own.
    requested_ctx_total = min(max_ctx * target_parallel, 262144)

    tensor_split = ",".join(["1"] * num_gpus)

    return {
        "ctx_size": requested_ctx_total,
        "parallel": target_parallel,
        "tensor_split": tensor_split,
        "n_gpu_layers": 99,
        "num_gpus": num_gpus,
    }


def vllm_mem_util(free_mib, total_mib, safety_gib=2):
    """
    vLLM's --gpu-memory-utilization is a FRACTION OF TOTAL VRAM, claimed
    up front — unlike llama.cpp's incremental allocation. If other
    processes (embeddings servers, Xorg, etc.) already hold VRAM, compute
    the fraction relative to what's actually free, not total capacity, or
    vLLM will try to claim memory that's already in use.
    """
    target = free_mib - (safety_gib * 1024)
    return round(max(target, 0) / total_mib, 2)
