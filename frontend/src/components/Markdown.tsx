import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useRef, useState } from "react";

/** Renders a section: prose, fenced code, images out of the repo, and mermaid
 *  diagrams.
 *
 *  Mermaid is loaded only when a document actually contains one. It is by far
 *  the heaviest thing on this page, and most sections are prose and code — so
 *  paying for it up front would slow every view to serve a minority of them. */

let mermaidReady: Promise<typeof import("mermaid").default> | null = null;
function loadMermaid() {
  if (!mermaidReady) {
    mermaidReady = import("mermaid").then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        // Explicit, not left to the default: diagram labels are agent-written
        // text, and strict is what keeps them from carrying markup or links.
        securityLevel: "strict",
        theme: "dark",
        darkMode: true,
        themeVariables: {
          background: "#0b0f14", primaryColor: "#17202a",
          primaryTextColor: "#e8edf2", primaryBorderColor: "#3b4a59",
          lineColor: "#65a9ff", fontFamily: '"SFMono-Regular", monospace',
        },
      });
      return mermaid;
    });
  }
  return mermaidReady;
}

/** Images are written as ordinary repo-relative Markdown paths, so a section
 *  reads the same in an editor as it does here. Rewriting them to the asset
 *  endpoint is this renderer's job, not the author's.
 *
 *  Remote images are refused, not fetched. These documents are written by
 *  agents that may have read the web, and an <img> pointing off the box is a
 *  request your browser makes the moment the page opens -- to wherever the
 *  author chose, with whatever it put in the URL. null means "no image". */
function assetUrl(src: string, root: string): string | null {
  if (/^(data:image\/|\/api\/)/.test(src)) return src;
  if (/^[a-z][a-z0-9+.-]*:|^\/\//i.test(src)) return null;
  return `/api/overview-asset?root=${encodeURIComponent(root)}&path=${encodeURIComponent(src)}`;
}

/** Agent-written Markdown becomes HTML in this page, so it is sanitized as
 *  untrusted: no scripts, no event handlers, no javascript: links. data-*
 *  survives because the mermaid placeholders ride on data-src. */
function sanitize(html: string): string {
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}

const escape = (s: string) =>
  s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c] as string));

export function Markdown({ text, root }: { text: string; root: string }) {
  const host = useRef<HTMLDivElement>(null);
  const [html, setHtml] = useState("");

  useEffect(() => {
    let alive = true;
    const renderer = new marked.Renderer();
    renderer.image = ({ href, title, text: alt }) => {
      const src = assetUrl(href, root);
      return src === null
        ? `<span class="md-blocked-image">[remote image not loaded: ${escape(alt || href)}]</span>`
        : `<img src="${escape(src)}" alt="${escape(alt ?? "")}"${title ? ` title="${escape(title)}"` : ""} loading="lazy" />`;
    };
    // mermaid fences come through as placeholders and get drawn after mount;
    // everything else stays an ordinary code block
    renderer.code = ({ text: code, lang }) =>
      lang === "mermaid"
        ? `<div class="mermaid-block" data-src="${encodeURIComponent(code)}"></div>`
        : `<pre class="md-code"><code>${escape(code)}</code></pre>`;
    Promise.resolve(marked.parse(text || "", { renderer, breaks: true, gfm: true }))
      .then((out) => { if (alive) setHtml(sanitize(out)); });
    return () => { alive = false; };
  }, [text, root]);

  useEffect(() => {
    const blocks = host.current?.querySelectorAll<HTMLElement>(".mermaid-block[data-src]");
    if (!blocks?.length) return;
    let alive = true;
    loadMermaid().then(async (mermaid) => {
      for (const [index, block] of [...blocks].entries()) {
        if (!alive) return;
        const src = decodeURIComponent(block.dataset.src || "");
        try {
          const { svg } = await mermaid.render(`d${Date.now()}-${index}`, src);
          block.innerHTML = svg;
        } catch (error) {
          // a half-written diagram is normal mid-conversation, so show the
          // source and the reason rather than an empty gap -- as text, since
          // the source is exactly what the author wrote
          const pre = document.createElement("pre");
          pre.className = "md-code md-diagram-error";
          pre.textContent = `${(error as Error).message}\n\n${src}`;
          block.replaceChildren(pre);
        }
        block.removeAttribute("data-src");
      }
    });
    return () => { alive = false; };
  }, [html]);

  if (!text.trim()) return null;
  return <div className="markdown" ref={host} dangerouslySetInnerHTML={{ __html: html }} />;
}
