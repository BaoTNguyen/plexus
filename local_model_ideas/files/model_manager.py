"""
model_manager.py
A small HTTP control plane in front of llama-server / vLLM. Agents always
talk to the same fixed port; this service handles discovering local models,
picking parameters sized to live VRAM and the model's own characteristics,
launching the right backend, and swapping models on request.

Endpoints:
  GET  /models   -> list every locally discovered model and its metadata
  POST /load     -> {"model": "<name>"} start/replace the running model
  POST /unload   -> stop whatever is currently running
  GET  /status   -> what's currently loaded and whether it's healthy

Note: hot-swap means a real process stop + start, not an in-place weight
swap — llama.cpp/vLLM don't support replacing weights without a reload.
Expect the model's normal load time (seconds for llama.cpp, up to ~90s for
vLLM) as downtime during every swap.
"""
import subprocess
import signal
import time
import requests
from flask import Flask, request, jsonify

from model_registry import scan_models
from param_calc import pick_params
from backends import BUILDERS, STARTUP_WAIT_SECONDS

app = Flask(__name__)

current_proc = None
current_model = None
current_backend = None
PORT = 8001


def stop_current():
    global current_proc, current_model, current_backend
    if current_proc:
        current_proc.send_signal(signal.SIGTERM)
        try:
            current_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            current_proc.kill()
        current_proc = None
        current_model = None
        current_backend = None


def wait_until_ready(port, timeout):
    """Poll the OpenAI-compatible health/completions endpoint until it
    responds, rather than trusting a fixed sleep."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status_code == 200:
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(2)
    return False


def start_model(model_name, target_parallel=3, num_gpus=2):
    global current_proc, current_model, current_backend

    models = scan_models()
    if model_name not in models:
        raise ValueError(f"Unknown model: {model_name}. Available: {list(models)}")

    info = models[model_name]
    backend = info["backend"]

    params = pick_params(info, num_gpus=num_gpus, target_parallel=target_parallel)

    stop_current()

    cmd = BUILDERS[backend](info, params, PORT)
    proc = subprocess.Popen(cmd)
    current_proc = proc
    current_model = model_name
    current_backend = backend

    # Fail fast if the process dies immediately (bad args, OOM, etc.)
    time.sleep(3)
    if proc.poll() is not None:
        raise RuntimeError(f"{backend} exited immediately — check logs for {model_name}")

    ready = wait_until_ready(PORT, timeout=STARTUP_WAIT_SECONDS[backend] + 30)
    if not ready:
        raise RuntimeError(f"{backend} did not become ready within timeout")

    return {"backend": backend, "params": params}


@app.route("/models", methods=["GET"])
def list_models():
    return jsonify(scan_models())


@app.route("/load", methods=["POST"])
def load_model():
    body = request.json or {}
    model_name = body.get("model")
    target_parallel = body.get("parallel", 3)
    num_gpus = body.get("num_gpus", 2)
    if not model_name:
        return jsonify({"status": "error", "message": "missing 'model'"}), 400
    try:
        result = start_model(model_name, target_parallel=target_parallel, num_gpus=num_gpus)
        return jsonify({"status": "loaded", "model": model_name, **result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/unload", methods=["POST"])
def unload_model():
    stop_current()
    return jsonify({"status": "unloaded"})


@app.route("/status", methods=["GET"])
def status():
    return jsonify({
        "current_model": current_model,
        "backend": current_backend,
        "running": current_proc is not None and current_proc.poll() is None,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=9000)
