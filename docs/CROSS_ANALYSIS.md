# Cross-analysis: `gamma/prod` vs `alpha/prod`

Both implement the same PLANv3 backend (local-only, LM Studio, typed tools,
plain-text vault memory, full autonomy). They diverge in shape and completeness.
This compares them and lists the improvements worth porting **into** `alpha/prod`.

## Verdict in one line

`alpha/prod` is the more complete and correct **runtime** (async live streaming,
real cancellation, tool subsetting, reversible deletes, tripwires, profile
auto-load, governors, schema-validated tool args, a fake model that exercises the
full tool loop). `gamma/prod` has a few genuinely better **memory-persistence**
ideas (a SQLite-indexed note catalog with content hashes, a persisted link table,
a richer vault taxonomy) plus one small loop governor. Port those; keep the rest.

## Shape

| | alpha/prod | gamma/prod |
|---|---|---|
| Layout | flat `jarvis/` package | `src/jarvis/` |
| Internal types | Pydantic models | dataclasses (`slots=True`) |
| Tool args | Pydantic model per tool → **auto JSON schema + validation** | plain `dict` + hand-written JSON schema; **no validation** |
| Server / loop | **async**; run is a background task | **synchronous**; run executes *inside* the POST request (blocks) |
| Event streaming | live pub/sub over SSE as the run executes | SSE endpoint replays already-stored events then closes (no live push) |
| Cancellation / kill switch | works concurrently (async task) | registry exists but the run blocks the request, so cancel can't land mid-run |
| Tool subsetting | yes (relevance-ranked subset) | no — sends **all** tool schemas every call |
| Retrieval | keyword + **wikilink-graph traversal** + optional vector | SQL `LIKE` over an indexed note table (no graph traversal, no vector) |
| Memory index | rescans Markdown per search | **SQLite `memory_notes` index** (hash, summary, frontmatter) ← *better* |
| Profile auto-load | always loads `JARVIS.md` (+active project) | not loaded into context (only search hits) |
| fs tools | list/read/write/append/mkdir/search/**move/delete** | list/read/write/append/mkdir/search (**no move/delete**) |
| Delete safety | recycle-bin + snapshot → **reversible**, with restore | n/a (no delete tool); rollback has snapshot but **no restore method** |
| Tripwires / path scope | yes | none |
| Tool timeout | enforced (`wait_for`) | declared but not enforced |
| Governors | step limit, wall-clock, no-progress detector | step limit, **max_tool_failures** ← *we lack this* |
| Fake model | scripts multi-step tool calls → tests the real loop | returns one final string → only tests a no-tool run |
| Tests | 20 (incl. end-to-end tool loop, governors, rollback, API) | 2 (trivial run + vault basics) |
| LOC | ~3,000 | ~1,750 |

## Where gamma is genuinely better — port these into alpha

1. **SQLite-indexed note catalog (highest value).** gamma writes every note into a
   `memory_notes` table (id, vault_path, type, title, project_id, source,
   `content_hash`, `summary`, `frontmatter_json`, `updated_at`). Search is a fast
   indexed query with a file-scan fallback, and `content_hash` gives cheap change
   detection for incremental reindex. alpha currently rescans the Markdown every
   search — fine at small scale, slow as the vault grows. **Adopt:** persist a note
   index in SQLite (extend alpha's `vault_files` into a `memory_notes` catalog);
   keep Markdown canonical; use the index for the keyword pass and for the
   gardener's change detection. `vault.reindex` repopulates it.

2. **Persisted wikilink edges.** gamma defines a `memory_links` table (from/to,
   link_text, link_type). alpha builds the graph in memory on every retrieval.
   **Adopt (medium):** persist edges during indexing so graph traversal and orphan
   detection scale without a full rescan.

3. **`max_tool_failures` governor.** A distinct cap on cumulative failed tool calls
   per run — cheap insurance beyond alpha's step-limit + no-progress detector.
   **Adopt (easy):** add to the loop and `limits` config.

4. **Richer vault taxonomy (optional).** gamma uses a PARA-style tree
   (Inbox/Daily/Projects/Areas/Resources/People/Procedures/Skills/Runs/Archive/
   System/Templates) and per-project `Decisions` + `Skills` folders. alpha's tree is
   leaner. **Adopt selectively:** add `Areas/`, `Resources/`, `Archive/`,
   `System/Templates/`, and a per-project `Decisions/` folder; skip the rest to
   avoid over-structuring. Make it config-driven (alpha already supports this).

5. **Minor robustness:** `ensure_ascii=True` on JSON writes (matches the plan's
   ASCII-safe note), PowerShell `-ExecutionPolicy Bypass` for script execution, and
   a `source_uri` field on notes distinct from `source`.

## Where alpha is stronger — keep as-is (do NOT adopt gamma's choices)

- **Async runtime + background run task.** Required for live SSE streaming and for
  cancellation/kill switch to actually work. gamma's synchronous run blocks the
  request, making its cancel/kill endpoints effectively inert.
- **Tool subsetting.** Sending all schemas to a 9–14B local model hurts tool-call
  accuracy and burns context; alpha subsets per task.
- **Pydantic-validated tool args.** Bad arguments are rejected with a structured
  error before the handler runs; gamma passes raw dicts unchecked.
- **Reversible delete + move + rollback restore.** alpha trashes via Recycle Bin
  with a pre-snapshot and can restore files/dirs/overwrites/bulk-moves; gamma has
  no delete/move tools and no restore path.
- **Tripwires + path scope.** alpha refuses catastrophic commands and protects
  system paths; gamma has neither.
- **Profile auto-load.** alpha always injects `JARVIS.md` (+ active `PROJECT.md`) —
  the "never re-explain yourself" guarantee; gamma only injects search hits.
- **Enforced tool timeouts** and a **fake model that drives the real tool loop**
  (gamma's fake can't, so its loop is largely untested).

## Recommended change set for alpha/prod (prioritized)

1. SQLite `memory_notes` catalog + `content_hash` change detection; wire the
   keyword pass and gardener to it. *(high value, moderate effort)*
2. `max_tool_failures` governor. *(easy)*
3. Persisted `memory_links` edges for scalable graph/orphan queries. *(medium)*
4. Optional vault taxonomy additions + per-project `Decisions/`. *(easy, config)*
5. Minor robustness (ensure_ascii, ExecutionPolicy Bypass, source_uri). *(trivial)*

None of these change alpha's architecture; they harden the memory layer and add
one governor. Estimated impact: faster/scalable memory, better change detection,
and one more safety rail — while keeping all 20 tests green.
