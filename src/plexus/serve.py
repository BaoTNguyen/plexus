"""Control plane: a local, single-user dashboard over the ledger.

Warren shape (plan / run / activity / answer cards) mapped onto plexus's real
surface, but built on plexus's stack, not warren's: stdlib http.server + one
HTML file, no bearer auth, no SQLite, no build step. State still lives in
`.plexus/*.jsonl` — this is a lens over the system of record plus three write
paths (approve, resolve, run/stop) that call the same code the CLI does.

The README rejected a web UI for the autonomous loop; this is the deliberate
reversal for the *supervision* surface — deciding which goal to advance,
answering blocks, and stopping a run — which a CLI genuinely does not cover.

Read side reuses observe/diagnose/plan verbatim. Write side:
  approve, resolve  -> synchronous (fast; approve spins a worktree, seconds)
  plan, run         -> spawned subprocess (minutes); tracked so run can be stopped
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import diagnose, ledger, observe
from .plan import load_plan
from .run import _feature_state
from .spec import load_spec

# root(resolved str) -> Popen we spawned, kept only so we can reap it (avoid
# zombies). Liveness and stop are decided from the flock, not this table, so a
# run started from a terminal is detected and stopped just the same.
_PROCS: dict[str, subprocess.Popen] = {}


def _reap() -> None:
    for k, p in list(_PROCS.items()):
        if p.poll() is not None:  # poll() reaps a finished child
            _PROCS.pop(k, None)


def _scan_roots(base: Path) -> list[Path]:
    """A goal repo is a dir with a plexus.toml. `base` may be one goal, or a
    parent holding several (one level deep) — the factory view `plexus stack`
    implies but never had a UI for."""
    base = base.resolve()
    roots = []
    if (base / "plexus.toml").exists():
        roots.append(base)
    roots += [p.parent for p in base.glob("*/plexus.toml")]
    return sorted(set(roots))


def _goal_id(root: Path) -> str:
    try:
        return load_spec(root).goal_id
    except Exception:
        recs = ledger.read(root)
        return recs[0]["goal_id"] if recs else root.name


def _running(root: Path) -> bool:
    """Authoritative: is a `plexus run` holding this goal's flock right now?
    Probe by trying the same non-blocking lock the run holds — if we get it,
    nobody's running (release immediately); if we're blocked, one is. Works for
    terminal-started runs too, and is immune to PID reuse (unlike reading the
    stamped pid), because the kernel drops the flock the instant the holder dies."""
    lock = Path(root) / ".plexus" / "lock"
    if not lock.exists():
        return False
    try:
        f = open(lock, "r")
    except OSError:
        return False
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        f.close()


def _open_escalations(recs: list[dict], goal_id: str) -> list[dict]:
    """Per feature, raised minus resolved > 0 -> open, carrying the latest
    reason. Mirrors observe.status's counting so the two never disagree."""
    by_feat: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("goal_id") == goal_id and r.get("feature_id"):
            by_feat.setdefault(r["feature_id"], []).append(r)
    out = []
    for fid, rs in by_feat.items():
        raised = [r for r in rs if r["kind"] == "escalation.raised"]
        if len(raised) - sum(r["kind"] == "escalation.resolved" for r in rs) > 0:
            last = raised[-1]
            out.append({"feature_id": fid,
                        "reason_class": last.get("reason_class", "?"),
                        "reason": last.get("reason", "")})
    return out


def _list_goals(roots: list[Path]) -> list[dict]:
    out = []
    for root in roots:
        lines, code = observe.status(str(root))
        out.append({"root": str(root), "goal_id": _goal_id(root),
                    "status": lines[0] if lines else "no records",
                    "code": code, "running": _running(root)})
    return out


def _goal_detail(root: Path) -> dict:
    """Everything the tabs render, from the ledger alone. Pure enough to test
    without HTTP."""
    root = Path(root)
    recs = ledger.read(root)
    goal_id = _goal_id(root)
    try:
        plan = load_plan(root)
    except SystemExit:
        plan = []
    approved = any(r["kind"] == "plan.approved" for r in recs)

    features = []
    for feat in plan:
        state, next_attempt, budget_used = _feature_state(recs, goal_id, feat["id"])
        features.append({"id": feat["id"], "title": feat["title"],
                         "acceptance": feat["acceptance"], "state": state,
                         "attempt": next_attempt - 1, "budget_used": budget_used})

    activity = [{"ts": r.get("ts", ""), "kind": r.get("kind", ""),
                 "feature_id": r.get("feature_id", ""),
                 "reason": r.get("reason", "") or r.get("outcome", "")}
                for r in recs[-40:]][::-1]

    return {"goal_id": goal_id, "root": str(root), "approved": approved,
            "running": _running(root),
            "status": " · ".join(observe.status(str(root))[0]),
            "insights": observe.insights(str(root)),
            "features": features,
            "escalations": _open_escalations(recs, goal_id),
            "activity": activity,
            "why": diagnose.why(str(root))}


def _spawn(root: Path, *args: str) -> None:
    """Run a plexus subcommand detached in its own session so Stop can kill the
    whole tree (heart's agent children included), not just the python parent."""
    key = str(root.resolve())
    _reap()
    if _running(root):
        return  # flock would reject a second run anyway; don't even try
    cmd = [sys.executable, "-m", "plexus.cli", *args, "--root", str(root)]
    _PROCS[key] = subprocess.Popen(cmd, start_new_session=True)


def _stop(root: Path) -> bool:
    """Stop whatever run holds the lock — ours or a terminal's — via the pid it
    stamped into the lock file. SIGTERM the process group so heart's agent
    children die with it. Between features this is clean (state is fsynced per
    record, resume-from-next-open-child picks it up); mid-episode leaves a
    worktree for heart to clean. ponytail: getpgid(pid) is the run's own group
    for both a Popen(start_new_session) child and a shell-foreground run."""
    if not _running(root):
        return False
    try:
        pid = int((Path(root) / ".plexus" / "lock").read_text().strip())
        # guard against PID reuse: the flock says *a* run holds the lock, but the
        # stamped pid could have been recycled by an unrelated process. Only
        # signal if the pid still looks like a plexus run. On platforms without
        # /proc we can't check — the flock gate above is the fallback.
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"plexus" not in cmdline:
                return False
        except OSError:
            pass
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, ValueError):
        return False  # empty/torn lock (truncate window) or already gone
    _reap()
    return True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet; this is a local tool
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _root(self, qs) -> Path:
        return Path(qs["root"][0])

    def _guard(self) -> bool:
        """Reject cross-origin / rebinding requests. This server runs commands,
        SIGTERMs process groups and approves plans, so a page the user happens to
        visit must not be able to drive it via localhost. Require a loopback Host
        (blocks DNS-rebinding through an attacker hostname) and, when a browser
        sends Origin, require it to be loopback too (blocks plain CSRF). curl and
        same-origin fetches (no cross-origin Origin) pass."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host and host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return False
        return True

    def _allowed_root(self, root: Path) -> bool:
        """A request may only act on a goal repo this server actually scanned —
        never an arbitrary path from the request body."""
        try:
            return root.resolve() in set(self.server.roots)
        except OSError:
            return False

    def do_GET(self):
        if not self._guard():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/":
            body = _HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/goals":
            self._json(_list_goals(self.server.roots))
        elif u.path == "/api/goal":
            root = self._root(qs)
            if not self._allowed_root(root):
                return self._json({"error": "unknown root"}, 403)
            self._json(_goal_detail(root))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return self._json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        root = Path(data["root"])
        if not self._allowed_root(root):
            return self._json({"error": "unknown root"}, 403)
        try:
            if u.path == "/api/plan":
                _spawn(root, "plan")
            elif u.path == "/api/approve":
                from .plan import approve
                approve(load_spec(root), root, waive=data.get("waive", False))
            elif u.path == "/api/run":
                _spawn(root, "run", "--candidates", str(data.get("candidates", 1)))
            elif u.path == "/api/stop":
                _stop(root)
            elif u.path == "/api/resolve":
                ledger.record("escalation.resolved", goal_id=_goal_id(root),
                              feature_id=data["feature"], root=root,
                              resolution=data.get("answer", "resolved"))
            else:
                return self._json({"error": "not found"}, 404)
        except SystemExit as e:  # approve rejects unusable criteria this way
            return self._json({"error": str(e)}, 400)
        self._json({"ok": True})


def serve(root: str = ".", port: int = 8100) -> int:
    roots = _scan_roots(Path(root))
    if not roots:
        print(f"no plexus.toml under {Path(root).resolve()} — run `plexus init` first")
        return 2
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.roots = roots
    print(f"plexus control plane: http://127.0.0.1:{port}  "
          f"({len(roots)} goal{'s' if len(roots) != 1 else ''})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


_HTML = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>plexus control plane</title><style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
header{padding:12px 18px;border-bottom:1px solid var(--line);display:flex;
gap:14px;align-items:center}header b{color:var(--accent)}
.wrap{display:flex;height:calc(100vh - 49px)}
.side{width:300px;border-right:1px solid var(--line);overflow:auto}
.g{padding:12px 16px;border-bottom:1px solid var(--line);cursor:pointer}
.g:hover{background:var(--panel)}.g.sel{background:var(--panel);
border-left:3px solid var(--accent)}
.g .id{font-weight:600}.g .st{color:var(--dim);font-size:12px;margin-top:3px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.c0{background:var(--ok)}.c1{background:var(--warn)}.c2{background:var(--bad)}
.main{flex:1;overflow:auto;padding:18px}
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--line)}
.tab{padding:8px 16px;cursor:pointer;color:var(--dim);border-bottom:2px solid transparent}
.tab.on{color:var(--fg);border-bottom-color:var(--accent)}
button{background:var(--panel);color:var(--fg);border:1px solid var(--line);
padding:6px 14px;border-radius:6px;cursor:pointer;font:inherit}
button:hover{border-color:var(--accent)}button.p{background:var(--accent);color:#0d1117;border:0}
button:disabled{opacity:.4;cursor:not-allowed}
.bar{display:flex;gap:8px;margin-bottom:16px;align-items:center}
.row{padding:10px 14px;border:1px solid var(--line);border-radius:6px;margin-bottom:8px;
background:var(--panel)}.row .t{font-weight:600}.row .m{color:var(--dim);font-size:12px;margin-top:4px}
.badge{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line)}
.landed{color:var(--ok);border-color:var(--ok)}.escalated{color:var(--bad);border-color:var(--bad)}
.open{color:var(--dim)}
pre{white-space:pre-wrap;color:var(--dim);margin:0}
.act{font-size:12px;padding:5px 0;border-bottom:1px solid var(--line);display:flex;gap:10px}
.act .k{color:var(--accent);min-width:150px}.act .ts{color:var(--dim)}
textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:8px;font:inherit;margin:6px 0}
.hint{color:var(--dim);padding:40px;text-align:center}
</style></head><body>
<header><b>plexus</b> control plane<span id=hdr style=color:var(--dim)></span></header>
<div class=wrap><div class=side id=side></div><div class=main id=main>
<div class=hint>select a goal</div></div></div>
<script>
let sel=null, tab='run';
const j=(u,o)=>fetch(u,o).then(r=>r.json());
const post=(p,b)=>j('/api'+p,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(b)});
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function goals(){
  const gs=await j('/api/goals');
  document.getElementById('side').innerHTML=gs.map(g=>
    `<div class="g${g.root===sel?' sel':''}" onclick="pick('${g.root}')">
     <div class=id><span class="dot c${g.code}"></span>${esc(g.goal_id)}
     ${g.running?'▶':''}</div><div class=st>${esc(g.status)}</div></div>`).join('')
    ||'<div class=hint>no goals</div>';
}
function pick(r){sel=r;detail();}
function setTab(t){tab=t;detail();}

async function detail(){
  if(!sel){return}
  const d=await j('/api/goal?root='+encodeURIComponent(sel));
  document.getElementById('hdr').textContent=' · '+d.goal_id+(d.running?' · running':'');
  const T=['run','plan','activity','escalations'];
  let h=`<div class=tabs>`+T.map(t=>`<div class="tab${t===tab?' on':''}"
    onclick="setTab('${t}')">${t}${t==='escalations'&&d.escalations.length?
    ' ('+d.escalations.length+')':''}</div>`).join('')+`</div>`;
  h+=`<div class=bar>`+
    (d.running?`<button class=p onclick="act('/stop')">■ Stop</button>`:
     d.approved?`<button class=p onclick="act('/run')">▶ Run</button>`:
     `<button onclick="act('/plan')">Plan</button>
      <button class=p onclick="act('/approve')">✓ Approve &amp; arm</button>`)+
    `<span style=color:var(--dim)>${esc(d.status)}</span></div>`;

  if(tab==='run') h+=d.features.map(f=>
    `<div class=row><div class=t>${esc(f.id)} · ${esc(f.title)}
     <span class="badge ${f.state}">${f.state}</span></div>
     <div class=m>acceptance: ${esc(f.acceptance)} · attempt ${f.attempt}</div></div>`
    ).join('')||'<div class=hint>no plan yet — hit Plan</div>';
  else if(tab==='plan') h+=`<div class=row><pre>`+
    d.features.map(f=>`${f.id}: ${esc(f.title)}\n    ${esc(f.acceptance)}`).join('\n')+
    `</pre></div>`+(d.approved?'':'<div class=hint>plan not armed — Approve to enable Run</div>');
  else if(tab==='activity') h+=d.activity.map(a=>
    `<div class=act><span class=ts>${esc(a.ts.slice(11,19))}</span>
     <span class=k>${esc(a.kind)}</span><span>${esc(a.feature_id)} ${esc(a.reason)}</span></div>`
    ).join('')+`<div class=row style=margin-top:16px><b>insights</b><pre>${
    esc(d.insights.join('\n'))}</pre></div>`;
  else h+=d.escalations.map(e=>
    `<div class=row><div class=t>${esc(e.feature_id)}
     <span class="badge escalated">${esc(e.reason_class)}</span></div>
     <div class=m>${esc(e.reason)}</div>
     <textarea id="a_${e.feature_id}" placeholder="answer / decision…"></textarea>
     <button onclick="resolve('${e.feature_id}')">Resolve &amp; resume</button></div>`
    ).join('')||'<div class=hint>no open escalations</div>';
  document.getElementById('main').innerHTML=h;
}
async function act(p){const r=await post(p,{root:sel});
  if(r.error)alert(r.error);goals();detail();}
async function resolve(f){const a=document.getElementById('a_'+f).value;
  await post('/resolve',{root:sel,feature:f,answer:a||'resolved'});detail();}

goals();detail();
setInterval(()=>{goals();if(sel)detail();},5000);
</script></body></html>"""


def demo() -> None:
    """Self-check: _goal_detail derives tab state from a hand-written ledger."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "plexus.toml").write_text(
            '[goal]\nid="g1"\ntext="t"\n[ground_truth]\nsuite="true"\n')
        (root / ".plexus").mkdir()
        recs = [
            {"kind": "plan.created", "goal_id": "g1", "ts": "2026-01-01T00:00:00+00:00",
             "features": [{"feature_id": "f1", "title": "one", "acceptance": "true"}]},
            {"kind": "plan.approved", "goal_id": "g1", "ts": "2026-01-01T00:01:00+00:00"},
            {"kind": "feature.landed", "goal_id": "g1", "feature_id": "f1",
             "attempt": 1, "ts": "2026-01-01T00:02:00+00:00"},
            {"kind": "escalation.raised", "goal_id": "g1", "feature_id": "f2",
             "reason_class": "blocked_on_decision", "reason": "which db?",
             "ts": "2026-01-01T00:03:00+00:00"},
        ]
        (root / ".plexus" / "plan.jsonl").write_text(
            json.dumps({"plan_id": "p1", "id": "f1", "title": "one",
                        "spec": "s", "acceptance": "true"}) + "\n")
        (root / ".plexus" / "ledger.jsonl").write_text(
            "\n".join(json.dumps(r) for r in recs) + "\n")

        det = _goal_detail(root)
        assert det["goal_id"] == "g1", det["goal_id"]
        assert det["approved"] is True
        assert det["features"][0]["state"] == "landed", det["features"]
        assert len(det["escalations"]) == 1, det["escalations"]
        assert det["escalations"][0]["feature_id"] == "f2"
        assert _scan_roots(root) == [root.resolve()]

        # flock probe: a held lock (any process) reads as running; released -> not.
        # Two open fds conflict under flock even within one process.
        lock = root / ".plexus" / "lock"
        lock.write_text("12345")
        assert _running(root) is False
        held = open(lock, "w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _running(root) is True
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
        assert _running(root) is False
    print("ok")


if __name__ == "__main__":
    demo()
