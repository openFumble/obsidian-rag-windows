# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=1.10,<3",
#     "sqlite-vec>=0.1.6,<0.2",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-01T00:00:00Z"
# ///
r"""obsidian-rag-windows: local RAG search for an Obsidian vault, as an MCP server.

One file, no build step. Run with uv; it fetches the two dependencies itself.

    uv run --script obsidian_rag.py init --vault "C:\Users\you\Documents\Vault"
    uv run --script obsidian_rag.py index        # first full index
    uv run --script obsidian_rag.py check        # self-test
    uv run --script obsidian_rag.py              # MCP server (stdio) for Claude Desktop

What it does
------------
* Indexes every Markdown note: heading-aware chunks, embeddings from a local
  Ollama model (default bge-m3, multilingual), stored in SQLite with
  sqlite-vec (vectors) and FTS5 (keywords). Nothing leaves the computer.
* Hybrid search: meaning + exact words, fused with Reciprocal Rank Fusion,
  filters by type, folder and tag, at most N chunks per note.
* Link graph from wikilinks and Markdown links, links + backlinks, similar notes.
* Keeps itself up to date: while the MCP server runs it watches the vault and
  re-indexes changed notes; on start it catches up on changes made while it
  was not running. Only changed chunks are re-embedded.
* Safe vault tools: read, create, append, replace, move (no delete), all
  confined to the vault and to Markdown files.
* Paths always use "/"; "\" in input is accepted (Windows).

License: MIT (see LICENSE). Project: https://github.com/openFumble/obsidian-rag-windows
"""

from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import posixpath
import re
import sqlite3
import struct
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"
APP = "obsidian-rag-windows"

log = logging.getLogger(APP)


# =============================================================================
# Configuration
# =============================================================================

def app_home() -> Path:
    """Where config, index and logs live (never inside the vault)."""
    env = os.environ.get("OBSIDIAN_RAG_HOME")
    if env:
        return Path(env)
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / APP
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / APP


DEFAULTS: Dict[str, Dict[str, Any]] = {
    "embedding": {
        "url": "http://localhost:11434",
        "model": "bge-m3",
        "batch_size": 16,
        "timeout": 120,
    },
    "index": {
        "max_chunk_chars": 2000,
        "min_chunk_chars": 200,
        "max_heading_level": 4,
        # Folder names skipped anywhere in the vault. Folders starting with "." are always skipped.
        "exclude_folders": ["node_modules", "_resources"],
        # Path prefix -> note type, e.g. {"Journal/" = "daily"}. Frontmatter "type"/"typ" wins.
        "type_rules": {},
        "watch": True,
        "watch_interval": 5,
    },
    "search": {
        "similarity_threshold": 0.40,
        "limit": 5,
        "max_per_note": 2,
        "max_chars": 600,
        "rrf_k": 60,
    },
    "vault": {
        "allow_write": True,
    },
}


@dataclass
class Config:
    path: Path
    vault: Path
    data: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def get(self, section: str, key: str) -> Any:
        return self.data.get(section, {}).get(key, DEFAULTS[section][key])

    @property
    def home(self) -> Path:
        return self.path.parent

    @property
    def db_path(self) -> Path:
        return self.home / "index.db"

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        cfg_path = Path(path or os.environ.get("OBSIDIAN_RAG_CONFIG") or app_home() / "config.toml")
        data: Dict[str, Any] = {}
        if cfg_path.exists():
            with open(cfg_path, "rb") as f:
                data = tomllib.load(f)
        vault = os.environ.get("OBSIDIAN_RAG_VAULT") or data.get("vault_path") or ""
        if not vault:
            raise SystemExit(
                f"No vault configured. Run:  obsidian_rag.py init --vault \"<path to your vault>\"\n"
                f"(config file: {cfg_path})")
        sections = {k: v for k, v in data.items() if isinstance(v, dict)}
        if os.environ.get("OBSIDIAN_RAG_MODEL"):
            sections.setdefault("embedding", {})["model"] = os.environ["OBSIDIAN_RAG_MODEL"]
        if os.environ.get("OBSIDIAN_RAG_OLLAMA_URL"):
            sections.setdefault("embedding", {})["url"] = os.environ["OBSIDIAN_RAG_OLLAMA_URL"]
        return cls(path=cfg_path, vault=Path(vault).expanduser(), data=sections)


def toml_string(value: str) -> str:
    """A TOML string literal for any value (paths with backslashes stay readable)."""
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return json.dumps(value)


def write_default_config(path: Path, vault: str, model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"""# obsidian-rag-windows configuration
# Changes take effect after restarting Claude Desktop.

vault_path = {toml_string(vault)}

[embedding]
# Local Ollama server and embedding model. Changing the model needs a rebuild
# (tool "rebuild_index" or: obsidian_rag.py index --full).
url = "http://localhost:11434"
model = {toml_string(model)}

[index]
max_chunk_chars = 2000
# Folder names to skip (folders starting with "." are always skipped)
exclude_folders = ["node_modules", "_resources"]
# Re-index changed notes automatically while Claude Desktop runs
watch = true
watch_interval = 5

# Optional: note type by folder, used by the note_type search filter.
# Frontmatter "type:" wins. Notes named like 2026-10-11.md are "daily".
[index.type_rules]
# "Journal/" = "daily"
# "Projects/" = "project"

[search]
# Semantic hits below this cosine similarity are dropped (0..1)
similarity_threshold = 0.40
limit = 5
max_per_note = 2
max_chars = 600

[vault]
# false = read-only (no write_note, append_to_note, replace_in_note, move_note)
allow_write = true
"""
    path.write_text(text, encoding="utf-8")


def setup_logging(home: Path, verbose: bool = False) -> None:
    """Log to stderr (never stdout: that is the MCP channel) and to a small rotating file."""
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if not log.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        log.addHandler(h)
        try:
            home.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(home / "obsidian-rag.log", maxBytes=1_000_000,
                                                      backupCount=2, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass


# =============================================================================
# Paths
# =============================================================================

def norm_path(path: Optional[str]) -> str:
    """Vault-relative path with forward slashes and no leading slash or './'."""
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return posixpath.normpath(p).lstrip("/") if p else ""


# =============================================================================
# Markdown parsing: frontmatter, chunks, links, tags
# =============================================================================

_FM_LIST_ITEM = re.compile(r"^\s*-\s+(.*)$")
_FM_KEY = re.compile(r"^([A-Za-z0-9_\-äöüÄÖÜß]+)\s*:\s*(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Minimal YAML frontmatter parser (scalars, inline lists, block lists).

    Enough for Obsidian properties such as tags, aliases and type; anything
    more complex is ignored rather than failing.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text
    meta: Dict[str, Any] = {}
    key: Optional[str] = None
    for raw in lines[1:end]:
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m_item = _FM_LIST_ITEM.match(line)
        if m_item and key is not None and isinstance(meta.get(key), list):
            meta[key].append(_unquote(m_item.group(1)))
            continue
        m = _FM_KEY.match(line)
        if not m or raw[:1] in (" ", "\t"):
            continue
        key, value = m.group(1).lower(), m.group(2).strip()
        if value == "":
            meta[key] = []
        elif value.startswith("[") and value.endswith("]"):
            meta[key] = [_unquote(x) for x in value[1:-1].split(",") if x.strip()]
        else:
            meta[key] = _unquote(value)
    body = "\n".join(lines[end + 1:])
    return meta, body


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v for v in re.split(r"[,\s]+", str(value)) if v]


_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_INLINE_TAG = re.compile(r"(?:(?<=\s)|^)#([\w][\w\-/]*)", re.UNICODE)


def strip_code(body: str) -> str:
    """Body without fenced and inline code (for link and tag extraction)."""
    out, fence = [], None
    for line in body.split("\n"):
        m = _FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if m:
            fence = m.group(1)
            continue
        out.append(_INLINE_CODE.sub(" ", line))
    return "\n".join(out)


def extract_tags(meta: Dict[str, Any], body: str) -> List[str]:
    tags = _as_list(meta.get("tags") or meta.get("tag"))
    for line in strip_code(body).split("\n"):
        if _HEADING.match(line):
            continue
        tags += [t for t in _INLINE_TAG.findall(line) if not t.isdigit()]
    seen, out = set(), []
    for t in tags:
        t = t.lstrip("#").strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


@dataclass
class Chunk:
    ord: int
    heading: str        # heading path, e.g. "VLAN > Untagged und Tagged"
    text: str           # chunk text as written in the note
    embed_text: str     # text that is embedded (title + heading path + text)
    chash: str = ""


def _split_long(text: str, max_chars: int) -> List[str]:
    """Split text at paragraph, then line, then hard boundaries; fences stay intact if possible."""
    if len(text) <= max_chars:
        return [text]
    paras, cur, fence = [], [], None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence:
            cur.append(line)
            if m and m.group(1)[0] == fence[0]:
                fence = None
            continue
        if m:
            fence = m.group(1)
        if not line.strip() and not fence:
            if cur:
                paras.append("\n".join(cur))
                cur = []
            continue
        cur.append(line)
    if cur:
        paras.append("\n".join(cur))

    pieces: List[str] = []
    for p in paras:
        if len(p) <= max_chars:
            pieces.append(p)
            continue
        buf = ""
        for line in p.split("\n"):
            while len(line) > max_chars:
                if buf:
                    pieces.append(buf)
                    buf = ""
                pieces.append(line[:max_chars])
                line = line[max_chars:]
            if buf and len(buf) + 1 + len(line) > max_chars:
                pieces.append(buf)
                buf = line
            else:
                buf = f"{buf}\n{line}" if buf else line
        if buf:
            pieces.append(buf)

    out: List[str] = []
    buf = ""
    for p in pieces:
        if buf and len(buf) + 2 + len(p) > max_chars:
            out.append(buf)
            buf = p
        else:
            buf = f"{buf}\n\n{p}" if buf else p
    if buf:
        out.append(buf)
    return out


def chunk_markdown(body: str, title: str, max_chars: int = 2000, min_chars: int = 200,
                   max_level: int = 4) -> List[Chunk]:
    """Heading-aware chunks. Headings inside code fences are not headings."""
    sections: List[Tuple[str, List[str]]] = []
    stack: List[Tuple[int, str]] = []
    cur_lines: List[str] = []
    cur_path = ""
    fence = None

    def flush():
        if cur_lines:
            sections.append((cur_path, list(cur_lines)))

    for line in body.split("\n"):
        m_f = _FENCE.match(line)
        if fence:
            cur_lines.append(line)
            if m_f and m_f.group(1)[0] == fence[0] and len(m_f.group(1)) >= len(fence):
                fence = None
            continue
        if m_f:
            fence = m_f.group(1)
            cur_lines.append(line)
            continue
        m_h = _HEADING.match(line)
        if m_h and len(m_h.group(1)) <= max_level:
            flush()
            level, text = len(m_h.group(1)), m_h.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            cur_path = " > ".join(t for _, t in stack)
            cur_lines = [line]
            continue
        cur_lines.append(line)
    flush()

    # Drop empty sections, merge small ones into the previous chunk.
    merged: List[Tuple[str, str]] = []
    for path, lines in sections:
        text = "\n".join(lines).strip()
        content = "\n".join(l for l in lines if not _HEADING.match(l)).strip()
        if not content:
            continue
        if merged and len(text) < min_chars and len(merged[-1][1]) + 2 + len(text) <= max_chars:
            merged[-1] = (merged[-1][0], merged[-1][1] + "\n\n" + text)
        else:
            merged.append((path, text))

    chunks: List[Chunk] = []
    for path, text in merged:
        for piece in _split_long(text, max_chars):
            header = title if not path or path == title else f"{title} > {path}"
            embed = f"{header}\n\n{piece}"
            chunks.append(Chunk(ord=len(chunks), heading=path or title, text=piece, embed_text=embed,
                                chash=hashlib.sha1(embed.encode("utf-8")).hexdigest()))
    return chunks


_WIKILINK = re.compile(r"!?\[\[([^\]\|#\^\n]+)(?:[#\^][^\]\|\n]*)?(?:\|[^\]\n]*)?\]\]")
_MDLINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^)\s]+))(?:\s+\"[^\"]*\")?\s*\)")


def extract_raw_links(body: str, source: str) -> List[Tuple[str, str]]:
    """(kind, value) pairs: ("name", "Note") for wikilinks, ("path", "dir/note.md") for md links."""
    clean = strip_code(body)
    out: List[Tuple[str, str]] = []
    for m in _WIKILINK.finditer(clean):
        name = m.group(1).strip()
        if name:
            out.append(("name", name))
    src_dir = posixpath.dirname(norm_path(source))
    for m in _MDLINK.finditer(clean):
        target = urllib.parse.unquote((m.group(1) or m.group(2)).split("#", 1)[0]).strip()
        if not target or re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", target):
            continue
        if not target.lower().endswith(".md"):
            continue
        if target.startswith("/"):
            path = norm_path(target)
        else:
            path = norm_path(posixpath.join(src_dir, target))
        if not path.startswith(".."):
            out.append(("path", path))
    return list(dict.fromkeys(out))


class LinkResolver:
    """Resolves link names to vault paths like Obsidian: exact path, else unique-ish basename."""

    def __init__(self, paths: Iterable[str]):
        self.by_path: Dict[str, str] = {}
        self.by_name: Dict[str, List[str]] = {}
        for p in paths:
            self.by_path[p.lower()] = p
            stem = posixpath.splitext(posixpath.basename(p))[0].lower()
            self.by_name.setdefault(stem, []).append(p)

    def resolve(self, kind: str, value: str) -> Optional[str]:
        v = norm_path(value)
        if not v:
            return None
        if kind == "path" or "/" in v:
            hit = self.by_path.get(v.lower()) or self.by_path.get(v.lower() + ".md")
            if hit:
                return hit
        stem = posixpath.basename(v)
        if stem.lower().endswith(".md"):
            stem = stem[:-3]
        matches = self.by_name.get(stem.lower())
        if not matches:
            return None
        return min(matches, key=lambda p: (len(p), p))


@dataclass
class ParsedNote:
    path: str
    title: str
    type: str
    tags: List[str]
    aliases: List[str]
    chunks: List[Chunk]
    raw_links: List[Tuple[str, str]]


_DATE_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_note(path: str, text: str, cfg: Config) -> ParsedNote:
    path = norm_path(path)
    title = posixpath.splitext(posixpath.basename(path))[0]
    meta, body = parse_frontmatter(text)
    ntype = str(meta.get("type") or meta.get("typ") or "").strip().lower()
    if not ntype:
        rules = cfg.get("index", "type_rules") or {}
        for prefix in sorted(rules, key=len, reverse=True):
            if path.lower().startswith(norm_path(prefix).lower().rstrip("/") + "/"):
                ntype = str(rules[prefix]).lower()
                break
    if not ntype:
        ntype = "daily" if _DATE_NAME.match(title) else "note"
    chunks = chunk_markdown(body, title, int(cfg.get("index", "max_chunk_chars")),
                            int(cfg.get("index", "min_chunk_chars")),
                            int(cfg.get("index", "max_heading_level")))
    return ParsedNote(path=path, title=title, type=ntype, tags=extract_tags(meta, body),
                      aliases=_as_list(meta.get("aliases") or meta.get("alias")),
                      chunks=chunks, raw_links=extract_raw_links(body, path))


# =============================================================================
# Embeddings (Ollama)
# =============================================================================

class EmbeddingError(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(self, url: str, model: str, batch_size: int = 16, timeout: float = 120):
        self.url = url.rstrip("/")
        self.model = model
        self.batch_size = max(1, int(batch_size))
        self.timeout = timeout
        self._legacy = False
        self.calls = 0          # number of texts embedded (for stats and tests)

    def _post(self, endpoint: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict:
        req = urllib.request.Request(f"{self.url}{endpoint}", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 404 and "model" in body.lower():
                raise EmbeddingError(f"Ollama has no model '{self.model}'. Run: ollama pull {self.model}") from e
            if e.code == 404:
                raise FileNotFoundError(endpoint) from e
            raise EmbeddingError(f"Ollama error {e.code}: {body[:200]}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            raise EmbeddingError(f"Ollama not reachable at {self.url} ({e}). Is Ollama running?") from e

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = list(texts[i:i + self.batch_size])
            if not self._legacy:
                try:
                    data = self._post("/api/embed", {"model": self.model, "input": batch, "truncate": True})
                    embs = data.get("embeddings") or []
                    if len(embs) != len(batch):
                        raise EmbeddingError("Ollama returned a wrong number of embeddings")
                    out += embs
                    self.calls += len(batch)
                    continue
                except FileNotFoundError:
                    self._legacy = True   # Ollama older than 0.3: one text per request
            for t in batch:
                data = self._post("/api/embeddings", {"model": self.model, "prompt": t})
                if not data.get("embedding"):
                    raise EmbeddingError("Ollama returned an empty embedding")
                out.append(data["embedding"])
                self.calls += 1
        return out

    def check(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"url": self.url, "model": self.model}
        try:
            req = urllib.request.Request(f"{self.url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                models = [m.get("name", "") for m in json.load(resp).get("models", [])]
            info["reachable"] = True
            info["model_installed"] = any(m == self.model or m.split(":")[0] == self.model.split(":")[0]
                                          for m in models)
        except Exception as e:  # noqa: BLE001
            info["reachable"] = False
            info["error"] = str(e)
        return info


# =============================================================================
# Store: SQLite + sqlite-vec + FTS5
# =============================================================================

def _f32(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unf32(blob: bytes) -> List[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def build_fts_query(query: str) -> str:
    """Free text -> safe FTS5 query: every word must occur; "bge-m3" -> phrase "bge m3"."""
    phrases = []
    for term in query.split():
        parts = re.findall(r"\w+", term, flags=re.UNICODE)
        if parts:
            phrases.append('"' + " ".join(parts) + '"')
    return " ".join(phrases)


class Store:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS files (
        path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, hash TEXT,
        title TEXT, type TEXT, tags TEXT, aliases TEXT, indexed_at TEXT);
    CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY, path TEXT NOT NULL, ord INTEGER, heading TEXT,
        content TEXT NOT NULL, chash TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
    CREATE INDEX IF NOT EXISTS idx_chunks_chash ON chunks(chash);
    CREATE TABLE IF NOT EXISTS raw_links (source TEXT, kind TEXT, value TEXT);
    CREATE INDEX IF NOT EXISTS idx_raw_links_source ON raw_links(source);
    CREATE TABLE IF NOT EXISTS links (source TEXT, target TEXT, PRIMARY KEY (source, target));
    CREATE INDEX IF NOT EXISTS idx_links_target ON links(target);
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False,
                                  isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(self.SCHEMA)
        self.vec_error: Optional[str] = None
        self.fts_error: Optional[str] = None
        try:
            import sqlite_vec  # type: ignore
            self.db.enable_load_extension(True)
            sqlite_vec.load(self.db)
            self.db.enable_load_extension(False)
        except Exception as e:  # noqa: BLE001
            self.vec_error = f"sqlite-vec unavailable: {e}"
        try:
            self.db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                id UNINDEXED, title, heading, content, tags,
                tokenize = 'unicode61 remove_diacritics 2')""")
        except sqlite3.OperationalError as e:
            self.fts_error = f"FTS5 unavailable: {e}"

    # -- helpers ---------------------------------------------------------------

    @property
    def vec_ok(self) -> bool:
        return self.vec_error is None

    @property
    def fts_ok(self) -> bool:
        return self.fts_error is None

    def q(self, sql: str, params: Sequence[Any] = ()) -> List[tuple]:
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def meta_get(self, key: str) -> Optional[str]:
        r = self.q("SELECT value FROM meta WHERE key = ?", (key,))
        return r[0][0] if r else None

    def meta_set(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def has_vec_table(self) -> bool:
        return bool(self.q("SELECT 1 FROM sqlite_master WHERE name = 'chunks_vec'"))

    def ensure_vec_table(self, dim: int, model: str) -> None:
        with self.lock:
            if not self.has_vec_table():
                self.db.execute(f"""CREATE VIRTUAL TABLE chunks_vec USING vec0(
                    id TEXT PRIMARY KEY, embedding float[{int(dim)}] distance_metric=cosine)""")
                self.meta_set("dim", str(dim))
                self.meta_set("model", model)

    # -- writes ----------------------------------------------------------------

    def vectors_by_chash(self, chashes: Iterable[str]) -> Dict[str, bytes]:
        """Stored embeddings that can be reused for unchanged chunk texts."""
        out: Dict[str, bytes] = {}
        if not self.vec_ok or not self.has_vec_table():
            return out
        ch = list(set(chashes))
        for i in range(0, len(ch), 400):
            part = ch[i:i + 400]
            qm = ",".join("?" * len(part))
            for chash, blob in self.q(
                    f"SELECT c.chash, v.embedding FROM chunks c JOIN chunks_vec v ON v.id = c.id "
                    f"WHERE c.chash IN ({qm})", part):
                out.setdefault(chash, blob)
        return out

    def _delete_chunks(self, path: str) -> None:
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE path = ?", (path,))]
        if not ids:
            return
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            qm = ",".join("?" * len(part))
            if self.vec_ok and self.has_vec_table():
                self.db.execute(f"DELETE FROM chunks_vec WHERE id IN ({qm})", part)
            if self.fts_ok:
                self.db.execute(f"DELETE FROM chunks_fts WHERE id IN ({qm})", part)
            self.db.execute(f"DELETE FROM chunks WHERE id IN ({qm})", part)

    def replace_note(self, note: ParsedNote, vectors: Dict[str, bytes], mtime_ns: int, size: int,
                     fhash: str) -> None:
        """Swap all rows of one note in a single transaction."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._delete_chunks(note.path)
                title_field = " ".join([note.title] + note.aliases)
                tags_field = " ".join(note.tags)
                for c in note.chunks:
                    cid = hashlib.sha1(f"{note.path}\0{c.ord}\0{c.chash}".encode("utf-8")).hexdigest()[:24]
                    self.db.execute("INSERT OR REPLACE INTO chunks (id, path, ord, heading, content, chash) "
                                    "VALUES (?, ?, ?, ?, ?, ?)", (cid, note.path, c.ord, c.heading, c.text, c.chash))
                    if c.chash in vectors and self.vec_ok and self.has_vec_table():
                        self.db.execute("INSERT INTO chunks_vec (id, embedding) VALUES (?, ?)",
                                        (cid, vectors[c.chash]))
                    if self.fts_ok:
                        self.db.execute("INSERT INTO chunks_fts (id, title, heading, content, tags) "
                                        "VALUES (?, ?, ?, ?, ?)", (cid, title_field, c.heading, c.text, tags_field))
                self.db.execute(
                    "INSERT OR REPLACE INTO files (path, mtime_ns, size, hash, title, type, tags, aliases, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (note.path, mtime_ns, size, fhash, note.title, note.type, json.dumps(note.tags),
                     json.dumps(note.aliases), datetime.now(timezone.utc).isoformat(timespec="seconds")))
                self.db.execute("DELETE FROM raw_links WHERE source = ?", (note.path,))
                self.db.executemany("INSERT INTO raw_links (source, kind, value) VALUES (?, ?, ?)",
                                    [(note.path, k, v) for k, v in note.raw_links])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def touch_file(self, path: str, mtime_ns: int, size: int) -> None:
        with self.lock:
            self.db.execute("UPDATE files SET mtime_ns = ?, size = ? WHERE path = ?", (mtime_ns, size, path))

    def remove_note(self, path: str) -> None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self._delete_chunks(path)
                self.db.execute("DELETE FROM files WHERE path = ?", (path,))
                self.db.execute("DELETE FROM raw_links WHERE source = ?", (path,))
                self.db.execute("DELETE FROM links WHERE source = ? OR target = ?", (path, path))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def resolve_links(self, sources: Optional[Iterable[str]] = None) -> int:
        """Rebuild resolved link edges from raw links (all, or only for some sources)."""
        with self.lock:
            resolver = LinkResolver(r[0] for r in self.db.execute("SELECT path FROM files"))
            if sources is None:
                rows = self.db.execute("SELECT source, kind, value FROM raw_links").fetchall()
                srcs = None
            else:
                srcs = list(sources)
                rows = []
                for s in srcs:
                    rows += self.db.execute("SELECT source, kind, value FROM raw_links WHERE source = ?",
                                            (s,)).fetchall()
            edges = set()
            for src, kind, value in rows:
                tgt = resolver.resolve(kind, value)
                if tgt and tgt != src:
                    edges.add((src, tgt))
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if srcs is None:
                    self.db.execute("DELETE FROM links")
                else:
                    self.db.executemany("DELETE FROM links WHERE source = ?", [(s,) for s in srcs])
                self.db.executemany("INSERT OR IGNORE INTO links (source, target) VALUES (?, ?)", sorted(edges))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            return len(edges)

    def clear(self) -> None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            for t in ("files", "chunks", "raw_links", "links"):
                self.db.execute(f"DELETE FROM {t}")
            if self.fts_ok:
                self.db.execute("DELETE FROM chunks_fts")
            if self.has_vec_table():
                self.db.execute("DROP TABLE chunks_vec")
            self.db.execute("DELETE FROM meta WHERE key IN ('dim', 'model')")
            self.db.execute("COMMIT")

    # -- reads -----------------------------------------------------------------

    def files(self) -> Dict[str, Tuple[int, int, str]]:
        return {r[0]: (r[1], r[2], r[3]) for r in self.q("SELECT path, mtime_ns, size, hash FROM files")}

    def chunk_rows(self, ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        ids = list(ids)
        out: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            qm = ",".join("?" * len(part))
            for r in self.q(f"SELECT c.id, c.path, c.heading, c.content, f.type, f.tags FROM chunks c "
                            f"LEFT JOIN files f ON f.path = c.path WHERE c.id IN ({qm})", part):
                out[r[0]] = {"id": r[0], "path": r[1], "heading": r[2], "content": r[3],
                             "type": r[4] or "note", "tags": json.loads(r[5] or "[]")}
        return out

    @staticmethod
    def _filter_sql(note_type: Optional[str], folder: Optional[str], tag: Optional[str]) -> Tuple[str, List[Any]]:
        conds, params = [], []
        if note_type:
            conds.append("f.type = ?")
            params.append(note_type.lower())
        if folder:
            conds.append("(c.path LIKE ? ESCAPE '\\')")
            esc = norm_path(folder).rstrip("/").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(esc + "/%")
        if tag:
            conds.append("EXISTS (SELECT 1 FROM json_each(f.tags) WHERE json_each.value = ?)")
            params.append(tag.lstrip("#").lower())
        return (" AND ".join(conds), params)

    def vector_search(self, emb: Sequence[float], k: int, note_type=None, folder=None, tag=None
                      ) -> List[Tuple[str, float]]:
        if not self.vec_ok or not self.has_vec_table():
            return []
        where, params = self._filter_sql(note_type, folder, tag)
        if not where:
            rows = self.q("SELECT id, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? "
                          "ORDER BY distance", (_f32(emb), k))
        else:
            # Exact search over the filtered subset: no candidates lost to post-filtering.
            rows = self.q(f"SELECT c.id, vec_distance_cosine(v.embedding, ?) AS d FROM chunks c "
                          f"JOIN files f ON f.path = c.path JOIN chunks_vec v ON v.id = c.id "
                          f"WHERE {where} ORDER BY d LIMIT ?", [_f32(emb)] + params + [k])
        return [(r[0], 1.0 - r[1]) for r in rows if r[1] is not None]

    def keyword_search(self, query: str, k: int, note_type=None, folder=None, tag=None
                       ) -> List[Tuple[str, float]]:
        if not self.fts_ok:
            return []
        match = build_fts_query(query)
        if not match:
            return []
        where, params = self._filter_sql(note_type, folder, tag)
        extra = f" AND {where}" if where else ""
        try:
            rows = self.q(f"SELECT c.id, bm25(chunks_fts, 0.0, 4.0, 2.0, 1.0, 2.0) AS s FROM chunks_fts "
                          f"JOIN chunks c ON c.id = chunks_fts.id LEFT JOIN files f ON f.path = c.path "
                          f"WHERE chunks_fts MATCH ?{extra} ORDER BY s LIMIT ?", [match] + params + [k])
        except sqlite3.OperationalError as e:
            log.debug("keyword search failed: %s", e)
            return []
        return [(r[0], r[1]) for r in rows]

    def similarity(self, ids: Sequence[str], emb: Sequence[float]) -> Dict[str, float]:
        if not ids or not self.vec_ok or not self.has_vec_table():
            return {}
        qm = ",".join("?" * len(ids))
        return {r[0]: 1.0 - r[1] for r in self.q(
            f"SELECT id, vec_distance_cosine(embedding, ?) FROM chunks_vec WHERE id IN ({qm})",
            [_f32(emb)] + list(ids)) if r[1] is not None}

    def note_vector(self, path: str) -> Optional[List[float]]:
        if not self.vec_ok or not self.has_vec_table():
            return None
        vecs = [_unf32(r[0]) for r in self.q(
            "SELECT v.embedding FROM chunks c JOIN chunks_vec v ON v.id = c.id WHERE c.path = ?", (path,))]
        if not vecs:
            return None
        n = len(vecs)
        return [sum(col) / n for col in zip(*vecs)]

    def links(self, path: str) -> List[str]:
        return sorted(r[0] for r in self.q("SELECT target FROM links WHERE source = ?", (path,)))

    def backlinks(self, path: str) -> List[str]:
        return sorted(r[0] for r in self.q("SELECT source FROM links WHERE target = ?", (path,)))

    def has_file(self, path: str) -> bool:
        return bool(self.q("SELECT 1 FROM files WHERE path = ?", (path,)))

    def first_chunk(self, path: str) -> str:
        r = self.q("SELECT content FROM chunks WHERE path = ? ORDER BY ord LIMIT 1", (path,))
        return r[0][0] if r else ""

    def stats(self) -> Dict[str, Any]:
        files = self.q("SELECT COUNT(*) FROM files")[0][0]
        chunks = self.q("SELECT COUNT(*) FROM chunks")[0][0]
        vectors = self.q("SELECT COUNT(*) FROM chunks_vec")[0][0] if self.vec_ok and self.has_vec_table() else 0
        edges = self.q("SELECT COUNT(*) FROM links")[0][0]
        types = dict(self.q("SELECT COALESCE(type, 'note'), COUNT(*) FROM files GROUP BY 1"))
        return {"notes": files, "chunks": chunks, "chunks_with_vectors": vectors, "link_edges": edges,
                "notes_by_type": types, "index_model": self.meta_get("model"),
                "vector_search": self.vec_ok, "keyword_search": self.fts_ok,
                "vector_error": self.vec_error, "keyword_error": self.fts_error}


# =============================================================================
# Indexer and watcher
# =============================================================================

class Indexer:
    def __init__(self, cfg: Config, store: Store, embedder: OllamaEmbedder):
        self.cfg = cfg
        self.store = store
        self.embedder = embedder
        self.write_lock = threading.Lock()
        self.state: Dict[str, Any] = {"running": False, "last_sync": None, "last_error": None,
                                      "last_result": None, "progress": None}

    # -- scanning --------------------------------------------------------------

    def scan(self) -> Dict[str, Tuple[int, int]]:
        """All Markdown files of the vault: relative posix path -> (mtime_ns, size)."""
        root = self.cfg.vault
        exclude = {x.lower() for x in self.cfg.get("index", "exclude_folders")}
        out: Dict[str, Tuple[int, int]] = {}
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        name = e.name
                        if name.startswith("."):
                            continue
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if name.lower() not in exclude:
                                    stack.append(Path(e.path))
                            elif name.lower().endswith(".md") and e.is_file():
                                st = e.stat()
                                rel = Path(e.path).relative_to(root).as_posix()
                                out[rel] = (st.st_mtime_ns, st.st_size)
                        except OSError:
                            continue
            except OSError:
                continue
        return out

    def _check_model(self) -> None:
        idx_model = self.store.meta_get("model")
        if idx_model and idx_model != self.embedder.model:
            raise EmbeddingError(
                f"Index was built with model '{idx_model}', config says '{self.embedder.model}'. "
                f"Rebuild the index (tool rebuild_index or: obsidian_rag.py index --full).")

    # -- single file -----------------------------------------------------------

    def index_file(self, rel: str, stat: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
        """(Re)index one note. Embeds only chunks whose text is new. Raises on embedding errors
        without touching the stored version, so a failing Ollama never loses data."""
        rel = norm_path(rel)
        absf = self.cfg.vault / rel
        raw = absf.read_bytes()
        st = absf.stat()
        mtime_ns, size = stat or (st.st_mtime_ns, st.st_size)
        fhash = hashlib.sha1(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        note = parse_note(rel, text, self.cfg)
        vectors: Dict[str, bytes] = {}
        embedded = 0
        if self.store.vec_ok and note.chunks:
            self._check_model()
            vectors = self.store.vectors_by_chash(c.chash for c in note.chunks)
            todo = [c for c in note.chunks if c.chash not in vectors]
            unique = list({c.chash: c for c in todo}.values())
            if unique:
                embs = self.embedder.embed([c.embed_text for c in unique])
                self.store.ensure_vec_table(len(embs[0]), self.embedder.model)
                for c, e in zip(unique, embs):
                    vectors[c.chash] = _f32(e)
                embedded = len(unique)
        self.store.replace_note(note, vectors, mtime_ns, size, fhash)
        return {"path": rel, "chunks": len(note.chunks), "embedded": embedded}

    # -- whole vault -----------------------------------------------------------

    def sync(self, full: bool = False, stable_seconds: float = 0.0,
             progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, Any]:
        """Bring the index in line with the vault. Cheap when nothing changed."""
        with self.write_lock:
            self.state["running"] = True
            t0 = time.monotonic()
            try:
                if full:
                    self.store.clear()
                disk = self.scan()
                known = self.store.files()
                now_ns = time.time_ns()
                changed = [p for p, st in disk.items()
                           if p not in known or (known[p][0], known[p][1]) != st]
                if stable_seconds:
                    changed = [p for p in changed if now_ns - disk[p][0] >= stable_seconds * 1e9]
                # changed/new first (their vectors can come from moved notes), removals after
                removed = [p for p in known if p not in disk]
                done = embedded = unchanged = 0
                errors: List[Dict[str, str]] = []
                for i, p in enumerate(sorted(changed), 1):
                    self.state["progress"] = f"{i}/{len(changed)} {p}"
                    if progress:
                        progress(i, len(changed), p)
                    try:
                        if p in known:
                            h = hashlib.sha1((self.cfg.vault / p).read_bytes()).hexdigest()
                            if h == known[p][2]:
                                self.store.touch_file(p, *disk[p])
                                unchanged += 1
                                continue
                        r = self.index_file(p, disk[p])
                        done += 1
                        embedded += r["embedded"]
                    except EmbeddingError as e:
                        errors.append({"path": p, "error": str(e)})
                        if "not reachable" in str(e) or "Rebuild the index" in str(e):
                            break   # no point in trying the remaining files now
                    except (OSError, UnicodeError) as e:
                        errors.append({"path": p, "error": str(e)})
                for p in removed:
                    self.store.remove_note(p)
                if done or removed or full:
                    edges = self.store.resolve_links()
                else:
                    edges = None
                result = {"notes_on_disk": len(disk), "indexed": done, "unchanged": unchanged,
                          "removed": len(removed), "chunks_embedded": embedded,
                          "link_edges": edges, "seconds": round(time.monotonic() - t0, 2),
                          "errors": errors or None}
                self.state["last_result"] = result
                self.state["last_error"] = errors[0]["error"] if errors else None
                self.state["last_sync"] = datetime.now().isoformat(timespec="seconds")
                return result
            finally:
                self.state["running"] = False
                self.state["progress"] = None

    def index_one_now(self, rel: str) -> Dict[str, Any]:
        """Index a note right after a write tool changed it."""
        rel = norm_path(rel)
        if not self.write_lock.acquire(timeout=5):
            return {"indexed": False, "index_note": "indexing is busy; the watcher picks this note up shortly"}
        try:
            r = self.index_file(rel)
            self.store.resolve_links()
            return {"indexed": True, **r}
        except Exception as e:  # noqa: BLE001
            return {"indexed": False, "index_error": f"{e} (the watcher retries automatically)"}
        finally:
            self.write_lock.release()

    def remove_one_now(self, rel: str) -> None:
        if not self.write_lock.acquire(timeout=5):
            return  # the watcher notices the missing file on its next round
        try:
            self.store.remove_note(norm_path(rel))
            self.store.resolve_links()
        finally:
            self.write_lock.release()


class Watcher(threading.Thread):
    """Polls the vault (cheap stat calls) and syncs changed notes. Survives Ollama outages."""

    def __init__(self, indexer: Indexer, interval: float = 5.0):
        super().__init__(daemon=True, name="vault-watcher")
        self.indexer = indexer
        self.interval = max(1.0, float(interval))
        self.stop_event = threading.Event()
        self.paused = threading.Event()

    def run(self) -> None:
        log.info("watcher started (every %.0fs)", self.interval)
        first = True
        backoff = self.interval
        while not self.stop_event.is_set():
            if not self.paused.is_set():
                try:
                    r = self.indexer.sync(stable_seconds=0 if first else 2)
                    if r["indexed"] or r["removed"]:
                        log.info("synced: %s indexed, %s removed, %s chunks embedded",
                                 r["indexed"], r["removed"], r["chunks_embedded"])
                    if r["errors"]:
                        log.warning("sync errors: %s", r["errors"][0]["error"])
                        backoff = min(backoff * 2, 300)
                    else:
                        backoff = self.interval
                    first = False
                except Exception as e:  # noqa: BLE001
                    log.exception("sync failed: %s", e)
                    self.indexer.state["last_error"] = str(e)
                    backoff = min(backoff * 2, 300)
            self.stop_event.wait(backoff)


# =============================================================================
# Engine: search + vault tools
# =============================================================================

class VaultError(ValueError):
    pass


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.embedder = OllamaEmbedder(cfg.get("embedding", "url"), cfg.get("embedding", "model"),
                                       cfg.get("embedding", "batch_size"), cfg.get("embedding", "timeout"))
        self.indexer = Indexer(cfg, self.store, self.embedder)
        self.watcher: Optional[Watcher] = None
        self.rebuild_state: Dict[str, Any] = {"running": False, "started": None, "finished": None,
                                              "result": None}

    # -- helpers ---------------------------------------------------------------

    def _hit(self, row: Dict[str, Any], sim: Optional[float], match: str) -> Dict[str, Any]:
        mc = int(self.cfg.get("search", "max_chars"))
        text = row["content"]
        return {"path": row["path"], "heading": row["heading"],
                "text": text if len(text) <= mc else text[:mc] + " …",
                "similarity": None if sim is None else round(sim, 3), "match": match,
                "type": row["type"], "tags": row["tags"]}

    def _diversify(self, ranked: List[str], rows: Dict[str, Dict], limit: int) -> List[str]:
        """Best chunk of every note first, then more chunks per note up to max_per_note."""
        cap = max(1, int(self.cfg.get("search", "max_per_note")))
        per: Dict[str, int] = {}
        first, rest = [], []
        for cid in ranked:
            if cid not in rows:
                continue
            p = rows[cid]["path"]
            if p not in per:
                per[p] = 1
                first.append(cid)
            elif per[p] < cap:
                per[p] += 1
                rest.append(cid)
        chosen = set((first + rest)[:limit])
        return [c for c in ranked if c in chosen]

    def note_path(self, path: str) -> str:
        p = norm_path(path)
        if p and not p.lower().endswith(".md"):
            p += ".md"
        return p

    # -- search ----------------------------------------------------------------

    def search(self, query: str, limit: Optional[int] = None, note_type: Optional[str] = None,
               folder: Optional[str] = None, tag: Optional[str] = None, expand: int = 0,
               mode: str = "hybrid") -> Dict[str, Any]:
        limit = max(1, int(limit or self.cfg.get("search", "limit")))
        threshold = float(self.cfg.get("search", "similarity_threshold"))
        if not (query or "").strip():
            return {"results": [], "notes": ["Empty query."]}
        notes: List[str] = []
        sem: List[Tuple[str, float]] = []
        kw: List[Tuple[str, float]] = []
        emb = None
        if mode in ("hybrid", "semantic") and self.store.vec_ok and self.store.has_vec_table():
            try:
                idx_model = self.store.meta_get("model")
                if idx_model and idx_model != self.embedder.model:
                    raise EmbeddingError(f"index model '{idx_model}' differs from config '{self.embedder.model}'"
                                         f" - run rebuild_index")
                emb = self.embedder.embed([query])[0]
                sem = [(i, s) for i, s in self.store.vector_search(emb, limit * 4, note_type, folder, tag)
                       if s >= threshold]
            except EmbeddingError as e:
                if mode == "semantic":
                    return {"results": [], "notes": [str(e)]}
                notes.append(f"semantic search skipped: {e}")
        if mode in ("hybrid", "keyword"):
            kw = self.store.keyword_search(query, limit * 4, note_type, folder, tag)

        k = int(self.cfg.get("search", "rrf_k"))
        score: Dict[str, float] = {}
        for rank, (cid, _) in enumerate(sem):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank + 1)
        for rank, (cid, _) in enumerate(kw):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank + 1)
        fused = sorted(score, key=lambda c: score[c], reverse=True)
        rows = self.store.chunk_rows(fused)
        ranked = self._diversify(fused, rows, limit)

        sims = dict(sem)
        if emb is not None:
            missing = [c for c in ranked if c not in sims]
            sims.update(self.store.similarity(missing, emb))
        sem_ids, kw_ids = {c for c, _ in sem}, {c for c, _ in kw}
        results = []
        for cid in ranked:
            match = "both" if cid in sem_ids and cid in kw_ids else ("keyword" if cid in kw_ids else "semantic")
            results.append(self._hit(rows[cid], sims.get(cid), match))
        out: Dict[str, Any] = {"results": results}
        if expand and results:
            seeds = list(dict.fromkeys(r["path"] for r in results))
            out["linked_notes"] = [
                {**n, "preview": self.store.first_chunk(n["path"])[:300]}
                for n in self.neighbors(seeds, int(expand), limit * 2)]
        if not results and not notes:
            notes.append("No matching notes. Try other words, or keyword_search for exact names.")
        if notes:
            out["notes"] = notes
        return out

    def neighbors(self, seeds: Sequence[str], hops: int, limit: int) -> List[Dict[str, Any]]:
        visited = set(seeds)
        queue = deque((s, 0) for s in seeds)
        out: List[Dict[str, Any]] = []
        while queue and len(out) < limit:
            p, d = queue.popleft()
            if d >= hops:
                continue
            for direction, nexts in (("link", self.store.links(p)), ("backlink", self.store.backlinks(p))):
                for n in nexts:
                    if n in visited:
                        continue
                    visited.add(n)
                    out.append({"path": n, "hop": d + 1, "via": p, "direction": direction})
                    queue.append((n, d + 1))
                    if len(out) >= limit:
                        return out
        return out

    def note_links(self, path: str, hops: int = 1, limit: int = 30) -> Dict[str, Any]:
        p = self.note_path(path)
        if not self.store.has_file(p):
            return {"error": f"Note not found in index: {p}"}
        out: Dict[str, Any] = {"path": p, "links": self.store.links(p), "backlinks": self.store.backlinks(p)}
        if hops > 1:
            out["further"] = [n for n in self.neighbors([p], hops, limit) if n["hop"] > 1]
        return out

    def similar(self, path: str, limit: Optional[int] = None) -> Dict[str, Any]:
        p = self.note_path(path)
        limit = int(limit or self.cfg.get("search", "limit"))
        vec = self.store.note_vector(p)
        if vec is None:
            return {"error": f"Note not found in index (or no vectors yet): {p}"}
        hits = self.store.vector_search(vec, limit * 8 + 10)
        rows = self.store.chunk_rows(c for c, _ in hits)
        best: Dict[str, Tuple[float, str]] = {}
        for cid, sim in hits:
            r = rows.get(cid)
            if not r or r["path"] == p:
                continue
            if r["path"] not in best or sim > best[r["path"]][0]:
                best[r["path"]] = (sim, cid)
        top = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[:limit]
        return {"path": p, "similar": [{"path": fp, "heading": rows[cid]["heading"], "similarity": round(s, 3),
                                        "preview": rows[cid]["content"][:200]} for fp, (s, cid) in top]}

    # -- vault access ----------------------------------------------------------

    def _safe(self, path: str, must_exist: bool) -> Tuple[Path, str]:
        rel = self.note_path(path)
        if not rel or rel.startswith("..") or re.match(r"^[a-zA-Z]:", rel):
            raise VaultError(f"Invalid note path: {path!r} (use a path relative to the vault)")
        if any(part.startswith(".") for part in rel.split("/")):
            raise VaultError("Paths with hidden folders (e.g. .obsidian, .trash) are not allowed")
        root = self.cfg.vault.resolve()
        absf = (root / rel).resolve()
        if root != absf and root not in absf.parents:
            raise VaultError("Path leaves the vault")
        if must_exist and not absf.is_file():
            raise VaultError(f"Note does not exist: {rel}")
        return absf, rel

    def read_note(self, path: str, with_context: bool = False) -> Dict[str, Any]:
        absf, rel = self._safe(path, must_exist=True)
        out: Dict[str, Any] = {"path": rel, "content": absf.read_text(encoding="utf-8", errors="replace")}
        if with_context:
            out["links"] = self.store.links(rel)
            out["backlinks"] = self.store.backlinks(rel)
            out["similar"] = self.similar(rel, 3).get("similar", [])
        return out

    @staticmethod
    def _read_exact(absf: Path) -> str:
        """Read without newline translation, so CRLF files keep their line endings."""
        with open(absf, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def _write(self, absf: Path, text: str) -> None:
        absf.parent.mkdir(parents=True, exist_ok=True)
        tmp = absf.with_name(absf.name + ".tmp-obsidian-rag")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        for attempt in range(5):   # Windows: another program may hold the file for a moment
            try:
                os.replace(tmp, absf)
                return
            except PermissionError:
                if attempt == 4:
                    tmp.unlink(missing_ok=True)
                    raise
                time.sleep(0.2 * (attempt + 1))

    def write_note(self, path: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
        absf, rel = self._safe(path, must_exist=False)
        if absf.exists() and not overwrite:
            raise VaultError(f"Note already exists: {rel}. Use append_to_note / replace_in_note, "
                             f"or overwrite=true to replace it completely.")
        self._write(absf, content)
        return {"written": rel, **self.indexer.index_one_now(rel)}

    def append_to_note(self, path: str, text: str, heading: Optional[str] = None) -> Dict[str, Any]:
        absf, rel = self._safe(path, must_exist=True)
        content = self._read_exact(absf)
        nl = "\r\n" if "\r\n" in content else "\n"
        block = text.strip("\r\n").replace("\r\n", "\n").split("\n")
        if not heading:
            content = content.rstrip("\r\n") + nl + nl + nl.join(block) + nl
        else:
            lines = content.split(nl)
            target = heading.strip().lstrip("#").strip().lower()
            start = level = None
            end = len(lines)
            fence = None
            for i, line in enumerate(lines):
                m_f = _FENCE.match(line)
                if fence:
                    if m_f and m_f.group(1)[0] == fence[0]:
                        fence = None
                    continue
                if m_f:
                    fence = m_f.group(1)
                    continue
                m = _HEADING.match(line)
                if not m:
                    continue
                if start is None:
                    if m.group(2).strip().lower() == target:
                        start, level = i, len(m.group(1))
                elif len(m.group(1)) <= level:
                    end = i
                    break
            if start is None:
                raise VaultError(f"Heading not found in {rel}: {heading!r}")
            insert_at = end
            while insert_at - 1 > start and not lines[insert_at - 1].strip():
                insert_at -= 1
            tail = [""] + lines[end:] if end < len(lines) else []
            content = nl.join(lines[:insert_at] + block + tail)
            if not content.endswith(nl):
                content += nl
        self._write(absf, content)
        return {"updated": rel, **self.indexer.index_one_now(rel)}

    def replace_in_note(self, path: str, old: str, new: str) -> Dict[str, Any]:
        absf, rel = self._safe(path, must_exist=True)
        content = self._read_exact(absf)
        count = content.count(old) if old else 0
        if count == 0 and "\r\n" in content and "\n" in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = content.count(old)
        if count == 0:
            raise VaultError("Text not found. Copy it exactly from read_note (including spaces).")
        if count > 1:
            raise VaultError(f"Text occurs {count} times. Include more surrounding text so it is unique.")
        self._write(absf, content.replace(old, new, 1))
        return {"updated": rel, **self.indexer.index_one_now(rel)}

    def move_note(self, path: str, new_path: str) -> Dict[str, Any]:
        src, rel = self._safe(path, must_exist=True)
        dst, new_rel = self._safe(new_path, must_exist=False)
        if dst.exists():
            raise VaultError(f"Target exists: {new_rel}")
        linking = self.store.backlinks(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        res = self.indexer.index_one_now(new_rel)      # first: reuses the stored vectors
        self.indexer.remove_one_now(rel)
        out = {"moved": rel, "to": new_rel, **res}
        old_name, new_name = posixpath.splitext(posixpath.basename(rel))[0], \
            posixpath.splitext(posixpath.basename(new_rel))[0]
        if linking and old_name != new_name:
            out["update_links_in"] = linking
            out["hint"] = (f"These notes link to [[{old_name}]]. Obsidian only updates links for moves made "
                           f"inside Obsidian - replace the links with [[{new_name}]].")
        return out

    def list_notes(self, folder: str = "", recent: bool = False, limit: int = 50) -> Dict[str, Any]:
        folder = norm_path(folder).rstrip("/")
        rows = self.store.q("SELECT path, mtime_ns, type FROM files")
        if folder:
            rows = [r for r in rows if r[0].lower().startswith(folder.lower() + "/")]
        rows.sort(key=(lambda r: -r[1]) if recent else (lambda r: r[0].lower()))
        return {"count": len(rows), "notes": [
            {"path": r[0], "type": r[2],
             "modified": datetime.fromtimestamp(r[1] / 1e9).isoformat(timespec="minutes")}
            for r in rows[:max(1, int(limit))]]}

    # -- maintenance -----------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {"version": __version__, "vault": str(self.cfg.vault), "vault_exists": self.cfg.vault.is_dir(),
                "config": str(self.cfg.path), "model": self.embedder.model, **self.store.stats(),
                "model_mismatch": bool(self.store.meta_get("model") and
                                       self.store.meta_get("model") != self.embedder.model),
                "sync": {k: v for k, v in self.indexer.state.items()},
                "watcher": bool(self.watcher and self.watcher.is_alive()),
                "rebuild": dict(self.rebuild_state)}

    def rebuild(self) -> Dict[str, Any]:
        if self.rebuild_state["running"]:
            return {"status": "already_running", "started": self.rebuild_state["started"]}

        def work():
            self.rebuild_state.update(running=True, started=datetime.now().isoformat(timespec="seconds"),
                                      finished=None, result=None)
            if self.watcher:
                self.watcher.paused.set()
            try:
                self.rebuild_state["result"] = self.indexer.sync(full=True)
            except Exception as e:  # noqa: BLE001
                self.rebuild_state["result"] = {"error": str(e)}
            finally:
                if self.watcher:
                    self.watcher.paused.clear()
                self.rebuild_state.update(running=False, finished=datetime.now().isoformat(timespec="seconds"))

        threading.Thread(target=work, daemon=True, name="rebuild").start()
        return {"status": "started", "hint": "Progress in index_status (sync.progress). Search results are "
                                             "incomplete until it has finished."}

    def start_watcher(self) -> None:
        if self.cfg.get("index", "watch") and not self.watcher:
            self.watcher = Watcher(self.indexer, self.cfg.get("index", "watch_interval"))
            self.watcher.start()


# =============================================================================
# MCP server
# =============================================================================

INSTRUCTIONS = """Local search and safe editing for the user's Obsidian vault.
Work token-efficiently: call `search` first and read only the notes you need (read_note).
Never read whole folders. Use `keyword_search` for exact names, IDs or model names.
Use note_links / similar_notes for context. Write tools re-index the note at once.
Paths are relative to the vault; "/" and "\\" both work. Nothing is ever deleted."""


def build_server(engine: Engine):
    try:
        from mcp.server import MCPServer as Server  # mcp >= 2
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server  # type: ignore  # mcp 1.x
    try:
        from mcp.types import ToolAnnotations
        ro = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
        rw = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    except Exception:  # noqa: BLE001
        ro = rw = None

    mcp = Server("obsidian-rag-windows", instructions=INSTRUCTIONS)

    def tool(annotations):
        return mcp.tool(annotations=annotations) if annotations is not None else mcp.tool()

    def safe(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        try:
            return fn()
        except VaultError as e:
            return {"error": str(e)}

    @tool(ro)
    def search(query: str, limit: Optional[int] = None, note_type: Optional[str] = None,
               folder: Optional[str] = None, tag: Optional[str] = None, expand: int = 0) -> dict:
        """Search the vault by meaning AND exact words (hybrid). Start here.

        Args:
            query: A question or search words, any language
            limit: Max results (default from config, usually 5)
            note_type: Filter, e.g. "daily" or a frontmatter type
            folder: Only notes below this folder, e.g. "Projects"
            tag: Only notes with this tag (without #)
            expand: 1 = also list notes linked to the hits
        """
        return engine.search(query, limit, note_type, folder, tag, expand, "hybrid")

    @tool(ro)
    def keyword_search(query: str, limit: Optional[int] = None, note_type: Optional[str] = None,
                       folder: Optional[str] = None, tag: Optional[str] = None) -> dict:
        """Exact-word search (BM25): every word must occur. For names, IDs, model names.

        Args:
            query: Words or a name, e.g. "bge-m3" or "Port-Forwarding"
            limit: Max results
            note_type: Optional type filter
            folder: Optional folder filter
            tag: Optional tag filter
        """
        return engine.search(query, limit, note_type, folder, tag, 0, "keyword")

    @tool(ro)
    def read_note(path: str, with_context: bool = False) -> dict:
        """Full text of one note. with_context=true adds links, backlinks and similar notes.

        Args:
            path: Vault-relative path, e.g. "Projects/Plan.md" (".md" optional)
            with_context: Also return links, backlinks and similar notes
        """
        return safe(lambda: engine.read_note(path, with_context))

    @tool(ro)
    def note_links(path: str, hops: int = 1) -> dict:
        """Outgoing links and backlinks of a note; hops=2 adds notes two steps away.

        Args:
            path: Vault-relative path
            hops: 1 or more
        """
        return engine.note_links(path, hops)

    @tool(ro)
    def similar_notes(path: str, limit: Optional[int] = None) -> dict:
        """Notes closest in meaning to a given note (one entry per note).

        Args:
            path: Vault-relative path
            limit: Number of notes
        """
        return engine.similar(path, limit)

    @tool(ro)
    def list_notes(folder: str = "", recent: bool = False, limit: int = 50) -> dict:
        """List notes (paths only), alphabetically or most recently modified first.

        Args:
            folder: Optional folder, e.g. "Journal"
            recent: true = newest first
            limit: Max entries
        """
        return engine.list_notes(folder, recent, limit)

    @tool(ro)
    def index_status() -> dict:
        """Index statistics, watcher state, last sync result and rebuild progress."""
        return engine.status()

    @tool(rw)
    def rebuild_index() -> dict:
        """Rebuild the whole index in the background (after a model change). Returns at once."""
        return engine.rebuild()

    if engine.cfg.get("vault", "allow_write"):
        @tool(rw)
        def write_note(path: str, content: str, overwrite: bool = False) -> dict:
            """Create a new note. Refuses to replace an existing note unless overwrite=true.

            Args:
                path: Vault-relative path, folders are created as needed
                content: Full Markdown content
                overwrite: Replace an existing note completely (use with care)
            """
            return safe(lambda: engine.write_note(path, content, overwrite))

        @tool(rw)
        def append_to_note(path: str, text: str, heading: Optional[str] = None) -> dict:
            """Append text to a note: at the end, or at the end of the section under `heading`.

            Args:
                path: Vault-relative path of an existing note
                text: Markdown to add
                heading: Optional heading text (without #) whose section gets the text
            """
            return safe(lambda: engine.append_to_note(path, text, heading))

        @tool(rw)
        def replace_in_note(path: str, old: str, new: str) -> dict:
            """Replace one exact, unique piece of text in a note.

            Args:
                path: Vault-relative path
                old: Exact text to replace (must occur exactly once)
                new: Replacement text
            """
            return safe(lambda: engine.replace_in_note(path, old, new))

        @tool(rw)
        def move_note(path: str, new_path: str) -> dict:
            """Move or rename a note (never overwrites). Reports notes whose links need updating.

            Args:
                path: Current vault-relative path
                new_path: New vault-relative path
            """
            return safe(lambda: engine.move_note(path, new_path))

    return mcp


# =============================================================================
# Claude Desktop registration
# =============================================================================

def claude_config_candidates() -> List[Path]:
    """claude_desktop_config.json locations: classic install and Microsoft Store / MSIX package."""
    out: List[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        local = os.environ.get("LOCALAPPDATA")
        if local and (Path(local) / "Packages").is_dir():
            for pkg in sorted((Path(local) / "Packages").glob("Claude_*")):
                out.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    elif sys.platform == "darwin":
        out.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        out.append(Path.home() / ".config" / "Claude" / "claude_desktop_config.json")
    return out


def claude_config_targets(explicit: Optional[str] = None) -> List[Path]:
    if explicit:
        return [Path(explicit)]
    cands = claude_config_candidates()
    existing = [c for c in cands if c.exists()]
    if existing:
        return existing
    # Nothing yet: prefer the MSIX package folder if Claude is installed that way.
    for c in reversed(cands):
        if c.parent.parent.parent.is_dir() and "Packages" in c.parts:
            return [c]
    return cands[:1]


def register_claude(name: str, command: str, args: List[str], env: Dict[str, str],
                    config: Optional[str] = None, remove: bool = False) -> List[Dict[str, str]]:
    """Add (or remove) this server in claude_desktop_config.json, keeping everything else intact."""
    results = []
    for target in claude_config_targets(config):
        data: Dict[str, Any] = {}
        if target.exists():
            raw = target.read_text(encoding="utf-8-sig").strip()
            if raw:
                data = json.loads(raw)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = target.with_name(f"{target.name}.bak-{stamp}")
            backup.write_bytes(target.read_bytes())
        else:
            backup = None
        servers = data.setdefault("mcpServers", {})
        if remove:
            existed = servers.pop(name, None) is not None
            action = "removed" if existed else "not present"
        else:
            action = "updated" if name in servers else "added"
            servers[name] = {"command": command, "args": args, "env": env}
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, target)
        results.append({"file": str(target), "action": action, "backup": str(backup) if backup else None})
    return results


def find_uv() -> Optional[str]:
    import shutil
    for cand in (shutil.which("uv"), str(Path.home() / ".local" / "bin" / ("uv.exe" if sys.platform == "win32"
                                                                             else "uv"))):
        if cand and Path(cand).exists():
            return str(Path(cand).resolve())
    return None


# =============================================================================
# Command line
# =============================================================================

def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(prog="obsidian_rag.py", description=f"{APP} {__version__}")
    ap.add_argument("--config", help="Path to config.toml (default: app folder)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="Run the MCP server on stdio (default)")
    p = sub.add_parser("init", help="Create the config file")
    p.add_argument("--vault", required=True)
    p.add_argument("--model", default=DEFAULTS["embedding"]["model"])
    p.add_argument("--force", action="store_true", help="Overwrite an existing config")
    p = sub.add_parser("index", help="Index new and changed notes")
    p.add_argument("--full", action="store_true", help="Rebuild from scratch")
    sub.add_parser("watch", help="Keep the index up to date in the foreground")
    sub.add_parser("check", help="Self-test: vault, Ollama, model, SQLite features, index")
    sub.add_parser("status", help="Index statistics")
    p = sub.add_parser("register", help="Add this server to Claude Desktop (keeps a backup)")
    p.add_argument("--name", default="obsidian-rag")
    p.add_argument("--claude-config", help="Path to claude_desktop_config.json (auto-detected)")
    p.add_argument("--uv", help="Path to uv.exe (auto-detected)")
    p.add_argument("--remove", action="store_true", help="Remove the entry instead")
    p = sub.add_parser("search", help="Search from the terminal")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int)
    p.add_argument("--mode", choices=["hybrid", "semantic", "keyword"], default="hybrid")
    p.add_argument("--type", dest="note_type")
    p.add_argument("--folder")
    p.add_argument("--tag")
    args = ap.parse_args(argv)
    cmd = args.cmd or "serve"

    if cmd == "init":
        path = Path(args.config or os.environ.get("OBSIDIAN_RAG_CONFIG") or app_home() / "config.toml")
        vault = Path(args.vault).expanduser()
        if not vault.is_dir():
            print(f"Vault folder not found: {vault}", file=sys.stderr)
            return 2
        if path.exists() and not args.force:
            print(f"Config exists, keeping it: {path} (use --force to replace)", file=sys.stderr)
            return 0
        write_default_config(path, str(vault.resolve()), args.model)
        print(f"Config written: {path}", file=sys.stderr)
        return 0

    if cmd == "register":
        uv = args.uv or find_uv()
        if not uv and not args.remove:
            print("uv not found. Install it first: https://docs.astral.sh/uv/", file=sys.stderr)
            return 2
        home = str(Path(args.config).parent if args.config else app_home())
        res = register_claude(args.name, uv or "", ["run", "--script", str(Path(__file__).resolve())],
                              {"OBSIDIAN_RAG_HOME": home}, args.claude_config, args.remove)
        _print(res)
        return 0

    cfg = Config.load(args.config)
    setup_logging(cfg.home, args.verbose)
    if not cfg.vault.is_dir():
        log.error("Vault folder not found: %s (edit %s)", cfg.vault, cfg.path)
        if cmd != "check":
            return 2
    engine = Engine(cfg)

    if cmd == "serve":
        engine.start_watcher()
        build_server(engine).run(transport="stdio")
        return 0

    if cmd == "index":
        last = [0.0]

        def progress(i, n, path):
            if time.monotonic() - last[0] > 0.5 or i == n:
                last[0] = time.monotonic()
                print(f"\r  {i}/{n}  {path[:70]:<70}", end="", file=sys.stderr, flush=True)

        r = engine.indexer.sync(full=args.full, progress=progress)
        print(file=sys.stderr)
        _print(r)
        return 1 if r["errors"] else 0

    if cmd == "watch":
        engine.start_watcher()
        print("Watching the vault, Ctrl+C to stop.", file=sys.stderr)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    if cmd == "status":
        _print(engine.status())
        return 0

    if cmd == "check":
        report: Dict[str, Any] = {"version": __version__, "python": sys.version.split()[0],
                                  "config": str(cfg.path), "config_found": cfg.path.exists(),
                                  "vault": str(cfg.vault), "vault_exists": cfg.vault.is_dir(),
                                  "notes_in_vault": len(engine.indexer.scan()) if cfg.vault.is_dir() else 0,
                                  "ollama": engine.embedder.check()}
        try:
            dim = len(engine.embedder.embed(["test"])[0])
            report["embedding"] = f"ok ({dim} dimensions)"
        except Exception as e:  # noqa: BLE001
            report["embedding"] = f"error: {e}"
        report["index"] = engine.store.stats()
        report["ok"] = bool(report["vault_exists"] and str(report["embedding"]).startswith("ok")
                            and report["index"]["vector_search"] and report["index"]["keyword_search"])
        _print(report)
        return 0 if report["ok"] else 1

    if cmd == "search":
        _print(engine.search(args.query, args.limit, args.note_type, args.folder, args.tag, 0, args.mode))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
