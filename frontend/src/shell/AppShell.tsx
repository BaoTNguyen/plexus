import * as Dialog from "@radix-ui/react-dialog";
import * as Tooltip from "@radix-ui/react-tooltip";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  Bell,
  ChevronLeft,
  ChevronRight,
  FolderPlus,
  Gauge,
  Play,
  Search,
  Settings,
  Star,
  Tag,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
import { lastTab } from "../lastTab";
import type { Fleet, Goal } from "../types";

function SettingsDialog({ fleet }: { fleet?: Fleet }) {
  const client = useQueryClient();
  const [path, setPath] = useState("");
  // Cost model lives here rather than on the dashboard: it is configuration you
  // touch when a plan or a rate card changes, not a reading. Costs arrive
  // pre-filled from the signed-in plan (registry.detect_subscriptions), so the
  // usual number of fields to type is zero.
  const dashboard = useQuery({ queryKey: ["dashboard"], queryFn: () => api.dashboard() });
  const [subscriptions, setSubscriptions] = useState({ claude: 0, codex: 0 });
  const [pricing, setPricing] = useState({
    claude: { input: 0, output: 0 }, codex: { input: 0, output: 0 },
  });
  const [costDirty, setCostDirty] = useState(false);
  useEffect(() => {
    if (dashboard.data && !costDirty) {
      setSubscriptions(dashboard.data.cost.subscriptions);
      setPricing(dashboard.data.cost.pricing);
    }
  }, [dashboard.data, costDirty]);
  const saveAccounting = useMutation({
    mutationFn: () => api.saveAccounting({ subscriptions, pricing }),
    onSuccess: () => {
      setCostDirty(false);
      client.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  const detected = dashboard.data?.cost.detected_subscriptions ?? {};
  const updateFleet = useMutation({
    mutationFn: (values: Partial<Fleet>) => api.post<Fleet>("/fleet", values),
    onSuccess: (data) => client.setQueryData(["fleet"], data),
  });
  const addProject = useMutation({
    mutationFn: () => api.post("/add", { path }),
    onSuccess: () => {
      setPath("");
      client.invalidateQueries({ queryKey: ["goals"] });
    },
  });
  return (
    <Dialog.Root>
      <Dialog.Trigger asChild>
        <button className="icon-button" aria-label="Fleet settings">
          <Settings size={16} />
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog-overlay" />
        <Dialog.Content className="dialog-content">
          <Dialog.Title>Fleet settings</Dialog.Title>
          <Dialog.Description>
            Limits apply to runs launched from this control plane.
          </Dialog.Description>
          <div className="settings-grid">
            {([
              ["local_slots", "Local model slots"],
              ["global_agents", "Global agents"],
              ["max_goals", "Concurrent goals"],
            ] as const).map(([key, label]) => (
              <label key={key}>
                <span>{label}</span>
                <input
                  type="number"
                  min={0}
                  defaultValue={fleet?.[key] ?? 0}
                  onChange={(event) =>
                    updateFleet.mutate({ [key]: Number(event.target.value) || 0 })
                  }
                />
              </label>
            ))}
          </div>
          <form
            className="add-project"
            onSubmit={(event) => {
              event.preventDefault();
              if (path) addProject.mutate();
            }}
          >
            <label htmlFor="project-path">Add folder to workspace</label>
            <div>
              <input
                id="project-path"
                value={path}
                onChange={(event) => setPath(event.target.value)}
                placeholder="/path/to/project"
              />
              <button className="button" disabled={!path || addProject.isPending}>
                <FolderPlus size={15} /> Add
              </button>
            </div>
          </form>
          <div className="settings-section">
            <h3>Cost model <small>USD · per month</small></h3>
            <p className="settings-hint">
              {Object.keys(detected).length
                ? `Detected from your signed-in plans (${Object.entries(detected)
                    .map(([p, v]) => `${p} $${v}/mo`).join(", ")}). Override only for a negotiated rate.`
                : "No plan detected — sign in to Claude Code or Codex, or enter costs by hand."}
            </p>
            <div className="accounting-grid">
              <label>
                <span>Claude subscription</span>
                <input type="number" min={0} step="0.01" value={subscriptions.claude}
                  onChange={(event) => { setSubscriptions({ ...subscriptions, claude: Number(event.target.value) }); setCostDirty(true); }} />
              </label>
              <label>
                <span>Codex subscription</span>
                <input type="number" min={0} step="0.01" value={subscriptions.codex}
                  onChange={(event) => { setSubscriptions({ ...subscriptions, codex: Number(event.target.value) }); setCostDirty(true); }} />
              </label>
              <button className="button button-primary" disabled={!costDirty || saveAccounting.isPending} onClick={() => saveAccounting.mutate()}>Save costs</button>
              <label><span>Claude input / MTok</span><input type="number" min={0} step="0.01" value={pricing.claude.input} onChange={(event) => { setPricing({ ...pricing, claude: { ...pricing.claude, input: Number(event.target.value) } }); setCostDirty(true); }} /></label>
              <label><span>Claude output / MTok</span><input type="number" min={0} step="0.01" value={pricing.claude.output} onChange={(event) => { setPricing({ ...pricing, claude: { ...pricing.claude, output: Number(event.target.value) } }); setCostDirty(true); }} /></label>
              <span />
              <label><span>Codex input / MTok</span><input type="number" min={0} step="0.01" value={pricing.codex.input} onChange={(event) => { setPricing({ ...pricing, codex: { ...pricing.codex, input: Number(event.target.value) } }); setCostDirty(true); }} /></label>
              <label><span>Codex output / MTok</span><input type="number" min={0} step="0.01" value={pricing.codex.output} onChange={(event) => { setPricing({ ...pricing, codex: { ...pricing.codex, output: Number(event.target.value) } }); setCostDirty(true); }} /></label>
            </div>
          </div>
          {(updateFleet.error || addProject.error || saveAccounting.error) && (
            <p className="error">
              {(updateFleet.error || addProject.error || saveAccounting.error)?.message}
            </p>
          )}
          <Dialog.Close asChild>
            <button className="dialog-close" aria-label="Close settings">
              <X size={16} />
            </button>
          </Dialog.Close>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function ProjectRow({ goal, collapsed }: { goal: Goal; collapsed: boolean }) {
  const client = useQueryClient();
  const [editingLabel, setEditingLabel] = useState(false);
  const [label, setLabel] = useState(goal.label);
  type ProjectFields = { pinned?: boolean | null; label?: string };
  const update = useMutation({
    mutationFn: (fields: ProjectFields) =>
      api.post("/project", { root: goal.root, ...fields }),
    onMutate: async (fields) => {
      await client.cancelQueries({ queryKey: ["goals"] });
      const previous = client.getQueryData<Goal[]>(["goals"]);
      client.setQueryData<Goal[]>(["goals"], (current = []) =>
        current.map((item) =>
          item.project_id === goal.project_id
            ? {
                ...item,
                ...(fields.label !== undefined ? { label: fields.label } : {}),
                ...(fields.pinned !== undefined ? { pinned: Boolean(fields.pinned) } : {}),
              }
            : item,
        ),
      );
      return { previous };
    },
    onError: (_error, _fields, context) => {
      if (context?.previous) client.setQueryData(["goals"], context.previous);
    },
    onSettled: () => client.invalidateQueries({ queryKey: ["goals"] }),
  });
  const remove = useMutation({
    mutationFn: () => api.post("/remove", { path: goal.root }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["goals"] }),
  });
  const stateClass = goal.running ? "running" : goal.code === 0 ? "ok" : goal.code === 2 ? "warn" : "bad";
  return (
    <div className="project-item">
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <Link
            to="/p/$projectId/$tab"
            // where you left this project, not a fixed tab — and "goal" has
            // not been a tab since the four-tab rework, so every click here
            // used to fall through to the overview
            params={{ projectId: goal.project_id, tab: lastTab(goal.project_id) }}
            className="project-row"
            activeProps={{ className: "project-row active" }}
          >
            <span className={`project-dot dot-${stateClass}`} />
            {!collapsed && (
              <span className="project-copy">
                <strong>{goal.name}</strong>
                <small>{goal.status}</small>
              </span>
            )}
            {goal.running && <Play className="project-running" size={12} fill="currentColor" />}
          </Link>
        </Tooltip.Trigger>
        {collapsed && (
          <Tooltip.Portal>
            <Tooltip.Content className="tooltip" side="right">
              {goal.name} · {goal.status}
            </Tooltip.Content>
          </Tooltip.Portal>
        )}
      </Tooltip.Root>
      {!collapsed && (
        <div className="project-tools">
          <button
            type="button"
            aria-label={goal.pinned ? `Unpin ${goal.name}` : `Pin ${goal.name}`}
            onClick={() => update.mutate({ pinned: goal.pinned ? null : true })}
          >
            <Star size={12} fill={goal.pinned ? "currentColor" : "none"} />
          </button>
          <button
            type="button"
            aria-label={`Set group for ${goal.name}`}
            onClick={() => setEditingLabel((value) => !value)}
          >
            <Tag size={12} />
          </button>
          <button
            type="button"
            aria-label={`Remove ${goal.name} from workspace`}
            onClick={() => {
              if (window.confirm(`Remove ${goal.name} from the workspace? This only unregisters it — nothing on disk is deleted.`)) {
                remove.mutate();
              }
            }}
          >
            <Trash2 size={12} />
          </button>
        </div>
      )}
      {editingLabel && (
        <form
          className="project-label-form"
          onSubmit={(event) => {
            event.preventDefault();
            update.mutate({ label });
            setEditingLabel(false);
          }}
        >
          <input
            autoFocus
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="scope label — shared labels group repos into one scope"
          />
        </form>
      )}
    </div>
  );
}

type ScopeEntry = { key: string; title: string; goals: Goal[]; kind: "pinned" | "label" | "auto" };

/** The episode-feed filter for a scope: an explicit label first (repos tagged
 * together by hand), else the auto-derived scope_id (repos the registered-root
 * scan grouped on its own) — mirrors the priority `scopes` groups them by.
 * null for a scope with nothing to merge (pinned, or a lone repo — that one
 * just reads its own episodes directly). */
function scopeQuery(entry: ScopeEntry): { label?: string; scope_id?: string } | null {
  if (entry.kind === "label") return { label: entry.title };
  if (entry.kind === "auto" && entry.goals.length > 1) return { scope_id: entry.goals[0].scope_id };
  return null;
}

function scopeStatus(goals: Goal[]): "running" | "bad" | "warn" | "ok" {
  if (goals.some((goal) => goal.running)) return "running";
  if (goals.some((goal) => goal.code === 1)) return "bad";
  if (goals.some((goal) => goal.code === 2)) return "warn";
  return "ok";
}

/** The right-hand column a scope click opens: either its repos (2+ only,
 * with full pin/tag/remove controls) or its recent episodes, merged across
 * every repo in the scope and sorted newest first. A single-repo scope skips
 * the toggle — "the individual repo" from the spec is just this one row. */
function ScopeColumn({ entry, onClose }: { entry: ScopeEntry; onClose: () => void }) {
  const [view, setView] = useState<"repos" | "episodes">(
    entry.goals.length > 1 ? "repos" : "episodes",
  );
  useEffect(() => {
    setView(entry.goals.length > 1 ? "repos" : "episodes");
  }, [entry.key, entry.goals.length]);
  const query = scopeQuery(entry);
  const episodes = useQuery({
    queryKey: ["scope-episodes", entry.key],
    queryFn: () => (query ? api.scopeEpisodes(query, 50) : api.episodes(entry.goals[0].root)),
    enabled: view === "episodes",
    refetchInterval: 15_000,
  });
  return (
    <aside className="scope-column">
      <div className="scope-column-header">
        <strong>{entry.title}</strong>
        <button type="button" className="icon-button" aria-label="Close scope panel" onClick={onClose}>
          <X size={14} />
        </button>
      </div>
      {entry.goals.length > 1 && (
        <div className="scope-view-toggle">
          <button type="button" className={view === "repos" ? "active" : ""} onClick={() => setView("repos")}>
            Repos
          </button>
          <button type="button" className={view === "episodes" ? "active" : ""} onClick={() => setView("episodes")}>
            Episodes
          </button>
        </div>
      )}
      {view === "repos" ? (
        <div className="scope-repo-list">
          {entry.goals.map((goal) => (
            <ProjectRow goal={goal} collapsed={false} key={goal.project_id} />
          ))}
        </div>
      ) : (
        <div className="scope-episode-list">
          {entry.goals.length === 1 && <ProjectRow goal={entry.goals[0]} collapsed={false} />}
          {episodes.isLoading && <div className="sidebar-empty">loading…</div>}
          {(episodes.data || []).map((ep) => (
            <Link
              key={ep.episode_id}
              to="/p/$projectId/ep/$episodeId"
              params={{ projectId: ep.project_id || entry.goals[0].project_id, episodeId: ep.episode_id }}
              className="scope-episode-row"
            >
              <strong>{ep.feature_id || ep.episode_id}</strong>
              <small>
                {(ep.project_name ? `${ep.project_name} · ` : "") + ep.state
                  + (ep.started ? ` · ${ep.started.slice(0, 16).replace("T", " ")}` : "")}
              </small>
            </Link>
          ))}
          {episodes.isSuccess && !episodes.data.length && (
            <div className="sidebar-empty">no episodes yet</div>
          )}
        </div>
      )}
    </aside>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("plexus.sidebar") === "collapsed",
  );
  const [search, setSearch] = useState("");
  const [activeScopeKey, setActiveScopeKey] = useState<string | null>(null);
  const goals = useQuery({
    queryKey: ["goals"],
    queryFn: api.goals,
    refetchInterval: 5_000,
  });
  const fleet = useQuery({
    queryKey: ["fleet"],
    queryFn: api.fleet,
    staleTime: 30_000,
  });
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.dashboard(),
    refetchInterval: 5_000,
  });

  useEffect(() => {
    localStorage.setItem("plexus.sidebar", collapsed ? "collapsed" : "expanded");
  }, [collapsed]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "b") {
        event.preventDefault();
        setCollapsed((value) => !value);
      }
      if (event.key === "/" && !["INPUT", "TEXTAREA"].includes((event.target as Element).tagName)) {
        event.preventDefault();
        document.getElementById("project-search")?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // Each registered scope is 1+ repos. Priority mirrors the backend
  // (scopeQuery): an explicit label (repos tagged together by hand) wins;
  // otherwise repos auto-group by scope_id — the registered root they were
  // discovered under, so `plexus serve --root ~/Projects` over a five-repo
  // stack reads as one scope, not five. A repo added on its own has no
  // siblings under its root, so its auto-group is just itself. Pinned goals
  // stay a separate favorites shortcut (existing behavior), not a scope.
  const scopes = useMemo<ScopeEntry[]>(() => {
    const visible = goals.data || [];
    const pinned: Goal[] = [];
    const labeled = new Map<string, Goal[]>();
    const auto = new Map<string, Goal[]>();
    for (const goal of visible) {
      if (goal.pinned) pinned.push(goal);
      else if (goal.label) labeled.set(goal.label, [...(labeled.get(goal.label) || []), goal]);
      else auto.set(goal.scope_id, [...(auto.get(goal.scope_id) || []), goal]);
    }
    const entries: ScopeEntry[] = [];
    if (pinned.length) entries.push({ key: "pinned", title: "★ pinned", goals: pinned, kind: "pinned" });
    for (const [label, list] of [...labeled.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      entries.push({ key: `label:${label}`, title: label, goals: list, kind: "label" });
    }
    for (const [scopeId, list] of [...auto.entries()].sort(([, a], [, b]) =>
      a[0].scope_name.localeCompare(b[0].scope_name),
    )) {
      entries.push({
        key: `scope:${scopeId}`,
        title: list[0].scope_name,
        goals: [...list].sort((a, b) => a.name.localeCompare(b.name)),
        kind: "auto",
      });
    }
    return entries;
  }, [goals.data]);

  const activeScope = scopes.find((entry) => entry.key === activeScopeKey) || null;

  // Two levels, because "vascular" and "plexus" are different questions: the
  // first is a scope you want opened, the second a repo you want to land in.
  // Searching used to filter the goals the scopes were built from, so typing a
  // repo name silently rebuilt the grouping around it and you lost the scope
  // you were looking at.
  const query = search.trim().toLowerCase();
  const matches = useMemo(() => {
    if (!query) return null;
    const repos = (goals.data || []).filter((goal) =>
      `${goal.name} ${goal.goal_id} ${goal.label} ${goal.scope_name}`.toLowerCase().includes(query));
    return {
      scopes: scopes.filter((entry) => entry.title.toLowerCase().includes(query)),
      repos,
    };
  }, [goals.data, scopes, query]);

  const error = goals.error || dashboard.error;
  return (
    <Tooltip.Provider delayDuration={250}>
      <div className="app-shell">
        <header className="topbar">
          <Link to="/" className="brand"><Gauge size={17} /> plexus</Link>
          <span className="connection">
            {error ? "degraded" : dashboard.isFetching ? "updating…" : "connected"}
          </span>
          <div className="topbar-actions">
            <Link to="/alerts" className="alert-chip">
              <Bell size={14} />
              {dashboard.data?.alerts.length || 0}
            </Link>
            <SettingsDialog fleet={fleet.data} />
          </div>
        </header>
        {error && <div className="error-banner">{error.message} · showing last good data</div>}
        <div className="workspace">
          <aside className={`sidebar ${collapsed ? "collapsed" : ""}`}>
            <button
              className="collapse-button"
              aria-label={collapsed ? "Expand project panel" : "Collapse project panel"}
              onClick={() => setCollapsed((value) => !value)}
            >
              {collapsed ? <ChevronRight size={15} /> : <><ChevronLeft size={15} /> <span>collapse</span></>}
            </button>
            {!collapsed && (
              <label className="search-box">
                <Search size={14} />
                <input
                  id="project-search"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  placeholder="search scopes and repos…"
                />
              </label>
            )}
            <nav className="project-nav" aria-label="Projects">
              {matches && (
                <div className="search-results">
                  {matches.scopes.length > 0 && (
                    <div className="project-group">
                      <div className="group-label"><span>scopes</span><span>{matches.scopes.length}</span></div>
                      {matches.scopes.map((entry) => (
                        <button
                          type="button"
                          key={entry.key}
                          className={`project-row scope-row ${activeScopeKey === entry.key ? "active" : ""}`}
                          onClick={() => setActiveScopeKey(entry.key)}
                        >
                          <span className={`project-dot dot-${scopeStatus(entry.goals)}`} />
                          <span className="project-copy">
                            <strong>{entry.title}</strong>
                            <small>{entry.goals.length} repo{entry.goals.length === 1 ? "" : "s"}</small>
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                  {matches.repos.length > 0 && (
                    <div className="project-group">
                      <div className="group-label"><span>repos</span><span>{matches.repos.length}</span></div>
                      {matches.repos.map((goal) => (
                        <ProjectRow goal={goal} collapsed={false} key={goal.project_id} />
                      ))}
                    </div>
                  )}
                  {!matches.scopes.length && !matches.repos.length && (
                    <div className="sidebar-empty">nothing matches “{search.trim()}”</div>
                  )}
                </div>
              )}
              {!matches && scopes.map((entry) =>
                entry.kind === "pinned" ? (
                  <div className="project-group" key={entry.key}>
                    {!collapsed && <div className="group-label"><span>{entry.title}</span></div>}
                    {entry.goals.map((goal) => (
                      <ProjectRow goal={goal} collapsed={collapsed} key={goal.project_id} />
                    ))}
                  </div>
                ) : (
                  <button
                    type="button"
                    key={entry.key}
                    className={`project-row scope-row ${activeScopeKey === entry.key ? "active" : ""}`}
                    onClick={() => setActiveScopeKey((current) => (current === entry.key ? null : entry.key))}
                  >
                    <span className={`project-dot dot-${scopeStatus(entry.goals)}`} />
                    {!collapsed && (
                      <span className="project-copy">
                        <strong>{entry.title}</strong>
                        <small>
                          {entry.goals.length > 1
                            ? `${entry.goals.length} repos`
                            : entry.goals[0].status.toLowerCase()}
                        </small>
                      </span>
                    )}
                  </button>
                ),
              )}
              {!goals.isLoading && !scopes.length && (
                <div className="sidebar-empty">{collapsed ? "—" : "no projects"}</div>
              )}
            </nav>
          </aside>
          {activeScope && <ScopeColumn entry={activeScope} onClose={() => setActiveScopeKey(null)} />}
          <main className="main-content">{children}</main>
        </div>
      </div>
    </Tooltip.Provider>
  );
}
