"""Test suite for obsidian_rag.py.

Runs without Ollama: a small fake server returns deterministic bag-of-words
embeddings (texts sharing words get similar vectors), so search behaviour is
predictable. Uses the example vault in tests/vault (copied to a temp folder).

    uv run --with "mcp>=1.10,<3" --with "sqlite-vec>=0.1.6,<0.2" python tests/test_obsidian_rag.py
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import obsidian_rag as rag  # noqa: E402

PASSED: list = []
FAILED: list = []


def check(name, cond, detail=""):
    (PASSED if cond else FAILED).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"{mark} {name}" + (f"\n     -> {str(detail)[:600]}" if (detail and not cond) else ""), flush=True)


def section(title):
    print(f"\n== {title}", flush=True)


# -----------------------------------------------------------------------------
# Fake Ollama
# -----------------------------------------------------------------------------

from fake_ollama import FakeOllama, fake_embed  # noqa: E402

server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
threading.Thread(target=server.serve_forever, daemon=True).start()
OLLAMA = f"http://127.0.0.1:{server.server_address[1]}"

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

work = Path(tempfile.mkdtemp(prefix="obsidian-rag-test-"))
VAULT = work / "Mein Vault"          # space in the path on purpose
shutil.copytree(HERE / "vault", VAULT)
HOME = work / "home"
os.environ["OBSIDIAN_RAG_HOME"] = str(HOME)


def make_config(extra: str = "", home: Path = HOME) -> rag.Config:
    home.mkdir(parents=True, exist_ok=True)
    cfg_path = home / "config.toml"
    rag.write_default_config(cfg_path, str(VAULT), "bge-m3")
    text = cfg_path.read_text(encoding="utf-8")
    text = text.replace('url = "http://localhost:11434"', f'url = "{OLLAMA}"')
    text = text.replace("similarity_threshold = 0.40", "similarity_threshold = 0.25")
    text = text.replace("watch_interval = 5", "watch_interval = 1")
    cfg_path.write_text(text + extra, encoding="utf-8")
    return rag.Config.load(str(cfg_path))


def paths(result):
    return [r["path"] for r in result["results"]]


# -----------------------------------------------------------------------------
section("Markdown parsing")
# -----------------------------------------------------------------------------

meta, body = rag.parse_frontmatter("\ufeff---\ntitle: \"Hallo\"\ntags: [a, 'b c']\naliases:\n  - X\n  - \"Y\"\n"
                                   "nested:\n  k: v\n---\n# Body")
check("frontmatter: BOM, quoted scalar, inline + block lists",
      meta.get("title") == "Hallo" and meta.get("tags") == ["a", "b c"] and meta.get("aliases") == ["X", "Y"]
      and body.strip() == "# Body", (meta, body))
check("frontmatter: none", rag.parse_frontmatter("# Just text")[0] == {})
check("frontmatter: unterminated is left alone", rag.parse_frontmatter("---\na: b\n# no end")[0] == {})

tags = rag.extract_tags({"tags": "Alpha, beta"}, "Text #gamma und #delta/sub\n# Heading\n`#code`\n#123\n```\n#fence\n```")
check("tags: frontmatter + inline, no code, no headings, no numbers",
      tags == ["alpha", "beta", "gamma", "delta/sub"], tags)

md = ("# Titel\n\nIntro text that is long enough to stand alone. " + "x" * 220 + "\n\n"
      "## Abschnitt A\nInhalt A " + "a" * 300 + "\n\n```\n# not a heading\ncode\n```\n\n"
      "### Unter A1\nKurz.\n\n## Leer\n\n## Abschnitt B\n" + ("Absatz B. " * 60 + "\n\n") * 5)
chunks = rag.chunk_markdown(md, "Notiz", max_chars=800, min_chars=200)
heads = [c.heading for c in chunks]
check("chunks: heading path", "Titel > Abschnitt A" in heads, heads)
check("chunks: '#' inside code fence is not a heading", not any("not a heading" in h for h in heads), heads)
check("chunks: tiny section merged into previous", not any(h.endswith("Unter A1") for h in heads)
      and any("Kurz." in c.text for c in chunks if c.heading == "Titel > Abschnitt A"), heads)
check("chunks: empty section dropped", not any(h.endswith("Leer") for h in heads), heads)
check("chunks: long section split, all within limit",
      sum(1 for h in heads if h.endswith("Abschnitt B")) >= 3 and all(len(c.text) <= 800 for c in chunks),
      [(c.heading, len(c.text)) for c in chunks])
check("chunks: embed text carries title and heading", chunks[1].embed_text.startswith("Notiz > Titel > Abschnitt A"),
      chunks[1].embed_text[:60])
check("chunks: stable content hash", rag.chunk_markdown(md, "Notiz", 800, 200)[2].chash == chunks[2].chash)

raw = rag.extract_raw_links("[[A]] [[B|alias]] [[C#Heading]] ![[pic.png]] [x](../D.md) [y](<sub/E F.md#h>) "
                            "[u](https://x.org/a.md) `[[Code]]` [[Folder/G]]", "notes/src.md")
check("links: wikilinks, aliases, anchors, embeds, md links, code ignored",
      ("name", "A") in raw and ("name", "B") in raw and ("name", "C") in raw and ("name", "pic.png") in raw
      and ("path", "D.md") in raw and ("path", "notes/sub/E F.md") in raw and ("name", "Folder/G") in raw
      and not any(v == "Code" for _, v in raw) and not any("x.org" in v for _, v in raw), raw)
res = rag.LinkResolver(["a/Note.md", "b/c/Note.md", "Folder/G.md", "D.md"])
check("resolver: shortest path for ambiguous names", res.resolve("name", "note") == "a/Note.md")
check("resolver: folder path and md path", res.resolve("name", "Folder/G") == "Folder/G.md"
      and res.resolve("path", "D.md") == "D.md")
check("resolver: attachments and unknown names unresolved",
      res.resolve("name", "pic.png") is None and res.resolve("name", "Nope") is None)
check("norm_path: backslashes, ./ and leading slash",
      rag.norm_path(".\\Netzwerk\\VLAN.md") == "Netzwerk/VLAN.md" and rag.norm_path("/a//b/") == "a/b")
check("fts query: hyphen phrase and operator safety",
      rag.build_fts_query("bge-m3") == '"bge m3"' and rag.build_fts_query('a" OR * NEAR(') == '"a" "OR" "NEAR"')
check("toml string: quotes and backslashes", rag.toml_string("C:\\it's") == '"C:\\\\it\'s"'
      and rag.toml_string("C:\\x") == "'C:\\x'")

# -----------------------------------------------------------------------------
section("Indexing")
# -----------------------------------------------------------------------------

cfg = make_config()
eng = rag.Engine(cfg)
r = eng.indexer.sync()
on_disk = sorted(p.relative_to(VAULT).as_posix() for p in VAULT.rglob("*.md")
                 if not any(part.startswith(".") for part in p.relative_to(VAULT).parts))
st = eng.status()
check("index: all notes, hidden folders skipped", r["notes_on_disk"] == len(on_disk) == st["notes"]
      and not eng.store.has_file(".trash/Alt.md"), (r, on_disk))
check("index: every chunk has a vector", st["chunks"] == st["chunks_with_vectors"] > 0, st)
check("index: model and features recorded", st["index_model"] == "bge-m3" and st["vector_search"]
      and st["keyword_search"], st)
check("index: types from frontmatter and date names",
      st["notes_by_type"].get("daily") == 2 and st["notes_by_type"].get("wissen") == 4
      and st["notes_by_type"].get("projekt") == 1, st["notes_by_type"])
check("index: links resolved", eng.store.links("Netzwerk/Router und Layer-3-Switch.md") == ["Netzwerk/VLAN.md"]
      and "Willkommen.md" in eng.store.links("Projekte/Second Brain.md"),
      (eng.store.links("Netzwerk/Router und Layer-3-Switch.md"), eng.store.links("Projekte/Second Brain.md")))

calls = eng.embedder.calls
r2 = eng.indexer.sync()
check("sync: nothing changed -> nothing embedded", r2["indexed"] == 0 and eng.embedder.calls == calls, r2)

f = VAULT / "Netzwerk" / "PoE.md"
os.utime(f, None)
time.sleep(0.01)
os.utime(f, (time.time() + 5, time.time() + 5))
r3 = eng.indexer.sync()
check("sync: touched but unchanged file -> no embedding", r3["unchanged"] == 1 and eng.embedder.calls == calls, r3)

text = f.read_text(encoding="utf-8")
f.write_text(text.replace("| 802.3bt | 60 bis 90 W |", "| 802.3bt | 60 bis 90 W |\n| UPOE | 60 W |"), encoding="utf-8")
before = eng.embedder.calls
r4 = eng.indexer.sync()
check("sync: edit re-embeds only the changed chunk", r4["indexed"] == 1 and eng.embedder.calls - before == 1,
      (r4, eng.embedder.calls - before))

before = eng.embedder.calls
(VAULT / "Archiv").mkdir()
os.replace(VAULT / "KI" / "Embeddings.md", VAULT / "Archiv" / "Embeddings.md")
r5 = eng.indexer.sync()
check("sync: moved note reuses vectors (0 embeddings)", eng.embedder.calls == before and r5["removed"] == 1, r5)
check("sync: links follow the move (by note name)",
      "Archiv/Embeddings.md" in eng.store.links("Projekte/Second Brain.md")
      and "KI/Embeddings.md" not in eng.store.links("Projekte/Second Brain.md"),
      eng.store.links("Projekte/Second Brain.md"))
check("sync: old path gone", not eng.store.has_file("KI/Embeddings.md") and eng.store.has_file("Archiv/Embeddings.md"))
os.replace(VAULT / "Archiv" / "Embeddings.md", VAULT / "KI" / "Embeddings.md")
(VAULT / "Archiv").rmdir()
eng.indexer.sync()

(VAULT / "Temp.md").write_text("# Temp\n\nLink auf [[PoE]].", encoding="utf-8")
eng.indexer.sync()
had = "Temp.md" in eng.store.backlinks("Netzwerk/PoE.md")
(VAULT / "Temp.md").unlink()
eng.indexer.sync()
check("sync: deleted note removed incl. its links",
      had and not eng.store.has_file("Temp.md") and "Temp.md" not in eng.store.backlinks("Netzwerk/PoE.md"))

(VAULT / "Neu Später.md").write_text("Verweis auf [[Kommt Noch]].", encoding="utf-8")
eng.indexer.sync()
(VAULT / "Kommt Noch.md").write_text("# Kommt Noch\n\nJetzt da.", encoding="utf-8")
eng.indexer.sync()
check("links: link to a note created later resolves", "Kommt Noch.md" in eng.store.links("Neu Später.md"))
(VAULT / "Neu Später.md").unlink()
(VAULT / "Kommt Noch.md").unlink()
eng.indexer.sync()

# -----------------------------------------------------------------------------
section("Search")
# -----------------------------------------------------------------------------

s = eng.search("VLAN")
ps = paths(s)
keys = [(x["path"], x["heading"], x["text"]) for x in s["results"]]
check("search: VLAN note first", ps and ps[0] == "Netzwerk/VLAN.md", s)
check("search: no duplicate chunks", len(keys) == len(set(keys)), keys)
check("search: at most 2 chunks per note", max(ps.count(p) for p in ps) <= 2, ps)
check("search: forward slashes only", all("\\" not in p for p in ps))
check("search: every hit has a similarity and a match kind",
      all(x["similarity"] is not None and x["match"] in ("semantic", "keyword", "both") for x in s["results"]), s)

s = eng.search("Wie bekommt ein Access Point Strom über das Netzwerkkabel?")
check("search: paraphrase finds PoE", paths(s)[:1] == ["Netzwerk/PoE.md"], s)
s = eng.search("which GPU do I need to run large language models locally")
check("search: English question finds the note", "KI/Lokale Modelle.md" in paths(s)[:2], s)
s = eng.search("Ausbilder", note_type="daily")
check("search: type filter", s["results"] and all(x["type"] == "daily" for x in s["results"]), s)
s = eng.search("Netzwerk", folder="Journal")
check("search: folder filter", s["results"] and all(p.startswith("Journal/") for p in paths(s)), s)
s = eng.search("Netzwerk", tag="grundlagen")
check("search: tag filter", s["results"] and set(paths(s)) == {"Netzwerk/VLAN.md"}, s)
s = eng.search("Rezept Kürbissuppe Ingwer")
check("search: off-topic -> no results plus hint", s["results"] == [] and s.get("notes"), s)
s = eng.search("bge-m3")
check("search: exact term finds both notes", {"KI/Embeddings.md", "Projekte/Second Brain.md"} <= set(paths(s)), s)
s = eng.search("bge-m3", mode="keyword")
check("search: keyword mode", s["results"] and all(x["match"] == "keyword" for x in s["results"]), s)
s = eng.search("Virtual LAN", mode="keyword")
check("search: aliases are searchable", "Netzwerk/VLAN.md" in paths(s), s)
s = eng.search("netzwerk", mode="keyword", tag="netzwerk")
check("search: tags searchable and filterable", s["results"], s)
s = eng.search("VLAN", limit=2, expand=1)
linked = [n["path"] for n in s.get("linked_notes", [])]
check("search: expand adds linked notes, none repeated",
      linked and not (set(linked) & set(paths(s))) and len(linked) == len(set(linked)), s)
for q in ['"; DROP TABLE chunks; --', "NEAR( * ^ -", "", "   "]:
    try:
        eng.search(q)
        ok = True
    except Exception as e:  # noqa
        ok = False
        print(e)
check("search: hostile or empty queries are harmless", ok and eng.status()["chunks"] > 0)

# -----------------------------------------------------------------------------
section("Links, similar notes, reading")
# -----------------------------------------------------------------------------

a = eng.note_links("Netzwerk/VLAN.md")
b = eng.note_links("Netzwerk\\VLAN")
check("links: / and \\ and missing .md give the same answer", a == b, (a, b))
check("links: outgoing and backlinks", a["links"] == ["Netzwerk/PoE.md", "Netzwerk/Router und Layer-3-Switch.md"]
      and {"Willkommen.md", "Journal/2026-10-07.md", "Netzwerk/Router und Layer-3-Switch.md"} <= set(a["backlinks"]), a)
deep = eng.note_links("Journal/2026-10-07.md", hops=2)
check("links: second hop", any(n["hop"] == 2 for n in deep.get("further", [])), deep)
check("links: unknown note", "error" in eng.note_links("Gibt es nicht.md"))
sim = eng.similar("Netzwerk/VLAN.md")
sp = [x["path"] for x in sim["similar"]]
check("similar: excludes itself, one per note", "Netzwerk/VLAN.md" not in sp and len(sp) == len(set(sp)) > 0, sim)
check("similar: unknown note", "error" in eng.similar("Nope.md"))
rd = eng.read_note("Netzwerk\\PoE", with_context=True)
check("read: content and context", "Power over Ethernet" in rd["content"] and "Netzwerk/VLAN.md" in rd["links"]
      and "similar" in rd, rd.keys())
ls = eng.list_notes("Journal")
check("list: folder", ls["count"] == 2 and all(n["path"].startswith("Journal/") for n in ls["notes"]), ls)
check("list: recent first", eng.list_notes(recent=True, limit=1)["notes"][0]["path"] == "Netzwerk/PoE.md")

# -----------------------------------------------------------------------------
section("Vault write tools")
# -----------------------------------------------------------------------------

w = eng.write_note("Inbox/Idee", "# Idee\n\nZitronenfalter-Kalibrierung ist das Prüfwort.\n")
check("write: creates note and folder, indexed at once", w.get("indexed") and (VAULT / "Inbox" / "Idee.md").exists(), w)
check("write: searchable immediately", "Inbox/Idee.md" in paths(eng.search("Zitronenfalter Kalibrierung", mode="keyword")))
for bad in ["../outside.md", "C:\\Windows\\x.md", ".obsidian/x.md", "a/../../x.md", "Netzwerk/.hidden/x.md"]:
    try:
        eng.write_note(bad, "x")
        refused = False
    except rag.VaultError:
        refused = True
    check(f"write: refuses {bad!r}", refused)
check("write: nothing written outside the vault", not (work / "outside.md").exists() and not (work / "x.md").exists())
try:
    eng.write_note("Inbox/Idee.md", "überschrieben")
    refused = False
except rag.VaultError:
    refused = True
check("write: existing note not overwritten without overwrite=true", refused
      and "Zitronenfalter" in (VAULT / "Inbox" / "Idee.md").read_text(encoding="utf-8"))

eng.write_note("Inbox/Liste.md", "# Liste\n\n## Offen\n- eins\n\n## Erledigt\n- null\n")
eng.append_to_note("Inbox/Liste.md", "- zwei", heading="Offen")
t = (VAULT / "Inbox" / "Liste.md").read_text(encoding="utf-8")
check("append: under heading, before the next heading", "- eins\n- zwei\n\n## Erledigt" in t, t)
eng.append_to_note("Inbox/Liste.md", "- eins-b", heading="Erledigt")
t = (VAULT / "Inbox" / "Liste.md").read_text(encoding="utf-8")
check("append: last section", t.rstrip().endswith("- null\n- eins-b"), t)
eng.append_to_note("Inbox/Liste.md", "Schlusssatz.")
check("append: at the end", (VAULT / "Inbox" / "Liste.md").read_text(encoding="utf-8").rstrip().endswith("Schlusssatz."))
try:
    eng.append_to_note("Inbox/Liste.md", "x", heading="Gibt es nicht")
    refused = False
except rag.VaultError:
    refused = True
check("append: unknown heading refused", refused)

crlf = VAULT / "Inbox" / "Windows.md"
crlf.write_bytes(b"# Windows\r\n\r\nZeile eins\r\n")
eng.indexer.sync()
eng.append_to_note("Inbox/Windows.md", "Zeile zwei")
eng.replace_in_note("Inbox/Windows.md", "Zeile eins\n\nZeile zwei", "Zeile 1\n\nZeile 2")
raw_bytes = crlf.read_bytes()
check("crlf: line endings preserved on append and replace",
      b"\r\n" in raw_bytes and b"\n" not in raw_bytes.replace(b"\r\n", b"") and b"Zeile 1\r\n\r\nZeile 2" in raw_bytes, raw_bytes)

r = eng.replace_in_note("Inbox/Idee.md", "Prüfwort", "Testwort")
check("replace: unique text", "Testwort" in (VAULT / "Inbox" / "Idee.md").read_text(encoding="utf-8") and r["indexed"])
for old, why in [("gibt es nicht", "missing"), ("- ", "ambiguous")]:
    try:
        eng.replace_in_note("Inbox/Liste.md", old, "x")
        refused = False
    except rag.VaultError:
        refused = True
    check(f"replace: {why} text refused", refused)

eng.write_note("Inbox/Verweis.md", "Siehe [[Idee]].")
m = eng.move_note("Inbox/Idee.md", "Projekte/Gute Idee.md")
check("move: file moved and re-indexed", (VAULT / "Projekte" / "Gute Idee.md").exists()
      and eng.store.has_file("Projekte/Gute Idee.md") and not eng.store.has_file("Inbox/Idee.md"), m)
check("move: reports notes whose links need updating", m.get("update_links_in") == ["Inbox/Verweis.md"], m)
try:
    eng.move_note("Inbox/Liste.md", "Netzwerk/VLAN.md")
    refused = False
except rag.VaultError:
    refused = True
check("move: never overwrites", refused and "VLAN" in (VAULT / "Netzwerk" / "VLAN.md").read_text(encoding="utf-8"))

# -----------------------------------------------------------------------------
section("Failure handling")
# -----------------------------------------------------------------------------

down = rag.Engine(cfg)
down.embedder.url = "http://127.0.0.1:1"
target = VAULT / "Netzwerk" / "VLAN.md"
orig = target.read_text(encoding="utf-8")
target.write_text(orig + "\n## Neu\nBrandneuer Abschnitt über Spanning Tree.\n", encoding="utf-8")
r = down.indexer.sync()
check("ollama down: error reported, nothing lost", r["errors"] and "not reachable" in r["errors"][0]["error"]
      and down.store.has_file("Netzwerk/VLAN.md")
      and "Netzwerk/VLAN.md" in paths(down.search("802.1Q", mode="keyword")), r)
s = down.search("802.1Q")
check("ollama down: hybrid falls back to keywords with a note", s["results"] and s.get("notes"), s)
r = eng.indexer.sync()
check("ollama back: the change is picked up", r["indexed"] >= 1 and
      "Netzwerk/VLAN.md" in paths(eng.search("Spanning Tree", mode="keyword")), r)
target.write_text(orig, encoding="utf-8")
eng.indexer.sync()

missing = rag.Engine(cfg)
missing.embedder.model = "nicht-da"
info = missing.embedder.check()
try:
    missing.embedder.embed(["x"])
    msg = ""
except rag.EmbeddingError as e:
    msg = str(e)
check("model missing: clear message with pull command", "ollama pull nicht-da" in msg, msg)
check("model mismatch: detected in status", missing.status()["model_mismatch"] is True)
s = missing.search("VLAN")
check("model mismatch: search still answers by keyword and explains", s["results"] and "rebuild" in s["notes"][0], s)

FakeOllama.mode["legacy_only"] = True
old = rag.Engine(cfg)
emb = old.embedder.embed(["eins", "zwei"])
FakeOllama.mode["legacy_only"] = False
check("old Ollama: falls back to /api/embeddings", len(emb) == 2 and old.embedder._legacy)

# -----------------------------------------------------------------------------
section("Watcher and rebuild")
# -----------------------------------------------------------------------------

eng.start_watcher()
time.sleep(1.5)
(VAULT / "Inbox" / "Watcher.md").write_text("# Watcher\n\nAutomatisch gefundenes Prüfwort Schmetterlingsnetz.", encoding="utf-8")
found = False
for _ in range(40):
    time.sleep(0.25)
    if "Inbox/Watcher.md" in paths(eng.search("Schmetterlingsnetz", mode="keyword")):
        found = True
        break
check("watcher: new note indexed automatically", found, eng.status()["sync"])
(VAULT / "Inbox" / "Watcher.md").unlink()
gone = False
for _ in range(40):
    time.sleep(0.25)
    if not eng.store.has_file("Inbox/Watcher.md"):
        gone = True
        break
check("watcher: deleted note removed automatically", gone)

t0 = time.monotonic()
rb = eng.rebuild()
check("rebuild: returns at once", rb["status"] == "started" and time.monotonic() - t0 < 1, rb)
for _ in range(100):
    if not eng.rebuild_state["running"] and eng.rebuild_state["finished"]:
        break
    time.sleep(0.1)
st = eng.status()
check("rebuild: finished, index complete", st["rebuild"]["result"] and not st["rebuild"]["result"].get("errors")
      and st["chunks"] == st["chunks_with_vectors"] > 0, st["rebuild"])
eng.watcher.stop_event.set()

catchup = rag.Engine(cfg)
(VAULT / "Offline.md").write_text("# Offline\n\nGeschrieben, während der Server aus war: Leuchtturmwärter.", encoding="utf-8")
catchup.start_watcher()
ok = False
for _ in range(40):
    time.sleep(0.25)
    if "Offline.md" in paths(catchup.search("Leuchtturmwärter", mode="keyword")):
        ok = True
        break
check("catch-up: changes made while not running are indexed on start", ok)
catchup.watcher.stop_event.set()

# -----------------------------------------------------------------------------
section("Command line")
# -----------------------------------------------------------------------------

env = {**os.environ, "OBSIDIAN_RAG_HOME": str(work / "cli-home"), "PYTHONPATH": os.pathsep.join(sys.path)}
cli = [sys.executable, str(ROOT / "obsidian_rag.py")]
p = subprocess.run(cli + ["init", "--vault", str(VAULT)], env=env, capture_output=True, text=True, encoding="utf-8")
cfg_file = work / "cli-home" / "config.toml"
check("cli init: writes config", p.returncode == 0 and cfg_file.exists(), p.stderr)
cfg_file.write_text(cfg_file.read_text(encoding="utf-8").replace("http://localhost:11434", OLLAMA), encoding="utf-8")
p = subprocess.run(cli + ["init", "--vault", str(VAULT)], env=env, capture_output=True, text=True, encoding="utf-8")
check("cli init: keeps an existing config", p.returncode == 0 and OLLAMA in cfg_file.read_text(encoding="utf-8"))
p = subprocess.run(cli + ["index"], env=env, capture_output=True, text=True, encoding="utf-8")
check("cli index", p.returncode == 0 and json.loads(p.stdout)["indexed"] > 0, p.stdout + p.stderr)
p = subprocess.run(cli + ["check"], env=env, capture_output=True, text=True, encoding="utf-8")
rep = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
check("cli check: ok", p.returncode == 0 and rep.get("ok") is True, p.stdout + p.stderr)
p = subprocess.run(cli + ["search", "Port-Forwarding", "--mode", "keyword"], env=env, capture_output=True,
                   text=True, encoding="utf-8")
check("cli search", p.returncode == 0 and "Journal/2026-10-08.md" in p.stdout, p.stdout + p.stderr)
p = subprocess.run(cli + ["init", "--vault", str(work / "gibt-es-nicht"), "--force"], env=env,
                   capture_output=True, text=True, encoding="utf-8")
check("cli init: refuses a missing vault folder", p.returncode == 2)

claude_cfg = work / "claude" / "claude_desktop_config.json"
claude_cfg.parent.mkdir()
original = {"mcpServers": {"other": {"command": "npx", "args": ["-y", "x"]}},
            "preferences": {"sidebarMode": "chat", "folders": ["C:\\Users\\Ä"], "nested": {"a": [1]}}}
claude_cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps(original, indent=2).encode("utf-8"))  # with BOM
p = subprocess.run(cli + ["register", "--claude-config", str(claude_cfg), "--uv", sys.executable],
                   env=env, capture_output=True, text=True, encoding="utf-8")
data = json.loads(claude_cfg.read_text(encoding="utf-8"))
entry = data["mcpServers"].get("obsidian-rag", {})
check("register: entry added", p.returncode == 0 and entry.get("args", [None])[:2] == ["run", "--script"]
      and entry["args"][2].endswith("obsidian_rag.py") and entry["env"]["OBSIDIAN_RAG_HOME"], p.stdout + p.stderr)
check("register: other servers and preferences untouched", data["mcpServers"]["other"] == original["mcpServers"]["other"]
      and data["preferences"] == original["preferences"], data)
check("register: written without BOM", not claude_cfg.read_bytes().startswith(b"\xef\xbb\xbf"))
check("register: backup kept", any(x.name.startswith("claude_desktop_config.json.bak-") for x in claude_cfg.parent.iterdir()))
p = subprocess.run(cli + ["register", "--claude-config", str(claude_cfg), "--uv", sys.executable],
                   env=env, capture_output=True, text=True, encoding="utf-8")
check("register: running twice updates instead of duplicating", json.loads(p.stdout)[0]["action"] == "updated")
p = subprocess.run(cli + ["register", "--claude-config", str(claude_cfg), "--remove"],
                   env=env, capture_output=True, text=True, encoding="utf-8")
data = json.loads(claude_cfg.read_text(encoding="utf-8"))
check("register --remove: only our entry removed", "obsidian-rag" not in data["mcpServers"]
      and "other" in data["mcpServers"] and data["preferences"] == original["preferences"], data)
new_cfg = work / "claude2" / "claude_desktop_config.json"
p = subprocess.run(cli + ["register", "--claude-config", str(new_cfg), "--uv", sys.executable],
                   env=env, capture_output=True, text=True, encoding="utf-8")
check("register: creates a missing config file", new_cfg.exists() and "obsidian-rag" in
      json.loads(new_cfg.read_text(encoding="utf-8"))["mcpServers"], p.stderr)

# -----------------------------------------------------------------------------
section("MCP protocol (stdio)")
# -----------------------------------------------------------------------------


async def mcp_session(env_extra):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "obsidian_rag.py")],
                                   env={**env, **env_extra})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            init = await session.initialize()
            tools = sorted(t.name for t in (await session.list_tools()).tools)
            out = {"tools": tools, "instructions": getattr(init, "instructions", None)}
            if "write_note" in tools:
                res = await session.call_tool("write_note", {"path": "Inbox/Per MCP.md",
                                                             "content": "# Per MCP\n\nWalnussbaum."})
                out["write"] = json.loads(res.content[0].text)
                res = await session.call_tool("search", {"query": "Walnussbaum"})
                out["search"] = json.loads(res.content[0].text)
                res = await session.call_tool("note_links", {"path": "Netzwerk\\VLAN.md"})
                out["links"] = json.loads(res.content[0].text)
                res = await session.call_tool("read_note", {"path": "../geheim.txt"})
                out["bad_read"] = json.loads(res.content[0].text)
            return out


out = asyncio.run(mcp_session({}))
expected = ["append_to_note", "index_status", "keyword_search", "list_notes", "move_note", "note_links",
            "read_note", "rebuild_index", "replace_in_note", "search", "similar_notes", "write_note"]
check("mcp: all 12 tools", out["tools"] == expected, out["tools"])
check("mcp: server instructions sent", bool(out["instructions"]) and "search" in out["instructions"])
check("mcp: write + search round trip", out["write"].get("indexed") and
      "Inbox/Per MCP.md" in [x["path"] for x in out["search"]["results"]], out)
check("mcp: links over stdio", len(out["links"]["backlinks"]) >= 3, out["links"])
check("mcp: errors come back as messages", "error" in out["bad_read"], out["bad_read"])

ro_home = work / "ro-home"
shutil.copytree(work / "cli-home", ro_home)
(ro_home / "config.toml").write_text((ro_home / "config.toml").read_text(encoding="utf-8")
                                     .replace("allow_write = true", "allow_write = false"), encoding="utf-8")
out = asyncio.run(mcp_session({"OBSIDIAN_RAG_HOME": str(ro_home)}))
check("mcp: read-only mode hides write tools",
      out["tools"] == ["index_status", "keyword_search", "list_notes", "note_links", "read_note",
                       "rebuild_index", "search", "similar_notes"], out["tools"])

# -----------------------------------------------------------------------------
print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
if FAILED:
    print("FAILED:", *FAILED, sep="\n  ")
server.shutdown()
shutil.rmtree(work, ignore_errors=True)
sys.exit(1 if FAILED else 0)
