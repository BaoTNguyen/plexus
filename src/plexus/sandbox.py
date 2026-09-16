"""Fleet-level sandbox provisioning: the box, not the episode.

heart builds one container per episode and is right to -- the mount table, the
env, the network mode and the timeout are all facts about that episode. What it
has never done is create the things every episode assumes already exist: the
--internal network, the egress proxy, that proxy's allowlist, a current image,
and an absence of litter from the last run. Those were done by hand, once,
months ago, and the stack has been quietly depending on that ever since. A
sandbox that refuses at launch with a `docker run` line for a human to paste is
a setup step wearing an error message.

So the split, stated once: plexus owns what is true once per box per wave,
heart owns what is true per episode. Nothing here knows about a task.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

NETWORK = os.environ.get("HEART_MODEL_NETWORK", "heart-egress")
PROXY = os.environ.get("HEART_EGRESS_CONTAINER", "egress")
PROXY_PORT = os.environ.get("HEART_SANDBOX_PROXY_PORT", "8888")
IMAGE = os.environ.get("HEART_SANDBOX_IMAGE", "heart-agent:latest")

# What each provider's CLI actually talks to. Narrow on purpose: Claude Code
# also reaches for mcp-proxy.anthropic.com and a Datadog intake, and the
# episodes pass without either -- measured, 23 denied telemetry connections in a
# run that scored a diff.
_VENDOR_HOSTS = {
    "claude": ("api.anthropic.com",),
    "codex": ("chatgpt.com", "api.openai.com"),
}


def _docker(*args: str, timeout: int = 60) -> tuple[int, str]:
    if not shutil.which("docker"):
        return (127, "docker is not installed")
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return (124, f"timed out: docker {' '.join(args)}")
    return (r.returncode, (r.stdout + r.stderr).strip())


def local_model_hosts() -> list[str]:
    """`host.docker.internal:<port>` for every local endpoint in models.json.

    Ports, not a bare host alias. The alias is on the list so an agent can call
    the model server; bare, it also hands that agent the host's Postgres on 5432
    and heart's own server on 8000 -- a proxy defeating the reason it exists.
    """
    cfg_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    try:
        cfg = json.loads((cfg_home / "heart" / "models.json").read_text())
    except Exception:
        return []
    from urllib.parse import urlsplit

    ports = set()
    for profile in (cfg.get("profiles") or {}).values():
        url = (profile or {}).get("endpoint") or ""
        parts = urlsplit(url)
        if parts.hostname in ("127.0.0.1", "localhost", "::1", "0.0.0.0") and parts.port:
            ports.add(parts.port)
    return [f"host.docker.internal:{p}" for p in sorted(ports)]


def allowlist() -> list[str]:
    """Every host a sandboxed agent on this box may reach, and nothing else.

    Composed from what the machine can actually do: the local model servers
    named in models.json, plus the vendor endpoints for the seats that are
    signed in. A provider with no seat contributes no host -- the allowlist
    should not be wider than the credentials that could use it.
    """
    from .registry import detect_subscriptions

    hosts = list(local_model_hosts())
    for provider in detect_subscriptions():
        hosts.extend(_VENDOR_HOSTS.get(provider, ()))
    # an API key is a seat's equivalent for reaching the same host
    if os.environ.get("HEART_SANDBOX_ENV"):
        named = {n.strip() for n in os.environ["HEART_SANDBOX_ENV"].split(",")}
        if "ANTHROPIC_API_KEY" in named:
            hosts.extend(_VENDOR_HOSTS["claude"])
        if "OPENAI_API_KEY" in named:
            hosts.extend(_VENDOR_HOSTS["codex"])
    return sorted(dict.fromkeys(hosts))


def proxy_script() -> Path | None:
    """contrib/egress-proxy.py, from the heart checkout the registry names or
    from the installed package's own tree."""
    try:
        from .registry import load_registry

        root = (load_registry() or {}).get("heart")
        if root and (p := Path(root) / "contrib" / "egress-proxy.py").is_file():
            return p
    except Exception:
        pass
    try:
        import heart

        p = Path(heart.__file__).resolve().parents[2] / "contrib" / "egress-proxy.py"
        return p if p.is_file() else None
    except Exception:
        return None


def _running_allow() -> str | None:
    """The ALLOW the proxy is running with, or None if it is not running."""
    rc, out = _docker("inspect", PROXY, "--format",
                      "{{.State.Running}}\t{{range .Config.Env}}{{println .}}{{end}}")
    if rc != 0:
        return None
    running, _, env = out.partition("\t")
    if running.strip() != "true":
        return None
    for line in env.splitlines():
        if line.startswith("ALLOW="):
            return line[len("ALLOW="):].strip()
    return ""


def reap() -> tuple[int, int]:
    """Dead sandbox containers and the worktrees nothing owns.

    Both are the same kind of debt: a run that died between creating a resource
    and releasing it. 103 exited `heart-*` containers were on this box from one
    killed batch, each still pinning its worktree mount.
    """
    rc, out = _docker("ps", "-aq", "--filter", "name=heart-", "--filter", "status=exited")
    ids = out.split() if rc == 0 else []
    if ids:
        _docker("rm", *ids, timeout=120)
    try:
        from heart.env import reclaim

        trees = reclaim()
    except Exception:
        trees = 0
    return (len(ids), trees)


# What an installer's box needs before any of the below is even worth checking.
# Named here rather than in a README because a README does not fail a run.
_REQUIRED = {
    "git": "apt install git",
    "docker": "https://docs.docker.com/engine/install/ubuntu/",
    "uv": "curl -LsSf https://astral.sh/uv/install.sh | sh",
}
_AGENTS = ("claude", "codex", "gemini", "opencode")


def toolchain(root: str = ".", fix: bool = False) -> list[str]:
    """Is this checkout runnable at all? Reported before the box's sandbox.

    doctor() answers "can an episode run sandboxed here". This answers the
    question underneath it -- whether the person who just cloned plexus has
    the tools the sandbox itself assumes. Missing docker is not a degraded
    run, it is no run, and it should read differently from a stale allowlist.
    """
    import sys

    log: list[str] = []
    say = log.append
    r = Path(root)

    v = sys.version_info
    say(f"python {v.major}.{v.minor}: "
        + ("ok" if (v.major, v.minor) >= (3, 10) else "too old -- plexus needs >=3.10"))

    for tool, how in _REQUIRED.items():
        say(f"{tool}: ok" if shutil.which(tool) else f"{tool}: MISSING -- {how}")

    found = [a for a in _AGENTS if shutil.which(a)]
    say(f"agent cli: {', '.join(found)}" if found
        else "agent cli: none of " + "/".join(_AGENTS) + " on PATH -- nothing can execute a feature")

    try:
        import heart  # noqa: F401

        say("heart: importable")
    except Exception:
        say("heart: NOT importable -- `uv pip install -e ../heart` (plexus dispatches through it)")

    say("plexus.toml: ok" if (r / "plexus.toml").exists()
        else "plexus.toml: missing -- run `plexus init`")

    env, example = r / ".env", r / ".env.example"
    if env.exists():
        say(".env: ok")
    elif example.exists():
        say(".env: missing")
        if fix:
            env.write_text(example.read_text())
            say("  seeded from .env.example -- fill in the real values, it is gitignored")
    else:
        say(".env: absent (and no .env.example to seed from)")

    return log


def doctor(fix: bool = False) -> list[str]:
    """Report the box's sandbox readiness; with fix=True, make it so.

    Read-only by default because "what is wrong" and "change my machine" are
    different requests, and the second one should be typed.
    """
    log: list[str] = toolchain(fix=fix)
    say = log.append
    want = ",".join(allowlist())

    if not shutil.which("docker"):
        say("docker: not installed -- no sandboxed run can start")
        return log

    rc, out = _docker("network", "inspect", NETWORK, "--format", "{{.Internal}}")
    if rc != 0:
        say(f"network {NETWORK}: missing")
        if fix:
            rc, out = _docker("network", "create", "--internal", NETWORK)
            say(f"  created --internal" if rc == 0 else f"  create failed: {out}")
    elif out.strip() != "true":
        # not a nit: a routable network means the agent has the open internet and
        # the proxy it was pointed at is decoration
        say(f"network {NETWORK}: NOT --internal -- agents on it reach the open internet")
    else:
        say(f"network {NETWORK}: ok (--internal)")

    have = _running_allow()
    if have is None:
        say(f"proxy {PROXY}: not running")
    elif have != want:
        say(f"proxy {PROXY}: running with a stale allowlist")
        say(f"  running: {have or '(empty)'}")
        say(f"  wanted:  {want}")
    else:
        say(f"proxy {PROXY}: ok ({want})")
    if fix and have != want:
        script = proxy_script()
        if not script:
            say("  cannot fix: contrib/egress-proxy.py not found (register the heart checkout)")
        elif not want:
            say("  cannot fix: nothing to allow -- no local model and no seat detected")
        else:
            _docker("rm", "-f", PROXY)
            rc, out = _docker(
                "run", "-d", "--name", PROXY, "--restart", "unless-stopped",
                "--network", "bridge", "-e", f"ALLOW={want}", "-e", f"PORT={PROXY_PORT}",
                "-v", f"{script}:/proxy.py:ro", "--entrypoint", "python3", IMAGE, "/proxy.py")
            if rc != 0:
                say(f"  start failed: {out}")
            else:
                rc, out = _docker("network", "connect", NETWORK, PROXY)
                say("  started and attached" if rc == 0 else f"  attach failed: {out}")

    try:
        from heart.sandbox import image_is_stale

        stale = image_is_stale(IMAGE)
    except Exception as exc:  # heart not importable: say so rather than guess
        stale = f"cannot check image: {exc}"
    if stale:
        say(f"image {IMAGE}: {stale.splitlines()[0]}")
        if fix:
            say("  not rebuilt: `docker build` is slow and destructive of a working "
                "image -- run it yourself when you have read the reason")
    else:
        say(f"image {IMAGE}: ok")

    for host in local_model_hosts():
        _, port = host.rsplit(":", 1)
        say(f"model server :{port}: {'listening' if _listening(int(port)) else 'DOWN'}")

    if fix:
        containers, trees = reap()
        say(f"reaped: {containers} dead container(s), {trees} worktree(s)")
    return log


def _listening(port: int) -> bool:
    import socket

    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure() -> list[str]:
    """What a run does before a wave: fix what is fixable, say what is not."""
    return doctor(fix=True)
