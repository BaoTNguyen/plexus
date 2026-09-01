"""The project overview: four standing documents, in Markdown.

These were TOML lists of strings, which was wrong in two ways. Architecture is
a diagram and a data model, program design is signatures and pseudocode, and a
bullet list can hold neither — so the sections that most needed a picture were
the ones the format refused. And nobody types these: they come out of a
back-and-forth with a model, which produces prose, code fences and diagrams,
not `constraints = ["...", "..."]`.

So each section is one Markdown file under `.plexus/overview/`. Markdown carries
prose, fenced code, mermaid diagrams and images without a schema, it diffs in
git, a model can write it directly, and it is still a plain file you can edit in
the terminal that is already open next to it.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

#: key, heading, and the one-line brief that tells a model what belongs here
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("product-review", "Product review",
     "The problem being solved, who has it, the behaviour that fixes it, what is "
     "explicitly out of scope, and how you will know it worked. Mockups and "
     "example flows belong here."),
    ("system-architecture", "System architecture",
     "How the components operate and fit together: contracts between them, data "
     "models, and the constraints everything must be built within. A mermaid "
     "diagram is usually clearer than the paragraph describing it."),
    ("program-design", "Program design",
     "One level below architecture: the types, method signatures, module layout "
     "and call paths work should target. Pseudocode and real signatures both "
     "belong here — this is what makes review a check rather than a reading."),
    ("vertical-slices", "Vertical slices",
     "The order work is built and validated in, and how it coordinates across "
     "repos. What ships first, what it proves, and what depends on it."),
)

KEYS = tuple(key for key, _, _ in SECTIONS)


def overview_dir(root: str | Path = ".") -> Path:
    return Path(root) / ".plexus" / "overview"


def section_path(root: str | Path, key: str) -> Path:
    if key not in KEYS:
        raise ValueError(f"unknown section {key!r}")
    return overview_dir(root) / f"{key}.md"


def read(root: str | Path = ".") -> list[dict]:
    """All four sections. A section that has never been written reads as empty
    rather than missing — the page always shows the same four headings, so an
    unfilled one is visibly a gap instead of silently absent."""
    out = []
    for key, title, brief in SECTIONS:
        path = section_path(root, key)
        try:
            text = path.read_text(encoding="utf-8")
            updated = datetime.datetime.fromtimestamp(
                path.stat().st_mtime, datetime.timezone.utc).isoformat()
        except OSError:
            text, updated = "", ""
        out.append({"key": key, "title": title, "brief": brief,
                    "text": text, "updated": updated})
    return out


def write(root: str | Path, key: str, text: str) -> dict:
    """Atomic replace, so a crash mid-write cannot leave half a document where
    a whole one was."""
    path = section_path(root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return {"key": key, "bytes": len(text.encode())}


def as_context(root: str | Path = ".") -> str:
    """The overview as one block of Markdown, for a planner prompt.

    Without this the sections are decoration: a model plans against the TOML
    scope lists and never sees the architecture the human agreed. Empty
    sections are skipped rather than sent as blank headings, which would read
    to a model as 'this project has no architecture' instead of 'not written
    down yet'.
    """
    parts = []
    for section in read(root):
        text = section["text"].strip()
        if text:
            parts.append(f"## {section['title']}\n\n{text}")
    return "\n\n".join(parts)


def discuss_prompt(root: str | Path = ".") -> str:
    """The opening turn for working on the overview with a model.

    One conversation for all four sections, not one per section. They are not
    independent — architecture answers what product review asked, and slices
    order what program design describes — so a model that can only see one at a
    time keeps proposing things the others already settled.

    It opens with what is actually written, so the first turn is a reading of
    the current state rather than a request for you to paste it back. And it
    points at files: a conversation that ends with prose in a scrollback has
    produced nothing the planner will ever read.
    """
    lines = [
        "Help me work on this project's overview. Four standing documents, "
        "in .plexus/overview/:",
        "",
    ]
    for section in read(root):
        text = section["text"].strip()
        lines.append(f"### {section['title']}  ({section['key']}.md)")
        lines.append(f"Should cover: {section['brief']}")
        lines.append("Currently: " + ("empty." if not text else "\n\n" + text))
        lines.append("")
    lines += [
        f"All four live under {overview_dir(root)}.",
        "",
        "Read the repo first, then tell me which section is weakest and why, and "
        "start there. Ask me questions before writing — this is a back-and-forth, "
        "not a generation task. When we agree on something, write it to that "
        "section's file in Markdown. Use fenced code for signatures and "
        "pseudocode, and a ```mermaid fence for diagrams. Keep every claim "
        "specific to this repo.",
    ]
    return "\n".join(lines)


def assets(root: str | Path = ".") -> list[str]:
    """Images available to embed, relative to the repo root."""
    found: list[str] = []
    base = Path(root)
    for pattern in ("*.png", "*.jpg", "*.jpeg", "*.svg", "*.webp", "*.gif"):
        for path in list(base.glob(pattern)) + list(base.glob(f"docs/**/{pattern}")) \
                + list(overview_dir(root).glob(pattern)):
            try:
                found.append(str(path.relative_to(base)))
            except ValueError:
                continue
    return sorted(set(found))[:200]


def mermaid_blocks(text: str) -> list[str]:
    """Diagram sources in a section, for a renderer that loads lazily."""
    return re.findall(r"```mermaid\n(.*?)```", text, re.DOTALL)


def demo() -> None:
    """Self-check: round-trip, empty sections stay listed, planner context skips
    blanks, and an unknown key is refused rather than writing outside the dir."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sections = read(root)
        assert [s["key"] for s in sections] == list(KEYS), sections
        assert all(s["text"] == "" for s in sections), "unwritten must read empty"
        assert as_context(root) == "", "blank sections must not pad the prompt"

        body = "# Arch\n\n```mermaid\ngraph TD; a-->b;\n```\n"
        write(root, "system-architecture", body)
        again = {s["key"]: s for s in read(root)}
        assert again["system-architecture"]["text"] == body
        assert again["system-architecture"]["updated"], "no mtime recorded"
        assert "## System architecture" in as_context(root)
        assert "## Product review" not in as_context(root), "empty section leaked"
        assert mermaid_blocks(body) == ["graph TD; a-->b;\n"], mermaid_blocks(body)

        # a traversal key must not escape the overview directory
        for bad in ("../../etc/passwd", "nope", ""):
            try:
                section_path(root, bad)
                raise AssertionError(f"accepted {bad!r}")
            except ValueError:
                pass

        # one prompt covers all four sections and opens with what is written
        prompt = discuss_prompt(root)
        assert all(title in prompt for _, title, _ in SECTIONS), prompt[:400]
        assert "graph TD; a-->b;" in prompt, "current contents not seeded"
        assert "Currently: empty." in prompt, "unwritten sections must say so"
        assert "Ask me questions" in prompt
    print("overview self-check ok")


if __name__ == "__main__":
    demo()
