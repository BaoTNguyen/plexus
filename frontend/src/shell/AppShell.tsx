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
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api";
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
  const stateClass = goal.running ? "running" : goal.code === 0 ? "ok" : goal.code === 2 ? "warn" : "bad";
  return (
    <div className="project-item">
      <Tooltip.Root>
        <Tooltip.Trigger asChild>
          <Link
            to="/p/$projectId/$tab"
            params={{ projectId: goal.project_id, tab: "goal" }}
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
            placeholder="group label"
          />
        </form>
      )}
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("plexus.sidebar") === "collapsed",
  );
  const [search, setSearch] = useState("");
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

  const grouped = useMemo(() => {
    const visible = (goals.data || []).filter((goal) =>
      `${goal.name} ${goal.goal_id} ${goal.label}`.toLowerCase().includes(search.toLowerCase()),
    );
    const groups = new Map<string, Goal[]>();
    for (const goal of visible) {
      const key = goal.pinned ? "★ pinned" : goal.label || "ungrouped";
      groups.set(key, [...(groups.get(key) || []), goal]);
    }
    return [...groups.entries()].sort(([a], [b]) =>
      a === "★ pinned" ? -1 : b === "★ pinned" ? 1 : a.localeCompare(b),
    );
  }, [goals.data, search]);

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
                  placeholder="search projects…"
                />
              </label>
            )}
            <nav className="project-nav" aria-label="Projects">
              {grouped.map(([label, list]) => (
                <div className="project-group" key={label}>
                  {!collapsed && <div className="group-label"><span>{label}</span></div>}
                  {list.map((goal) => (
                    <ProjectRow goal={goal} collapsed={collapsed} key={goal.project_id} />
                  ))}
                </div>
              ))}
              {!goals.isLoading && !grouped.length && (
                <div className="sidebar-empty">{collapsed ? "—" : "no projects"}</div>
              )}
            </nav>
          </aside>
          <main className="main-content">{children}</main>
        </div>
      </div>
    </Tooltip.Provider>
  );
}
