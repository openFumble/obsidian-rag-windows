# Changelog

## 1.0.0 (2026-10-11)

First release.

- Hybrid search (sqlite-vec + FTS5, Reciprocal Rank Fusion) with type, folder and tag filters
- Heading-aware chunking, frontmatter (tags, aliases, type), inline tags, link graph
- Built-in watcher with catch-up on start; only changed chunks are re-embedded, moves are free
- Safe vault tools: read, write (no silent overwrite), append, replace, move; no delete
- Windows: forward-slash paths everywhere, CRLF preserved, MSIX and classic Claude Desktop
- One-click installer (uv, Ollama, model, config, first index, Claude Desktop registration, self-test)
