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
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

NETWORK = os.environ.get("HEART_MODEL_NETWORK", "heart-egress")
PROXY = os.environ.get("HEART_EGRESS_CONTAINER", "egress")
PROXY_PORT = os.environ.get("HEART_SANDBOX_PROXY_PORT", "8888")
INJECT_PORT = os.environ.get("HEART_SANDBOX_INJECT_PORT", "8889")
IMAGE = os.environ.get("HEART_SANDBOX_IMAGE", "heart-agent:latest")
# The web lane: its own --internal network and its own proxy, because every
# container on a network reaches every port of every proxy on it -- a shared
# network would let an "api" task borrow the web lane's reach.
WEB_NETWORK = os.environ.get("HEART_WEB_NETWORK", "heart-web")
WEB_PROXY = f"{PROXY}-web"

# What each provider's CLI actually talks to. Narrow on purpose: Claude Code
# also reaches for mcp-proxy.anthropic.com and a Datadog intake, and the
# episodes pass without either -- measured, 23 denied telemetry connections in a
# run that scored a diff. Port-pinned: a bare name allows every port on the
# host, and api.anthropic.com:8443 went through before this.
_VENDOR_HOSTS = {
    "claude": ("api.anthropic.com:443",),
    "codex": ("chatgpt.com:443", "api.openai.com:443"),
}

# Quad9: answers NXDOMAIN for domains on its threat feeds, which is the
# malware filter for everything the proxies resolve. host.docker.internal is
# pinned in /etc/hosts because Docker Desktop resolves it through its own DNS,
# and pointing --dns elsewhere loses it -- measured, the local model went dark.
_PROXY_NET_ARGS = ("--dns", "9.9.9.9", "--dns", "149.112.112.112",
                   "--add-host", "host.docker.internal:host-gateway")


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
    # lazy: registry reaches heart through the ledger; sandbox stays importable
    # on a box that has not installed heart yet, which is what `doctor` reports
    from .registry import detect_subscriptions, injected_seats

    # an injected seat reaches its vendor through the injector, never CONNECT;
    # the proxy refuses the host anyway, and the list should say what is true
    injected = injected_seats()
    skip = ({"claude"} if "anthropic" in injected else set()) | (
        {"codex"} if "chatgpt" in injected else set())
    hosts = list(local_model_hosts())
    for provider in detect_subscriptions():
        if provider not in skip:
            hosts.extend(_VENDOR_HOSTS.get(provider, ()))
    # an API key is a seat's equivalent for reaching the same host
    if os.environ.get("HEART_SANDBOX_ENV"):
        named = {n.strip() for n in os.environ["HEART_SANDBOX_ENV"].split(",")}
        if "ANTHROPIC_API_KEY" in named:
            hosts.extend(_VENDOR_HOSTS["claude"])
        if "OPENAI_API_KEY" in named:
            hosts.extend(_VENDOR_HOSTS["codex"])
    return sorted(dict.fromkeys(hosts))


# OAuth token endpoints. A seat file mounted into a container carries a refresh
# token, and a refresh rotates it at the provider -- the host's copy dies and
# you are logged out of your own machine. The api lane never lists these; the
# web lane's `*` would pass them, so it names them. Nothing a sandboxed agent
# legitimately does needs to mint a token.
_REFRESH_HOSTS = ("auth.openai.com", "console.anthropic.com", "platform.claude.com")


def lanes() -> list[tuple[str, str, str, str]]:
    """(network, proxy container, ALLOW, DENY) for each egress lane.

    The api/model lane reaches exactly the allowlist. The web lane reaches any
    public host on 80/443 (`*` -- the proxy refuses private addresses, IP
    literals and anything Quad9 filters), plus the local model servers by name,
    since those are private and `*` alone would refuse them.
    """
    return [(NETWORK, PROXY, ",".join(allowlist()), ""),
            (WEB_NETWORK, WEB_PROXY, ",".join(["*", *local_model_hosts()]),
             ",".join(_REFRESH_HOSTS))]


def proxy_script() -> Path | None:
    """contrib/egress-proxy.py, from the heart checkout the registry names or
    from the installed package's own tree."""
    try:  # lazy: registry reaches heart through the ledger; sandbox must not
        from .registry import load_registry

        root = (load_registry() or {}).get("heart")
        if root and (p := Path(root) / "contrib" / "egress-proxy.py").is_file():
            return p
    except Exception:
        pass
    try:  # lazy: the installed-heart fallback; absence is a normal answer here
        import heart

        p = Path(heart.__file__).resolve().parents[2] / "contrib" / "egress-proxy.py"
        return p if p.is_file() else None
    except Exception:
        return None


def _running_config(proxy: str) -> dict | None:
    """The settings a proxy is running with -- ALLOW, INJECT_PORT, whether the
    seat secrets are mounted -- or None if it is not running. The whole config
    is compared, not ALLOW alone: a token saved after the proxy started needs
    the proxy restarted with the mount, even though its allowlist is current."""
    rc, out = _docker("inspect", proxy, "--format",
                      "{{.State.Running}}\t{{range .Mounts}}{{.Destination}} {{end}}"
                      "\t{{range .Config.Env}}{{println .}}{{end}}")
    if rc != 0:
        return None
    running, _, rest = out.partition("\t")
    if running.strip() != "true":
        return None
    mounts, _, env = rest.partition("\t")
    have = dict.fromkeys(("ALLOW", "DENY", "INJECT_PORT"), "")
    for line in env.splitlines():
        key, _, value = line.partition("=")
        if key in have:
            have[key] = value.strip()
    have["secrets"] = "/secrets" in mounts.split()
    have["codex"] = "/codex" in mounts.split()
    return have


def _wanted_config(allow: str, deny: str = "") -> dict:
    from .registry import injected_seats

    injected = injected_seats()
    # secrets whenever anything is injected: the sentinel seed lives there,
    # and without it the injector accepts nothing
    return {"ALLOW": allow, "DENY": deny, "INJECT_PORT": INJECT_PORT if injected else "",
            "secrets": bool(injected), "codex": "chatgpt" in injected}


def _start_proxy(proxy: str, network: str, want: dict, script: Path) -> str:
    """Run one lane's proxy: on bridge for the way out, then attached to the
    lane's --internal network as the only container there that routes."""
    from .registry import seat_secrets, sentinel_seed

    if want["INJECT_PORT"]:
        sentinel_seed()
    _docker("rm", "-f", proxy)
    args = ["run", "-d", "--name", proxy, "--restart", "unless-stopped",
            "--network", "bridge", *_PROXY_NET_ARGS,
            "-e", f"ALLOW={want['ALLOW']}", "-e", f"DENY={want['DENY']}",
            "-e", f"PORT={PROXY_PORT}",
            "-v", f"{script}:/proxy.py:ro"]
    if want["INJECT_PORT"]:
        args += ["-e", f"INJECT_PORT={want['INJECT_PORT']}"]
    # directories, not files: a token rewritten by rename would leave a
    # single-file bind pointing at the old inode, and the proxy reads the file
    # on every request precisely so a rotation needs no restart. ~/.codex whole
    # is the cost of that for Codex -- history included, visible to the proxy
    # and to nothing an agent runs.
    if want["secrets"]:
        args += ["-v", f"{seat_secrets()}:/secrets:ro"]
    if want["codex"]:
        args += ["-v", f"{Path.home() / '.codex'}:/codex:ro"]
    rc, out = _docker(*args, "--entrypoint", "python3", IMAGE, "/proxy.py")
    if rc != 0:
        return f"  start failed: {out}"
    rc, out = _docker("network", "connect", network, proxy)
    return "  started and attached" if rc == 0 else f"  attach failed: {out}"


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
    try:  # lazy: heart may be absent; reclaiming nothing is an acceptable answer
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

    try:  # lazy: whether heart imports at all is exactly what this line checks
        import heart  # noqa: F401

        say("heart: importable")
    except Exception:
        say("heart: NOT importable -- clone it to ../heart and `uv sync` (plexus dispatches through it)")

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


def seat_report() -> list[str]:
    """How each signed-in seat reaches a container, and the step that makes it
    better. Reported, never done: the token is the operator's to create and to
    type, and nothing here reads it."""
    import time

    from .registry import codex_claims, detect_subscriptions, injected_seats, seat_secrets

    seats, injected, where = detect_subscriptions(), injected_seats(), seat_secrets()
    out = []
    if "anthropic" in injected:
        out.append("seat claude: injected by the proxy -- containers hold a sentinel, not the token")
    elif "claude" in seats:
        # An editor, not `read -s` in a subshell: that one-liner reads as
        # nothing happening, and a paste that lands at the prompt instead goes
        # into shell history.
        out += ["seat claude: MOUNTED into containers (the real OAuth file). To inject it instead,",
                "  run `claude setup-token`, then save the token -- in your own terminal:",
                f"    install -d -m 700 {where}",
                f"    install -m 600 /dev/null {where}/anthropic",
                f"    nano {where}/anthropic      # paste, Ctrl-O, Enter, Ctrl-X",
                "  and `plexus doctor --fix` to restart the proxies with it"]
    if "chatgpt" in injected:
        days = (codex_claims().get("exp", 0) - time.time()) / 86400
        out.append("seat codex: injected by the proxy -- containers hold a sentinel, not the token")
        if days < 3:
            # the host CLI is the only refresher: a container's refresh is
            # refused so it cannot rotate the token out from under the host
            out.append(f"  its access token {'EXPIRED' if days <= 0 else f'expires in {days:.1f} days'}"
                       " -- run `codex` once on this machine to refresh it")
    elif "codex" in seats:
        out.append("seat codex: mounted into containers")
    return out


def doctor(fix: bool = False) -> list[str]:
    """Report the box's sandbox readiness; with fix=True, make it so.

    Read-only by default because "what is wrong" and "change my machine" are
    different requests, and the second one should be typed.
    """
    log: list[str] = toolchain(fix=fix)
    say = log.append

    if not shutil.which("docker"):
        say("docker: not installed -- no sandboxed run can start")
        return log

    for network, proxy, allow, deny in lanes():
        rc, out = _docker("network", "inspect", network, "--format", "{{.Internal}}")
        if rc != 0:
            say(f"network {network}: missing")
            if fix:
                rc, out = _docker("network", "create", "--internal", network)
                say(f"  created --internal" if rc == 0 else f"  create failed: {out}")
        elif out.strip() != "true":
            # not a nit: a routable network means the agent has the open internet
            # and the proxy it was pointed at is decoration
            say(f"network {network}: NOT --internal -- agents on it reach the open internet")
        else:
            say(f"network {network}: ok (--internal)")

        want = _wanted_config(allow, deny)
        have = _running_config(proxy)
        if have is None:
            say(f"proxy {proxy}: not running")
        elif have != want:
            say(f"proxy {proxy}: running with stale settings")
            say(f"  running: {have}")
            say(f"  wanted:  {want}")
        else:
            say(f"proxy {proxy}: ok ({allow or 'injector only'})")
        if fix and have != want:
            script = proxy_script()
            if not script:
                say("  cannot fix: contrib/egress-proxy.py not found (register the heart checkout)")
            elif not allow and not want["INJECT_PORT"]:
                say("  cannot fix: nothing to allow -- no local model and no seat detected")
            else:
                say(_start_proxy(proxy, network, want, script))

    for line in seat_report():
        say(line)

    try:  # lazy: heart may be absent; the except says so rather than guessing
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
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure() -> list[str]:
    """What a run does before a wave: fix what is fixable, say what is not."""
    return doctor(fix=True)
