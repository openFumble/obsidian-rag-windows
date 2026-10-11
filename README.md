# obsidian-rag-windows

**Local AI search for your Obsidian vault in Claude Desktop, made for Windows.**
One installer, no programming, and your notes never leave your PC.

[![CI](https://github.com/openFumble/obsidian-rag-windows/actions/workflows/ci.yml/badge.svg)](https://github.com/openFumble/obsidian-rag-windows/actions/workflows/ci.yml)
[Deutsche Anleitung](README.de.md)

Claude normally has to read whole notes or folders to answer questions about your vault, which is slow, expensive and runs into limits.
This project gives Claude a **search index** (RAG = retrieval-augmented generation).
Claude asks the index first and reads only the few passages that matter.

| You ask Claude | What happens |
|---|---|
| "What did I write about VLANs?" | Hybrid search finds the right notes, even without the exact word |
| "Which note mentions `bge-m3`?" | Keyword search finds exact names, IDs and model names |
| "What is linked to my project note?" | Links and backlinks from your `[[wikilinks]]` |
| "Add today's result to my project note" | Claude edits the note safely, and the index updates at once |

## Features

- **Hybrid search**: meaning *and* exact words, fused with Reciprocal Rank Fusion. Filters by note type, folder and tag. Off-topic questions return nothing instead of random notes.
- **Multilingual**: the default model `bge-m3` understands German, English and 100+ other languages, and you can ask in English about German notes.
- **Link graph**: links, backlinks and linked notes from wikilinks (`[[Note]]`, `[[Note|Alias]]`, `[[Note#Heading]]`) and Markdown links.
- **Similar notes**: finds related notes by meaning.
- **Always up to date**: while Claude Desktop runs, changed notes are re-indexed within seconds. On start it catches up on everything you changed in Obsidian in the meantime. Only changed passages are re-computed.
- **Safe editing tools**: read, create, append, replace text, move notes. Nothing is ever deleted, existing notes are never overwritten by accident, and the tools only work inside the vault and only on Markdown files. These tools replace a separate file-system MCP server.
- **Built for Windows**: correct `\` / `/` paths, Windows line endings preserved, Microsoft Store and classic Claude Desktop supported.
- **Private**: embeddings are computed locally by [Ollama](https://ollama.com). No cloud service, no API key.

## Requirements

- Windows 11 (Windows 10 also works)
- [Claude Desktop](https://claude.ai/download)
- An [Obsidian](https://obsidian.md) vault
- About 2 GB free disk space (Ollama + model) and internet during installation

Everything else (uv, Python libraries, Ollama, the embedding model) is installed automatically.

## Installation

1. **Download**: on this page click **Code → Download ZIP** and extract the ZIP, for example to your Downloads folder.
2. **Run** `install.cmd` (double-click).
   - Windows may warn "Windows protected your PC" or ask whether to run the file, because it was downloaded from the internet. Choose **More info → Run anyway** or **Run**.
3. **Answer the questions**: pick your vault (the installer lists the vaults Obsidian knows) and confirm closing Claude Desktop if it is open.
4. **Start Claude Desktop** and ask: *"Search my vault for …"*

The installer:

1. installs **uv**, which runs the Python program and fetches its two libraries
2. installs **Ollama** via winget and downloads the model **bge-m3** (about 1.2 GB, one time)
3. copies the program to `%LOCALAPPDATA%\obsidian-rag-windows` and writes `config.toml`
4. builds the first index (a few minutes for large vaults)
5. adds the server to Claude Desktop's `claude_desktop_config.json`, keeping a backup next to it
6. runs a self-test

Your vault is not changed by the installation.

<details>
<summary>Alternative: install with one PowerShell command</summary>

```powershell
irm https://raw.githubusercontent.com/openFumble/obsidian-rag-windows/main/install.ps1 | iex
```

Options when running `install.ps1` directly:

| Option | Meaning |
|---|---|
| `-VaultPath "D:\Notes"` | Vault folder (otherwise asked) |
| `-Model bge-m3` | Another Ollama embedding model |
| `-Reconfigure` | Replace an existing config (e.g. another vault) |
| `-ServerName obsidian-rag` | Name shown in Claude Desktop |
| `-SkipOllama` / `-SkipIndex` / `-SkipClaude` | Skip a step |
| `-Yes` | No questions (closes Claude Desktop if needed) |
</details>

## Tools Claude gets

| Tool | Purpose |
|---|---|
| `search` | Hybrid search, the starting point. Options: `note_type`, `folder`, `tag`, `expand` (add linked notes) |
| `keyword_search` | Exact words only (names, IDs, model names) |
| `read_note` | Full text of a note, optionally with links, backlinks and similar notes |
| `note_links` | Links and backlinks, also two or more steps away |
| `similar_notes` | Notes closest in meaning |
| `list_notes` | Notes of a folder, or the most recently changed |
| `write_note` | Create a note (refuses to overwrite unless asked to) |
| `append_to_note` | Add text at the end or under a heading |
| `replace_in_note` | Replace one exact, unique text passage |
| `move_note` | Move or rename, and report notes whose links need updating |
| `index_status` | Statistics, watcher state, errors |
| `rebuild_index` | Rebuild everything in the background (after changing the model) |

Tip: copy the short text from [docs/CLAUDE_INSTRUCTIONS.md](docs/CLAUDE_INSTRUCTIONS.md) into the instructions of a Claude project. Claude then always searches before reading.

## How it works

```
Obsidian vault (your .md files)
      │  every 5 s: which files changed? (only timestamps, very cheap)
      ▼
obsidian_rag.py ── splits notes at headings ──► Ollama (bge-m3, local) ──► vectors
      │
      ▼
index.db (SQLite) ── vectors (sqlite-vec) + full-text index (FTS5) + link graph
      ▲
      │  MCP (stdio)
Claude Desktop ── search / read / write tools
```

- Notes are split at headings (H1 to H4) into passages of at most 2000 characters. Code blocks stay intact, and each passage is embedded together with its note title and heading path.
- Only passages whose text changed are sent to Ollama again. Moving a note costs nothing.
- If Ollama is not running, nothing is lost: search falls back to keywords, and the watcher retries later.
- Notes in folders starting with `.` (such as `.obsidian` and `.trash`) are ignored.

## Configuration

`%LOCALAPPDATA%\obsidian-rag-windows\config.toml` (restart Claude Desktop after changes):

```toml
vault_path = 'D:\Notes'

[embedding]
url = "http://localhost:11434"
model = "bge-m3"            # changing it requires rebuild_index

[index]
max_chunk_chars = 2000
exclude_folders = ["node_modules", "_resources"]
watch = true
watch_interval = 5

[index.type_rules]          # optional: note type by folder, for the note_type filter
"Journal/" = "daily"

[search]
similarity_threshold = 0.40 # semantic hits below this are dropped
limit = 5
max_per_note = 2            # breadth before depth: at most 2 passages per note
max_chars = 600

[vault]
allow_write = true          # false = read-only tools only
```

The note type comes from the frontmatter (`type:`), then from `type_rules`, and is `daily` for notes named like `2026-10-11.md`.

## Updating

Download the new ZIP and run `install.cmd` again, or use the one-line command. Your config and index are kept, and only changed notes are processed.

## Uninstalling

Run `%LOCALAPPDATA%\obsidian-rag-windows\uninstall.cmd`. It removes the entry from Claude Desktop (with a backup) and deletes the index. Your vault is not touched. Ollama, the model and uv stay installed, and the commands to remove them are printed.

## Troubleshooting

| Problem | Solution |
|---|---|
| The tools do not show up in Claude | Quit Claude Desktop completely (tray icon → Quit) and start it again. Check that `obsidian-rag` is listed under Settings → Developer |
| "Ollama not reachable" | Start Ollama from the Start menu. It normally starts with Windows |
| "Ollama has no model …" | `ollama pull bge-m3` in a terminal |
| First index is slow | Normal for big vaults without a GPU: it is done once, later only changes are processed |
| Vault on OneDrive | Works. Notes that exist only in the cloud are downloaded when they are read, so "Always keep on this device" makes it faster |
| Something else | Run `uv run --script "$env:LOCALAPPDATA\obsidian-rag-windows\obsidian_rag.py" check` and look at `obsidian-rag.log` in the same folder |

## Privacy

Everything runs on your computer. Internet is only used during installation, to download uv, the two Python libraries ([mcp](https://pypi.org/project/mcp/) and [sqlite-vec](https://pypi.org/project/sqlite-vec/)), Ollama and the model. Claude sees only the passages the tools return to it, as part of your normal Claude conversation.

## Development

```powershell
# tests (no Ollama needed, a fake embedding server is included)
uv run --with "mcp>=1.10,<3" --with "sqlite-vec>=0.1.6,<0.2" python tests/test_obsidian_rag.py

# try it on the example vault
$env:OBSIDIAN_RAG_HOME = "$env:TEMP\rag-demo"
uv run --script obsidian_rag.py init --vault tests\vault
uv run --script obsidian_rag.py index
uv run --script obsidian_rag.py search "Strom über das Netzwerkkabel"
```

CI runs the tests on Windows and Linux, and runs the full installer, a Claude-Desktop-style start and the uninstaller on Windows PowerShell 5.1.

## Acknowledgements

Inspired by [obsidian-notes-rag](https://github.com/proofsh/obsidian-notes-rag) (MIT). This project is a separate implementation with Windows-safe paths, hybrid search, a built-in watcher and vault editing tools.

## License

[MIT](LICENSE)
