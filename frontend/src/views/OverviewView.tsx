import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Eye, ImageIcon, Pencil } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Discussion } from "../components/Discussion";
import { Markdown } from "../components/Markdown";
import { Panel } from "../components/Status";
import type { OverviewSection } from "../types";

/** The project overview: four standing documents.
 *
 *  Written with a model, not typed into boxes. One conversation covers all four
 *  sections and runs in this tab — the agent opens with what is already written
 *  and edits the files directly, and this page re-reads them. Nothing here is a
 *  chat box, because a worse copy of the terminal is not worth building.
 *
 *  Markdown throughout, so a section can hold the diagram or the signature it
 *  actually needs. Bullet lists of strings could hold neither, which is why the
 *  sections that most needed a picture were the ones the old format refused. */

function Section({ section, root, assets }: {
  section: OverviewSection; root: string; assets: string[];
}) {
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(section.text);
  // a discussion rewrites the file underneath us; adopt it unless the user is
  // mid-edit, where clobbering their typing would be worse than being stale
  useEffect(() => { if (!editing) setDraft(section.text); }, [section.text, editing]);

  const save = useMutation({
    mutationFn: () => api.post("/overview", { root, key: section.key, text: draft }),
    onSuccess: () => {
      setEditing(false);
      client.invalidateQueries({ queryKey: ["overview", root] });
    },
  });
  return (
    <Panel
      title={section.title}
      action={
        <span className="section-actions">
          {section.updated && <time>{section.updated.slice(0, 10)}</time>}
          <button className="button button-small" onClick={() => setEditing((v) => !v)}>
            {editing ? <><Eye size={13} /> read</> : <><Pencil size={13} /> edit</>}
          </button>
        </span>
      }
    >
      {editing ? (
        <div className="section-editor">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            spellCheck={false}
            placeholder={section.brief}
            rows={18}
          />
          <div className="section-editor-bar">
            <small>
              Markdown. Fenced code for signatures and pseudocode, <code>```mermaid</code> for
              diagrams, <code>![](docs/x.png)</code> for images in the repo.
            </small>
            {assets.length > 0 && (
              <details className="section-assets">
                <summary><ImageIcon size={12} /> {assets.length} image{assets.length === 1 ? "" : "s"}</summary>
                <ul>
                  {assets.map((path) => (
                    <li key={path}>
                      <button onClick={() => setDraft((d) => `${d}\n\n![](${path})\n`)}>{path}</button>
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <button className="button button-primary button-small"
              disabled={draft === section.text || save.isPending}
              onClick={() => save.mutate()}>
              <Check size={13} /> save
            </button>
          </div>
          {save.error && <div className="inline-error">{save.error.message}</div>}
        </div>
      ) : section.text.trim() ? (
        <div className="section-body"><Markdown text={section.text} root={root} /></div>
      ) : (
        <div className="section-empty"><p>{section.brief}</p></div>
      )}
    </Panel>
  );
}

export function OverviewView({ root }: { root: string }) {
  const query = useQuery({
    queryKey: ["overview", root],
    queryFn: () => api.overview(root),
    refetchInterval: 4_000,
  });
  if (!query.data) return <div className="loading-grid" aria-label="Loading overview" />;
  return (
    <div className="stack">
      {/* one conversation for all four sections, above them, because they
          answer each other and a model that sees one at a time re-proposes
          what the others settled */}
      <Discussion root={root} view="overview" label="Discuss the overview" />
      {query.data.sections.map((section) => (
        <Section key={section.key} section={section} root={root} assets={query.data.assets} />
      ))}
    </div>
  );
}
