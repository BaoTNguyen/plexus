"""
agents.py
Fixed-role agent pattern: reasoning is assigned by agent identity, not
decided per-request. This is the simplest, most predictable pattern for a
small (2-4) fixed set of agents — decide once per role which agents reason,
rather than classifying each incoming task at runtime.
"""
import itertools
from llm_client import ask


class Agent:
    def __init__(self, name, reasoning, system_prompt, max_tokens=1000):
        self.name = name
        self.reasoning = reasoning
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens

    def handle(self, task: str):
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]
        result = ask(messages, reasoning=self.reasoning, max_tokens=self.max_tokens)
        if result["finish_reason"] != "stop":
            print(f"[{self.name}] WARNING: response truncated ({result['finish_reason']})")
        return result["content"]


# --- Fixed roster: 1 reasoning agent, 2 fast agents, matching the server's
# --parallel 3 slot count so all three can run concurrently without queuing.
planner = Agent(
    "Planner",
    reasoning=True,
    system_prompt="You break complex tasks into steps and reason carefully before answering.",
    max_tokens=8000,  # generous headroom for reasoning + final answer
)
executor_a = Agent(
    "ExecutorA",
    reasoning=False,
    system_prompt="You execute well-defined tasks quickly and directly.",
    max_tokens=1000,
)
executor_b = Agent(
    "ExecutorB",
    reasoning=False,
    system_prompt="You execute well-defined tasks quickly and directly.",
    max_tokens=1000,
)

_executor_cycle = itertools.cycle([executor_a, executor_b])


def dispatch(task: str, needs_planning: bool = False):
    """
    needs_planning should come from whatever labels tasks in your actual
    pipeline (a queue field, a workflow step type) whenever possible —
    that's more reliable than inferring it from task text. A cheap fallback
    heuristic is included below for free-form input with no existing label.
    """
    if needs_planning:
        return planner.handle(task)
    agent = next(_executor_cycle)
    return agent.handle(task)


def guess_needs_planning(task: str) -> bool:
    """Rough heuristic fallback only — prefer an explicit task-type label
    from your pipeline when one is available."""
    if len(task) > 400:
        return True
    signals = ["plan", "steps", "then", "after that", "compare", "analyze"]
    return any(s in task.lower() for s in signals)
