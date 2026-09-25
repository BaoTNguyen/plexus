/** Which tab you were last on, per project.
 *
 *  Switching projects used to land you on the overview every time, because the
 *  sidebar linked to a tab name that no longer existed and the router fell
 *  through to the default. Remembering the tab is the smaller half of the fix;
 *  react-query already keeps the data warm, so coming back is instant.
 *
 *  localStorage, wrapped: a private window can throw on both read and write,
 *  and losing a tab preference is not worth a blank page.
 */
const KEY = "plexus.lastTab";

function all(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "{}");
  } catch {
    return {};
  }
}

export function lastTab(projectId: string): string {
  return all()[projectId] || "overview";
}

export function rememberTab(projectId: string, tab: string): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...all(), [projectId]: tab }));
  } catch {
    /* no storage: the tab just stops being remembered */
  }
}
