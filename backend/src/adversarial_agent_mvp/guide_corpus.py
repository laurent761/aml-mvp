"""Build a deterministic, deduplicated snapshot of the guide and its direct links.

Only the operator CLI reads the repository. HTTP clients cannot select paths or URLs.
The snapshot is derived data; edit the source documents and rebuild, never edit it.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

SCHEMA = "aml.guide-corpus.v1"
DEFAULT_CORPUS = Path(__file__).with_name("guide_knowledge.json.gz")
MAX_FILE_BYTES = 1_000_000
CHUNK_CHARS = 1800
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP_CLASSES = {"source", "source-index", "system-map", "map-aside", "component-tools", "role-buttons", "reading-path", "walkthrough", "lineage-flow", "hero-links", "topbar", "footer"}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    def text(self) -> str:
        return " ".join(child.text() if isinstance(child, Node) else child for child in self.children)


class HTMLTree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag == "a" and node.attrs.get("href"):
            self.links.add(node.attrs["href"])
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def html_sections(text: str) -> tuple[list[dict[str, Any]], set[str]]:
    parser = HTMLTree()
    parser.feed(text)
    sections: list[dict[str, Any]] = []
    headings: list[tuple[int, str]] = []

    def walk(node: Node, anchor: str = "", in_main: bool = False) -> None:
        if node.tag in {"script", "style", "nav", "aside", "footer", "noscript", "button", "select"}:
            return
        if set(node.attrs.get("class", "").split()) & SKIP_CLASSES:
            return
        in_main = in_main or node.tag == "main"
        anchor = node.attrs.get("id", anchor)
        if in_main and re.fullmatch("h[1-6]", node.tag):
            level = int(node.tag[1])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, normalize(node.text())))
            sections.append({"heading": " / ".join(value for _, value in headings), "anchor": anchor, "blocks": []})
            return
        if in_main and node.tag in {"p", "li", "dt", "dd", "tr", "pre"}:
            nested = any(isinstance(child, Node) and child.tag in {"p", "h3", "h4", "ul", "ol", "div"} for child in node.children)
            if not nested:
                value = normalize(node.text())
                if value and sections:
                    sections[-1]["blocks"].append(value)
                return
        for child in node.children:
            if isinstance(child, Node):
                walk(child, anchor, in_main)

    walk(parser.root)
    return sections, parser.links


def text_sections(text: str, name: str) -> list[dict[str, Any]]:
    """Keep Markdown headings and code fences; retain line references for all text."""
    sections: list[dict[str, Any]] = []
    heading = name
    start = 1
    lines: list[str] = []
    fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        match = re.match(r"^(#{1,6})\s+(.+)", line) if name.endswith(".md") and not fence else None
        if match:
            if lines:
                sections.append({"heading": heading, "anchor": f"L{start}", "blocks": "\n".join(lines).split("\n\n")})
            heading, start, lines = match[2], number, []
        else:
            lines.append(line)
    if lines:
        sections.append({"heading": heading, "anchor": f"L{start}", "blocks": "\n".join(lines).split("\n\n")})
    return sections


def safe_link(root: Path, href: str) -> Path | None:
    link = urlsplit(href)
    if link.scheme or link.netloc or not link.path:
        return None
    path = (root / unquote(link.path)).resolve()
    if not path.is_relative_to(root) or any(part.startswith(".") for part in path.relative_to(root).parts):
        raise ValueError(f"guide link escapes the permitted corpus: {href}")
    if path.suffix not in {".md", ".py", ".json", ".yaml", ".yml", ".html"} and path.name != "Dockerfile":
        raise ValueError(f"unsupported guide source: {href}")
    if path.name == "project-guide.html":
        return None  # Compatibility redirect, never a second copy of the guide.
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"missing or oversized guide source: {href}")
    return path


def build_corpus(root: Path) -> dict[str, Any]:
    root = root.resolve()
    guide = root / "architecture-guide.html"
    guide_text = guide.read_text()
    guide_sections, links = html_sections(guide_text)
    paths = {guide}
    for href in sorted(links):
        if path := safe_link(root, href):
            paths.add(path)
    sources = []
    chunks: dict[str, dict[str, Any]] = {}
    duplicate_count = 0
    for path in sorted(paths, key=lambda p: (p != guide, str(p))):
        raw = path.read_bytes()
        name = path.relative_to(root).as_posix()
        source_hash = hashlib.sha256(raw).hexdigest()
        sources.append({"path": name, "sha256": source_hash, "bytes": len(raw)})
        sections = guide_sections if path == guide else text_sections(raw.decode("utf-8"), name)
        for section in sections:
            # Pack whole paragraphs where possible. No overlap duplicates in stored chunks.
            packed: list[str] = []
            pending = ""
            for block in section["blocks"]:
                block = block.strip()
                if not block:
                    continue
                pieces = [block[i:i + CHUNK_CHARS] for i in range(0, len(block), CHUNK_CHARS)]
                for piece in pieces:
                    if pending and len(pending) + len(piece) + 2 > CHUNK_CHARS:
                        packed.append(pending)
                        pending = ""
                    pending = pending + "\n\n" + piece if pending else piece
            if pending:
                packed.append(pending)
            for number, body in enumerate(packed):
                if len(normalize(body)) < 40:
                    continue
                digest = hashlib.sha256(normalize(body).encode()).hexdigest()
                reference = {"path": name, "heading": section["heading"], "anchor": section["anchor"], "part": number + 1, "sha256": source_hash}
                if digest in chunks:
                    if reference not in chunks[digest]["references"]:
                        chunks[digest]["references"].append(reference)
                    duplicate_count += 1
                else:
                    chunks[digest] = {"id": digest[:24], "text": body, "references": [reference]}
    if not chunks:
        raise ValueError("the guide corpus has no readable content")
    return {"schema": SCHEMA, "sources": sources, "chunks": list(chunks.values()), "duplicates_removed": duplicate_count}


def encode_corpus(corpus: dict[str, Any]) -> bytes:
    raw = json.dumps(corpus, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(raw, mtime=0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--check", action="store_true", help="Fail if the packaged corpus is stale.")
    args = parser.parse_args()
    corpus = build_corpus(args.repo)
    encoded = encode_corpus(corpus)
    if args.check:
        if not args.output.exists() or args.output.read_bytes() != encoded:
            raise SystemExit("Guide corpus is stale. Rebuild it before packaging the backend.")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(args.output)
    print(json.dumps({"documents": len(corpus["sources"]), "chunks": len(corpus["chunks"]), "duplicates_removed": corpus["duplicates_removed"], "bytes": len(encoded)}))


if __name__ == "__main__":
    main()
