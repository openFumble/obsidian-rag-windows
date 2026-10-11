# Instructions for Claude

Copy one of these blocks into the instructions of a Claude project (or into your
personal preferences). Claude then works token-efficiently with your vault.

## English

```
My Obsidian vault is available through the obsidian-rag tools.
- Always call `search` first; read only the notes you actually need with `read_note`. Never read whole folders.
- Use `keyword_search` for exact names, IDs or model names.
- Before creating a note, search whether the topic exists; extend existing notes instead of duplicating them.
- Use `append_to_note` or `replace_in_note` for changes; `write_note` only for new notes.
- After `move_note`, update the links in the notes it reports.
- Never delete anything.
```

## Deutsch

```
Mein Obsidian-Vault ist über die Werkzeuge von obsidian-rag erreichbar.
- Rufe immer zuerst `search` auf und lies mit `read_note` nur die Notizen, die du wirklich brauchst. Lies nie ganze Ordner.
- Für genaue Namen, Kennungen oder Modellnamen nutze `keyword_search`.
- Prüfe vor dem Anlegen einer Notiz per Suche, ob es das Thema schon gibt, und ergänze lieber, statt zu duplizieren.
- Änderungen mit `append_to_note` oder `replace_in_note`, `write_note` nur für neue Notizen.
- Nach `move_note` die Links in den gemeldeten Notizen anpassen.
- Lösche nie etwas.
```
