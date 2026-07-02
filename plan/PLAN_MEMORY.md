# JARVIS Memory v2 — Tiered Cognitive Memory

A redesign of JARVIS's "second brain" from a flat keyword vault into a tiered, self-organizing
memory: short / medium / long term, with semantic retrieval, an LLM **librarian** that actively
places, groups, describes and promotes memories, and a never-delete (archive-only) lifecycle.

Supersedes the current keyword+graph retriever and extends the RAG injection and `daily/`
exclusion added while hardening v1–v4.

## Locked decisions

1. **Retrieval:** add local **embeddings** (semantic) — run off-GPU so it never fights the brain.
2. **Curation:** an **LLM librarian** does the thinking — extraction, tiering, dedupe, grouping,
   descriptions. Runs on a schedule / idle, never in the hot path of a user request.
3. **Capture:** **auto-extract and auto-promote** — salient facts are pulled from every
   conversation and promoted to long-term without a manual gate.
4. **Forgetting:** **never delete** — memories decay in rank and are **archived**, never removed;
   anything can be revived.

## 1. The tiers

Memory is one store with a `tier` field, mapped onto the Obsidian-compatible vault folders so it
stays human-browsable.

- **Short-term (working).** An **enriched working set** for the current session, built
  **passively** (no extra model calls): the live conversation **+** every memory note touched this
  session **+** the atoms already extracted this session. Held on the `Pipe`/thread and always
  injected in full. Assembled by unioning conversation history, the hits of any recall done this
  session, and prior extractions — so "what we've been talking about" stays present without a live
  extractor. Not persisted as durable notes; scope = the session/thread (a new tab = a fresh set).
- **Medium-term (episodic).** Recent, time-stamped events and freshly-extracted facts — a rolling
  record of "what happened lately." Lives in `inbox/` and a new `episodic/` folder, each note
  carrying a decay weight. Retrieved by relevance **and** recency. This is where auto-extraction
  lands first.
- **Long-term (semantic).** Durable, deduped, generalized knowledge: the user profile,
  preferences, stable facts, and entities (people, projects, tools). Lives in `notes/`, `people/`,
  `projects/`, and a `profile` note. Reached by semantic similarity + graph links, with no recency
  penalty. Populated by the librarian promoting recurring/confirmed episodic memories.
- **Archive (tombstone tier).** Demoted or superseded memories. Lives in `archive/`. Excluded from
  normal recall, included only on an explicit deep search, and always revivable. Nothing is ever
  hard-deleted (decision #4).

Operational machine output (the `daily/` run-summaries / garden reports) is **not** memory and is
permanently excluded from recall — already enforced in `index.search` / `_scoped_notes`.

## 2. Data model

Extend `memory_notes` (in `data/store.py`) and add two tables. Frontmatter in each `.md` mirrors
the columns so the vault stays the source of truth (Obsidian-editable) and SQLite is the index.

- `memory_notes` add: `tier` (`short|medium|long|archive`), `salience` (float weight),
  `last_accessed`, `access_count`, `confidence` (0–1), `group_id`, `source_run_id`,
  `superseded_by` (path, for archive lineage).
- `memory_vectors` (new): `vault_path`, `embedding` (BLOB float32), `model`, `dim`. One row per
  note; brute-force cosine is fine at personal-vault scale (add ANN only if it ever gets slow).
- `memory_groups` (new): `id`, `kind` (`entity|topic`), `title`, `description`, `tier`,
  `centroid` (embedding), membership via `memory_notes.group_id`. A group is either an **entity**
  (a specific person/project/tool — lives in `people/`|`projects/`) or a **topic** cluster, and
  each materializes as a **concept note (map-of-content)** in the vault: an LLM-written description
  plus wikilinks to its members, browsable directly in Obsidian. This is the "grouping / separating
  / description" surface.

## 3. Embeddings (semantic backbone)

- **Engine:** in-process CPU embedder (sentence-transformers, e.g. `bge-small-en` or
  `nomic-embed-text`) via `embed.mode: in_process`. Chosen over `embed.mode: lmstudio` on purpose —
  the config already warns that LM Studio embedding swaps the chat model in/out and thrashes VRAM.
  CPU keeps the GPU free for brain + vision.
- **Write path:** every note write/update computes and stores its vector (`memory_vectors`).
- **Backfill:** one-time pass to embed all existing notes; also archive the 26 stale
  `daily/run-run-*.md` logs during this migration.
- **Cosine search:** `Retriever` gains a real vector path (the current `embed_fn` rerank becomes
  the primary signal, not an optional last step).

## 4. Retrieval (hybrid, tier-aware)

Replace the keyword-first ranker with a blended score, computed per candidate:

`score = w_sem·cosine(query, note) + w_kw·term_overlap + w_graph·link_proximity + w_tier·tier_bias − decay(age, tier)`

- **Working** memory is always in context (not retrieved — it's just there).
- **Long-term** gets a tier bias and **no** decay penalty; **episodic** is decayed by age so old
  events fade in rank (but never disappear).
- Keyword overlap keeps exact-term/name matches strong (the fix from v4 stays as one signal).
- Graph expansion (existing `neighbors`) pulls in linked entities.
- **Excluded** from default recall: `archive/` and `daily/`. Deep search can opt into `archive/`.

This upgrades the pipeline's current `_normalize` RAG injection: inject **working memory in full +
top-k semantic/episodic hits (grouped, with their descriptions)**, clearly labelled, so classify
and the direct answer can use them — the mechanism that made recall work, now tier-aware.

## 5. Ingestion & extraction (auto)

After each completed run (async, off the response path):

1. The **extractor** (LLM) reads the conversation and emits atomic memories:
   `{text, type: identity|preference|entity|goal|fact|event|task, subject, confidence}`. Types
   drive the promotion fast-path in §6 (identity/preference/entity/goal are durable; fact needs
   recurrence; event/task stay episodic).
2. Each atom is redacted (existing `redact`), embedded, and written to **episodic** (`inbox/` →
   `episodic/`) with `source_run_id` and provisional tier.
3. Cheap dedupe on write: if cosine to an existing note exceeds a threshold, **merge/update**
   (bump `confidence`, refresh `last_accessed`) instead of creating a duplicate — prevents the
   "3× BlueFalcon" clutter seen in the probe.

Explicit "remember X" still works and simply enters the same pipeline with `confidence = 1`.

## 6. Active placement — the librarian

A scheduled/idle **librarian** pass (LLM, batched) is where the real thinking happens. For a batch
of new/changed episodic memories it:

- **Classifies & promotes (3-layer policy):**
  1. **Type fast-path** — `identity`, `preference`, `entity`, and `goal` atoms promote to
     long-term on first sight (e.g. "my codename is X", "I prefer concise answers").
  2. **Recurrence/usage** — a plain `fact` promotes once it recurs or is reused ≥ N times
     (default 2) across ≥ 2 sessions; `event`/`task` atoms otherwise stay episodic and decay.
  3. **Librarian override** — the LLM may promote or hold against these defaults when the content
     clearly warrants it (a stated long-term goal promotes now; idle chit-chat never does).
  Layers 1–2 are deterministic and unit-testable; layer 3 is judgment layered on top.
- **Dedupes & merges:** collapses near-duplicates (semantic), keeping the richest phrasing and the
  union of links; the loser is archived with `superseded_by` set (lineage, never lost).
- **Groups:** clusters related memories (embedding clustering) and assigns `group_id`; creates or
  updates the group's **concept note** (map-of-content) with an LLM **description** and wikilinks
  to members. Splits a note that has drifted across multiple topics.
- **Describes:** writes/refreshes each note's `summary` and each group's `description` (used both
  for browsing and as the retrieval snippet).
- **Links:** adds graph edges to related entities (people/projects/tools).

Runs via the existing `gardener` hook (currently disabled) on a schedule + on idle; batches to keep
model cost bounded.

## 7. Lifecycle: promote, decay, archive (never delete)

- **Promote:** episodic → semantic when a fact recurs, is confirmed, or is referenced repeatedly
  (`access_count`).
- **Decay:** `salience` falls with age and disuse (episodic faster than semantic); it only affects
  **rank**, never existence.
- **Archive:** when salience drops below a floor, or a memory is superseded/merged, move it to
  `archive/` (tier=`archive`), excluded from default recall, revivable on deep search or if a new
  query strongly matches it.
- **Compaction, not deletion:** merges reduce note count over time so "never delete" doesn't mean
  unbounded recall cost — archive + dedupe keep the active set lean while retaining everything.
- **Defaults (tunable, settled in Phase 5 evals):** episodic half-life ≈ 14 days; semantic
  effectively non-decaying; archive floor at `salience < 0.1`; **dedupe-merge** at cosine > 0.92;
  **same-group/related** at cosine > 0.75; promotion recurrence **N = 2 across ≥ 2 sessions**.

## 8. Pipeline & tooling integration

- **Context injection** (`_normalize`): working memory in full + tier-aware semantic/episodic hits
  with group descriptions; excludes archive/daily. Replaces the current flat top-4 keyword inject.
- **Tools:** keep `vault.capture`/`search`/`read_note`/`write_note`; add `memory.forget`
  (=archive, never delete), `memory.pin` (force long-term, decay-immune), `memory.merge`. The
  `_MEM_WRITE` classify backstop still forces "remember/save" onto the tool path.
- **UI** (`web/index.html`): a memory browser showing the three tiers, groups + descriptions,
  salience, and manual pin/merge/forget — so you can see and steer placement.

## 9. Phased implementation

- **Phase 0 — Foundations.** Schema migration (`tier`, `salience`, `memory_vectors`,
  `memory_groups`), in-process CPU embedder wired, backfill + embed existing notes, archive stale
  `daily/run-*` logs.
- **Phase 1 — Retrieval.** Hybrid tier-aware scorer in `Retriever`/`index`; tier-aware context
  injection in the pipeline; extend `mem_probe.py` to assert semantic recall + tier weighting.
- **Phase 2 — Ingestion.** Post-run auto-extraction into episodic with embed + dedupe-on-write.
- **Phase 3 — Librarian.** Scheduled consolidation: tiering, merge, clustering, group descriptions,
  promotion, decay, archive (via `gardener`).
- **Phase 4 — Tools & UI.** `memory.forget/pin/merge`, memory browser with tiers/groups.
- **Phase 5 — Evals.** Memory scenarios in the live battery: capture→recall across tiers, dedupe,
  promotion after recurrence, grouping/description quality, archive-then-revive.

Each phase is independently shippable and leaves the system working.

## 10. Risks & mitigations

- **Auto-promote noise / privacy.** Aggressive capture means more junk and more sensitive data
  retained. Mitigate with `redact` on ingest, `confidence` thresholds for promotion, dedupe, and —
  because nothing is deleted — full auditability + easy archive.
- **Librarian cost.** LLM curation is expensive per call; batch it, run on idle/schedule, never in
  a user request's path.
- **CPU embedding latency.** Fine at personal scale; batch on write and cache. Revisit ANN only if
  the vault grows large.
- **Never-delete growth.** Bounded by archive-exclusion from recall + compaction via dedupe/merge,
  so retrieval stays fast even as raw storage grows.

## 11. Refined decisions (this pass)

- **Promotion** = type fast-path + recurrence + librarian override (3-layer, §6).
- **Short-term** = passive enriched working set (§1); extraction stays **post-run** (no per-turn
  live model call) — working memory is built by unioning, not by a live extractor.
- **Organization** = entity + topic **concept notes** (map-of-content) with descriptions (§2, §6).

## 12. Open questions (for later, not blocking)

- Embedding model choice (`bge-small` ~384d vs `nomic-embed` ~768d) — benchmark recall/latency on
  the real vault before locking.
- Exact decay half-lives and the archive floor — start from the §7 defaults, tune against Phase 5
  evals.
- Whether a `goal` should ever auto-demote if abandoned (currently: never delete, only decays in
  rank once superseded).

## 13. Appendix — worked mechanics & examples

Concrete walkthroughs so the abstract pieces above are unambiguous.

### A. Lifecycle of a durable fact — "my project codename is BlueFalcon"

1. **Say it (session S1).** After the run, the post-run extractor emits one atom:
   `{text:"project codename is BlueFalcon", type:"entity", subject:"project", confidence:1.0}`
   (explicit "remember" ⇒ confidence 1). It's redacted, embedded, and written to **episodic**
   (`inbox/…-bluefalcon.md`) with `source_run_id`.
2. **Dedupe-on-write.** Cosine vs existing notes < 0.92 ⇒ genuinely new (no merge). The v4 clutter
   of 3 identical captures would now collapse into one.
3. **Placement (type fast-path).** `type=entity` ⇒ promote to **long-term** immediately; attach to
   the **BlueFalcon** project entity group (create the `projects/BlueFalcon.md` concept note if
   absent) with description "Project codename: BlueFalcon" and a wikilink to the source atom.
4. **Recall (session S2, "what's my codename?").** Query embeds; the long-term note (no decay)
   scores top; injected into context; answer = "BlueFalcon." No tool call needed.
5. **Aging.** Long-term ⇒ effectively non-decaying; it persists indefinitely, never archived unless
   explicitly superseded (e.g. you rename the codename, which archives the old with
   `superseded_by`).

### B. Lifecycle of a transient fact — "I'm debugging the shell tool today"

- Extracted as `type:"task"`, confidence ~0.7 ⇒ stays **episodic**, ~14-day half-life.
- If it recurs next session ("still on the shell bug") ⇒ recurrence layer promotes the underlying
  fact ("working on the shell tool") toward long-term.
- If never referenced again ⇒ salience decays below 0.1 ⇒ **archived** (moved to `archive/`,
  excluded from recall), still revivable if a future query strongly matches it.

### C. Working-set assembly (passive, per request in a session)

```
working_set = dedupe(union(
    last K conversation turns (this thread),
    notes whose vault_path was returned/read by any recall this session,
    atoms extracted from earlier completed runs this session))
```
Capped to a token budget, injected labelled "Working memory (this session)". No model call — it's
just bookkeeping over things already seen. A new tab starts it empty.

### D. Retrieval scoring (worked)

Query "what's my codename?" → embed → gather candidates (semantic ∪ keyword ∪ graph), drop
`archive/` and `daily/`. Score each:
```
score = 0.6·cosine + 0.2·term_overlap + 0.1·graph_proximity + 0.1·tier_bias − decay(age,tier)
```
The BlueFalcon note: cosine ≈ 0.8, term_overlap on "codename" = 1, tier_bias(long) applied,
decay = 0 ⇒ ranks #1. Results are returned grouped under their concept note, each carrying its
description as the recall snippet.

### E. Grouping & splitting (librarian)

- Cluster episodic atoms by embedding (same-group at cosine > 0.75). Each cluster ⇒ a topic or
  entity group; create/update its concept note + LLM description + member wikilinks.
- If one note's atoms span >1 cluster (topic drift), **split** it into separate notes, each linked
  to the right group. Merges (dedupe > 0.92) go the other way. Both are reversible (archive lineage
  via `superseded_by`).

### F. Schema sketch (SQLite, `data/store.py`)

```
ALTER TABLE memory_notes ADD COLUMN tier TEXT DEFAULT 'medium';   -- short|medium|long|archive
ALTER TABLE memory_notes ADD COLUMN salience REAL DEFAULT 1.0;
ALTER TABLE memory_notes ADD COLUMN last_accessed REAL;
ALTER TABLE memory_notes ADD COLUMN access_count INTEGER DEFAULT 0;
ALTER TABLE memory_notes ADD COLUMN confidence REAL DEFAULT 0.7;
ALTER TABLE memory_notes ADD COLUMN group_id TEXT;
ALTER TABLE memory_notes ADD COLUMN source_run_id TEXT;
ALTER TABLE memory_notes ADD COLUMN superseded_by TEXT;
CREATE TABLE memory_vectors (vault_path TEXT PRIMARY KEY, embedding BLOB, model TEXT, dim INTEGER);
CREATE TABLE memory_groups  (id TEXT PRIMARY KEY, kind TEXT, title TEXT, description TEXT,
                             tier TEXT, centroid BLOB);
```
Frontmatter in each `.md` mirrors the note columns so the vault stays the human-editable source of
truth and SQLite is a rebuildable index.
