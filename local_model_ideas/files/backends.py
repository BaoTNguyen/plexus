"""
backends.py
Translates a picked parameter set into the actual launch command for
whichever inference engine the model needs.

Key differences between backends this abstracts over:
                    llama.cpp                  vLLM
Format              GGUF (quantized)           HF safetensors (usually full/half precision)
Multi-GPU           --tensor-split             --tensor-parallel-size
Context             --ctx-size                 --max-model-len
Memory control      implicit via -fit          --gpu-memory-utilization (fraction)
Reasoning toggle    chat_template_kwargs        varies by version/model; needs per-version check
                    per-request
Startup cost        seconds                     30-90+ seconds (CUDA graph capture, kernel compile)
"""

LLAMACPP_BIN = "/home/bao-tn/llama.cpp/build/bin/llama-server"


def build_llamacpp_cmd(info, params, port):
    return [
        LLAMACPP_BIN,
        "-m", info["path"],
        "--port", str(port),
        "--host", "127.0.0.1",
        "-ngl", str(params.get("n_gpu_layers", 99)),
        "--tensor-split", params["tensor_split"],
        "--ctx-size", str(params["ctx_size"]),
        "--parallel", str(params["parallel"]),
        "--alias", info.get("alias", "model"),
        "--reasoning", "off",  # toggle per-request via chat_template_kwargs, not here
    ]


def build_vllm_cmd(info, params, port):
    return [
        "python3", "-m", "vllm.entrypoints.openai.api_server",
        "--model", info["path"],
        "--port", str(port),
        "--host", "127.0.0.1",
        "--tensor-parallel-size", str(params["num_gpus"]),
        "--max-model-len", str(params["ctx_size"]),
        "--gpu-memory-utilization", str(params.get("gpu_mem_util", 0.9)),
        "--served-model-name", info.get("alias", "model"),
    ]


BUILDERS = {
    "llamacpp": build_llamacpp_cmd,
    "vllm": build_vllm_cmd,
}

# Rough expected startup time before checking whether the process is alive.
# vLLM needs much longer due to CUDA graph capture / kernel compilation.
STARTUP_WAIT_SECONDS = {
    "llamacpp": 3,
    "vllm": 60,
}
