import type {
  Dashboard,
  AccountingConfig,
  Episode,
  EpisodeDetail,
  Fleet,
  Goal,
  GoalDetail,
  Overview,
  TaskBoard,
  TermSessions,
  TermWindows,
  TranscriptLine,
  Validation,
} from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  const body = await response.json();
  if (!response.ok) {
    throw new Error(body.error || `${response.status} ${response.statusText}`);
  }
  return body as T;
}

export const api = {
  goals: () => request<Goal[]>("/api/goals"),
  fleet: () => request<Fleet>("/api/fleet"),
  // window_h 0 is all time
  dashboard: (windowH = 24) => request<Dashboard>(`/api/dashboard?window_h=${windowH}`),
  saveAccounting: (config: AccountingConfig) =>
    request<AccountingConfig>("/api/accounting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    }),
  goal: (root: string) =>
    request<GoalDetail>(`/api/goal?root=${encodeURIComponent(root)}`),
  episodes: (root: string) =>
    request<Episode[]>(`/api/episodes?root=${encodeURIComponent(root)}&limit=100`),
  scopeEpisodes: (query: { label?: string; scope_id?: string }, limit = 50) => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (query.label) qs.set("label", query.label);
    if (query.scope_id) qs.set("scope_id", query.scope_id);
    return request<Episode[]>(`/api/scope-episodes?${qs.toString()}`);
  },
  episode: (root: string, id: string) =>
    request<EpisodeDetail>(
      `/api/episode?root=${encodeURIComponent(root)}&id=${encodeURIComponent(id)}`,
    ),
  tasks: (root: string) => request<TaskBoard>(`/api/tasks?root=${encodeURIComponent(root)}`),
  overview: (root: string) => request<Overview>(`/api/overview?root=${encodeURIComponent(root)}`),
  validation: (root: string) =>
    request<Validation>(`/api/validation?root=${encodeURIComponent(root)}`),
  termSessions: (root: string) =>
    request<TermSessions>(`/api/term/sessions?root=${encodeURIComponent(root)}`),
  termWindows: (root: string) =>
    request<TermWindows>(`/api/term/windows?root=${encodeURIComponent(root)}`),
  termTranscript: (root: string, name: string) =>
    request<{ name: string; text: string }>(
      `/api/term/transcript?root=${encodeURIComponent(root)}&name=${encodeURIComponent(name)}`),
  /** the same recording, replayed server-side through a screen: one line per
   *  line, with a time of day when the recording carries timing */
  termTranscriptLines: (root: string, name: string) =>
    request<{ name: string; lines: TranscriptLine[] }>(
      `/api/term/transcript?format=lines&root=${encodeURIComponent(root)}`
      + `&name=${encodeURIComponent(name)}`),
  post: <T>(path: string, body: Record<string, unknown>) =>
    request<T>(`/api${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};
