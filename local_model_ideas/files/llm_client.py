"""
llm_client.py
Thin client agents import to talk to whatever model is currently loaded
behind the manager, on the fixed inference port. Reasoning is a per-call
choice, not a server-wide setting — the server itself stays launched with
--reasoning off; passing reasoning=True here adds the chat_template_kwargs
override for that one request only.

IMPORTANT re: max_tokens when reasoning=True — reasoning traces can run
from a few hundred to several thousand tokens depending on task difficulty
(observed variance in testing: ~1,100-8,900 characters on trivial prompts
alone). A too-small max_tokens can silently truncate the reasoning trace
with ZERO final answer returned. This wrapper enforces a floor of 4000
tokens whenever reasoning is enabled; raise it further for agents doing
genuinely hard, open-ended tasks.
"""
import requests

BASE_URL = "http://127.0.0.1:8001/v1/chat/completions"


def ask(messages, reasoning=False, max_tokens=500, timeout=180):
    payload = {
        "model": "current",  # server ignores this if only one model is loaded
        "messages": messages,
        "max_tokens": max_tokens if not reasoning else max(max_tokens, 4000),
    }
    if reasoning:
        payload["chat_template_kwargs"] = {"enable_thinking": True}

    resp = requests.post(BASE_URL, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()

    choice = data["choices"][0]
    msg = choice["message"]
    finish_reason = choice["finish_reason"]

    if finish_reason != "stop":
        # The request hit max_tokens before finishing — for a reasoning call
        # this can mean the final answer was truncated entirely. Callers
        # should check this and retry with a higher max_tokens if needed.
        print(f"WARNING: response did not finish cleanly (finish_reason={finish_reason})")

    return {
        "content": msg.get("content", ""),
        "reasoning": msg.get("reasoning_content", ""),
        "finish_reason": finish_reason,
        "completion_tokens": data["usage"]["completion_tokens"],
        "prompt_tokens": data["usage"]["prompt_tokens"],
    }
