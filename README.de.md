# obsidian-rag-windows

**Lokale KI-Suche für deinen Obsidian-Vault in Claude Desktop, gemacht für Windows.**
Ein Installer, keine Programmierkenntnisse nötig, und deine Notizen verlassen den PC nicht.

[English README](README.md)

Ohne Hilfe muss Claude ganze Notizen oder Ordner lesen, um Fragen zu deinem Vault zu beantworten. Das ist langsam, verbraucht viele Tokens und stößt an Grenzen.
Dieses Projekt gibt Claude einen **Suchindex** (RAG = Retrieval-Augmented Generation).
Claude fragt zuerst den Index und liest nur die wenigen Stellen, die wirklich passen.

| Du fragst Claude | Was passiert |
|---|---|
| „Was habe ich über VLANs geschrieben?“ | Die hybride Suche findet die passenden Notizen, auch ohne das genaue Wort |
| „Welche Notiz erwähnt `bge-m3`?“ | Die Stichwortsuche findet genaue Namen, Kennungen und Modellnamen |
| „Was ist mit meiner Projektnotiz verlinkt?“ | Links und Backlinks aus deinen `[[Wikilinks]]` |
| „Trag das heutige Ergebnis in meine Projektnotiz ein“ | Claude bearbeitet die Notiz sicher, der Index ist sofort aktuell |

## Funktionen

- **Hybride Suche**: Bedeutung *und* genaue Wörter, zusammengeführt mit Reciprocal Rank Fusion. Filter nach Notiztyp, Ordner und Tag. Themenfremde Fragen liefern nichts statt zufälliger Notizen.
- **Mehrsprachig**: Das Standardmodell `bge-m3` versteht Deutsch, Englisch und über 100 weitere Sprachen. Du kannst auf Englisch nach deutschen Notizen fragen.
- **Link-Graph**: Links, Backlinks und verlinkte Notizen aus Wikilinks (`[[Notiz]]`, `[[Notiz|Alias]]`, `[[Notiz#Überschrift]]`) und Markdown-Links.
- **Ähnliche Notizen**: findet inhaltlich verwandte Notizen.
- **Immer aktuell**: Solange Claude Desktop läuft, werden geänderte Notizen nach wenigen Sekunden neu eingelesen. Beim Start wird alles nachgeholt, was du in der Zwischenzeit in Obsidian geändert hast. Neu berechnet werden nur geänderte Abschnitte.
- **Sichere Bearbeitungswerkzeuge**: lesen, anlegen, ergänzen, Text ersetzen, verschieben. Es wird nie etwas gelöscht, und bestehende Notizen werden nie versehentlich überschrieben. Die Werkzeuge arbeiten nur innerhalb des Vaults und nur mit Markdown-Dateien. Ein zusätzlicher Dateisystem-Server ist damit unnötig.
- **Für Windows gebaut**: korrekte Pfade mit `\` und `/`, Windows-Zeilenumbrüche bleiben erhalten, Claude Desktop aus dem Microsoft Store und als klassische Installation wird unterstützt.
- **Privat**: Die Embeddings berechnet [Ollama](https://ollama.com) lokal. Kein Cloud-Dienst, kein API-Schlüssel.

## Voraussetzungen

- Windows 11 (Windows 10 geht auch)
- [Claude Desktop](https://claude.ai/download)
- Ein [Obsidian](https://obsidian.md)-Vault
- Etwa 2 GB freier Speicher (Ollama und Modell), Internet während der Installation

Alles andere (uv, Python-Bibliotheken, Ollama, das Embedding-Modell) installiert der Installer selbst.

## Installation

1. **Herunterladen**: Oben auf dieser Seite **Code → Download ZIP** klicken und die ZIP-Datei entpacken, z. B. in den Download-Ordner.
2. **`install.cmd` doppelklicken.**
   - Windows warnt eventuell mit „Der Computer wurde durch Windows geschützt“ oder fragt, ob die Datei ausgeführt werden soll, weil sie aus dem Internet stammt. Dann **Weitere Informationen → Trotzdem ausführen** bzw. **Ausführen** wählen.
3. **Fragen beantworten**: Vault auswählen (der Installer zeigt die Vaults, die Obsidian kennt). Ist Claude Desktop offen, das Schließen bestätigen.
4. **Claude Desktop starten** und zum Beispiel fragen: *„Durchsuche meinen Vault nach …“*

Was der Installer macht:

1. installiert **uv**, das das Python-Programm startet und seine zwei Bibliotheken lädt
2. installiert **Ollama** über winget und lädt das Modell **bge-m3** (etwa 1,2 GB, einmalig)
3. kopiert das Programm nach `%LOCALAPPDATA%\obsidian-rag-windows` und legt die `config.toml` an
4. baut den ersten Index auf (bei großen Vaults ein paar Minuten)
5. trägt den Server in die `claude_desktop_config.json` von Claude Desktop ein und legt daneben eine Sicherung ab
6. macht einen Selbsttest

An deinem Vault ändert die Installation nichts.

<details>
<summary>Alternative: Installation mit einem PowerShell-Befehl</summary>

```powershell
irm https://raw.githubusercontent.com/openFumble/obsidian-rag-windows/main/install.ps1 | iex
```

Optionen beim direkten Aufruf von `install.ps1`:

| Option | Bedeutung |
|---|---|
| `-VaultPath "D:\Notizen"` | Vault-Ordner (sonst wird gefragt) |
| `-Model bge-m3` | Anderes Ollama-Embedding-Modell |
| `-Reconfigure` | Bestehende Config ersetzen (z. B. anderer Vault) |
| `-ServerName obsidian-rag` | Name in Claude Desktop |
| `-SkipOllama` / `-SkipIndex` / `-SkipClaude` | Einen Schritt überspringen |
| `-Yes` | Keine Rückfragen (schließt Claude Desktop bei Bedarf) |
</details>

## Werkzeuge für Claude

| Werkzeug | Zweck |
|---|---|
| `search` | Hybride Suche, der Einstieg. Optionen: `note_type`, `folder`, `tag`, `expand` (verlinkte Notizen dazu) |
| `keyword_search` | Nur genaue Wörter (Namen, Kennungen, Modellnamen) |
| `read_note` | Ganze Notiz, optional mit Links, Backlinks und ähnlichen Notizen |
| `note_links` | Links und Backlinks, auch über mehrere Schritte |
| `similar_notes` | Inhaltlich ähnlichste Notizen |
| `list_notes` | Notizen eines Ordners oder die zuletzt geänderten |
| `write_note` | Notiz anlegen (überschreibt nur, wenn ausdrücklich gewünscht) |
| `append_to_note` | Text am Ende oder unter einer Überschrift ergänzen |
| `replace_in_note` | Eine genaue, eindeutige Textstelle ersetzen |
| `move_note` | Verschieben oder umbenennen, meldet Notizen, deren Links angepasst werden müssen |
| `index_status` | Zahlen, Watcher-Zustand, Fehler |
| `rebuild_index` | Alles im Hintergrund neu aufbauen (nach einem Modellwechsel) |

Tipp: Den kurzen Text aus [docs/CLAUDE_INSTRUCTIONS.md](docs/CLAUDE_INSTRUCTIONS.md) in die Anweisungen eines Claude-Projekts kopieren. Dann sucht Claude immer zuerst, bevor es liest.

## So funktioniert es

```
Obsidian-Vault (deine .md-Dateien)
      │  alle 5 s: Welche Dateien haben sich geändert? (nur Zeitstempel, sehr günstig)
      ▼
obsidian_rag.py ── teilt Notizen an Überschriften ──► Ollama (bge-m3, lokal) ──► Vektoren
      │
      ▼
index.db (SQLite) ── Vektoren (sqlite-vec) + Volltextindex (FTS5) + Link-Graph
      ▲
      │  MCP (stdio)
Claude Desktop ── Such-, Lese- und Schreibwerkzeuge
```

- Notizen werden an Überschriften (H1 bis H4) in Abschnitte von höchstens 2000 Zeichen geteilt. Codeblöcke bleiben ganz, und jeder Abschnitt wird zusammen mit Notiztitel und Überschriftenpfad berechnet.
- Nur Abschnitte, deren Text sich geändert hat, gehen erneut an Ollama. Verschieben kostet nichts.
- Läuft Ollama nicht, geht nichts verloren: Die Suche weicht auf Stichworte aus, und der Watcher versucht es später erneut.
- Ordner, die mit `.` beginnen (z. B. `.obsidian`, `.trash`), werden ignoriert.

## Einstellungen

`%LOCALAPPDATA%\obsidian-rag-windows\config.toml` (nach Änderungen Claude Desktop neu starten). Die Optionen sind in der [englischen README](README.md#configuration) erklärt. Häufig gebraucht:

- `similarity_threshold`: ab welcher Ähnlichkeit ein Treffer zählt (Standard 0,40)
- `[index.type_rules]`: Notiztyp nach Ordner, z. B. `"Journal/" = "daily"`, für den Filter `note_type`
- `allow_write = false`: nur Lesewerkzeuge

## Aktualisieren

Neue ZIP herunterladen und `install.cmd` erneut ausführen, oder den Einzeiler nutzen. Config und Index bleiben erhalten, verarbeitet werden nur geänderte Notizen.

## Deinstallieren

`%LOCALAPPDATA%\obsidian-rag-windows\uninstall.cmd` ausführen. Das entfernt den Eintrag aus Claude Desktop (mit Sicherung) und löscht den Index. Dein Vault bleibt unberührt. Ollama, das Modell und uv bleiben installiert, die Befehle zum Entfernen werden angezeigt.

## Wenn etwas nicht klappt

| Problem | Lösung |
|---|---|
| Claude zeigt die Werkzeuge nicht | Claude Desktop ganz beenden (Symbol in der Taskleiste → Beenden) und neu starten. Unter Einstellungen → Entwickler muss `obsidian-rag` stehen |
| „Ollama not reachable“ | Ollama über das Startmenü starten. Normalerweise startet es mit Windows |
| „Ollama has no model …“ | Im Terminal: `ollama pull bge-m3` |
| Der erste Index dauert lange | Bei großen Vaults ohne Grafikkarte normal. Das passiert einmal, danach werden nur Änderungen verarbeitet |
| Vault in OneDrive | Funktioniert. Nur online gespeicherte Notizen werden beim Lesen heruntergeladen, „Immer auf diesem Gerät behalten“ macht es schneller |
| Etwas anderes | `uv run --script "$env:LOCALAPPDATA\obsidian-rag-windows\obsidian_rag.py" check` ausführen und `obsidian-rag.log` im selben Ordner ansehen |

## Datenschutz

Alles läuft auf deinem Rechner. Internet wird nur bei der Installation gebraucht, für uv, die zwei Python-Bibliotheken ([mcp](https://pypi.org/project/mcp/) und [sqlite-vec](https://pypi.org/project/sqlite-vec/)), Ollama und das Modell. Claude sieht nur die Abschnitte, die die Werkzeuge im normalen Gespräch zurückgeben.

## Lizenz

[MIT](LICENSE)
