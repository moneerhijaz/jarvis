# JARVIS PLANv4 — The Thinking Graph

Replace the single free-form ReAct loop with a deterministic, typed **graph of
single-purpose nodes**. Every prompt flows through the same graph; each node does one
job under a strict input/output contract; tool calling is constrained; structured data
is rendered by code, never retyped by the model.

Decisions locked for this plan: **full replacement** of the ReAct loop; **structured
capability registry** (tags + schemas, no embeddings); **deterministic rendering** for
all structured results (model writes only the prose wrapper).

---

## 1. Why (root causes we are fixing)

The current `AgentLoop` asks one weak local model to do six jobs in a single turn —
decide, select a tool, emit the call, reason, transcribe results, and write prose. It
drops one each turn, and we patch whichever it dropped. The four structural faults:

1. **No phase separation.** Reasoning leaks into the answer; the model narrates intent
   instead of acting. → Split into nodes; keep reasoning in a non-user-facing scratch field.
2. **Model transcribes data.** It retypes tool output into prose and hallucinates
   (invented file paths). → Code renders structured results verbatim; model only writes
   the natural-language frame.
3. **Unconstrained control.** Free ReAct lets it pick wrong/invented tools or skip the
   call. → Planner binds each step to one specific tool; executor emits only that tool's
   arguments, validated against its schema.
4. **Flat tool registry + keyword selection.** No notion of what a tool does or returns.
   → Capability registry with tags + input/output schemas + examples, used for selection,
   feasibility, validation, and rendering.

---

## 2. Architecture overview

A directed graph with conditional edges. One typed **blackboard** state object flows
through it. Nodes are either *deterministic* (pure code) or *model* (one tight,
single-purpose LLM call). Every edge carries a validated contract; every node has a
bounded repair path.

```
Normalize → Classify ─┬─ direct ─────────────────────────────→ Render → Respond
                      ├─ infeasible ─────────────────────────→ Refuse/Offer → Respond
                      └─ tooled → Plan → ┌── per step ───────────────────┐
                                         │ BindArgs → CallTool            │
                                         │   → ValidateResult             │
                                         │   → on error: Repair / Replan  │
                                         └──────────────┬────────────────┘
                                                        ↓
                                            Aggregate → Render → Verify → Respond
```

Model calls per request: 1 (classify) + 1 (plan) + 1 per step (bind args) + optional 1
(verify) + 1 (prose frame in render). Each is small and single-purpose — far more
reliable on a 4B model than one giant call, and the total is bounded by the plan length.

---

## 3. The blackboard (typed shared state)

```python
class Turn(BaseModel):
    request: str                      # raw user message
    normalized: str = ""              # cleaned, references resolved
    history_context: str | None = None

    route: Literal["direct","tooled","infeasible"] | None = None
    assessment: str = ""              # short first-person, shown as a thought
    missing: str = ""; workaround: str = ""   # when infeasible

    plan: list[Step] = []             # typed steps (see §5)
    step_results: list[StepResult] = []   # one per executed step
    result: ResultBundle | None = None     # aggregated, typed (see §7)

    scratch: str = ""                 # private reasoning; NEVER user-facing
    answer: str = ""                  # final user-facing text
    artifacts: list[str] = []
    status: Literal["running","done","failed","refused"] = "running"
    errors: list[str] = []
```

Nodes read/write only their declared fields. Nothing in `scratch` is ever spoken or shown.

---

## 4. Nodes (purpose · contract · failure)

### N0 — Normalize (deterministic)
Clean markdown/whitespace; resolve follow-up references by folding recent history into
`history_context` only when the message refers back (pronoun/"again"/short follow-up,
the rule we already have). Output: `normalized`, `history_context`.

### N1 — Classify (model, 1 call)
Decide `route` ∈ {direct, tooled, infeasible} **and** feasibility, grounded against the
capability registry summary (not a guess). Returns `route`, `assessment`, and either a
direct `answer`, or `missing`+`workaround`. Single JSON contract; on parse failure →
default `tooled` (fail toward doing the work).

### N2a — Direct → Render (model wrote the answer in N1) → Respond.
### N2b — Infeasible → Refuse/Offer (deterministic template from `missing`/`workaround`).

### N3 — Plan (model, 1 call)
Produce a **typed plan**: an ordered list of `Step` objects, each bound to a concrete
tool id from the registry, with an intent and the expected output schema. The model
chooses tools *from the registry* (names validated); any unknown tool id is rejected and
re-asked once. Output: `plan`. Empty/!valid after repair → fail with a clear message.

### N4 — Execute (per step; mostly deterministic + 1 small model call for arg-binding)
For each `Step`:
- **BindArgs (model, tight):** given the step intent + prior results + the tool's input
  schema, emit ONLY that tool's arguments as JSON. The tool is fixed by the plan — the
  model cannot pick a different tool or "narrate." Validated against the schema; one
  repair retry on invalid args.
- **CallTool (deterministic):** execute via the existing executor (confirm gate, audit,
  rollback all unchanged). Risky tools still pause for approval.
- **ValidateResult (deterministic):** check the result against the tool's declared output
  schema and the step's expectation. ok + valid → record `StepResult`, mark step done.
- **On error/invalid:** Repair (retry a different way / skip an unreachable item and
  continue) up to a per-step cap; if a step is structurally impossible, Replan (re-enter
  N3 with the failure noted) once. Consecutive-failure governor (reset on progress) bails
  only on a true dead-end.

### N5 — Aggregate (deterministic)
Combine `step_results` into one typed `ResultBundle` (see §7). This is the single source
of truth for the answer — not the model's memory of the steps.

### N6 — Render (deterministic data + model prose frame)
The renderer picks a formatter by the bundle's `kind` (table/list/ranking/scalar/prose)
and formats the **data verbatim** from `ResultBundle`. A short model call writes only the
natural-language intro/summary ("Here are the five largest files on your C: drive:"),
which is concatenated with the code-rendered data. The model never emits the data rows,
so it cannot hallucinate paths/numbers.

### N7 — Verify (model, optional 1 call) + grounding (deterministic)
Deterministic grounding check first (paths/numbers in `answer` must appear in
`ResultBundle` — keep the code-level check we built). Then, if enabled, one grounded
model check: "does this answer the request, per the evidence?" Insufficient → one bounded
correction/replan cycle. With deterministic rendering this is mostly a backstop.

### N8 — Respond
Emit `answer` (+ artifacts), persist the turn, speak it. Streaming for the prose frame
only.

---

## 5. Step + StepResult contracts

```python
class Step(BaseModel):
    id: int
    intent: str                       # what this step accomplishes
    tool: str                         # MUST be a registered tool id
    expects: str                      # capability/output schema key it should return
    done: bool = False

class StepResult(BaseModel):
    step_id: int
    tool: str
    ok: bool
    data: dict | None = None          # validated against the tool's output schema
    error: str | None = None
```

---

## 6. Tool registry redesign (capability tags + schemas)

Extend `ToolSpec` so each tool is self-describing. Selection, feasibility, validation,
and rendering all read this — deterministic, offline, no embeddings.

```python
class ToolSpec(...):
    name: str
    capabilities: list[str]           # e.g. ["files.enumerate","disk.measure"]
    input_schema: type[BaseModel]     # already have args_model
    output_schema: dict               # declared shape of data on success
    output_kind: Literal["table","list","ranking","scalar","prose","action"]
    examples: list[dict]              # {request, args} few-shots for BindArgs
    risk, timeout_s, confirm, ...     # unchanged
```

- **Selection (`select_for`)** becomes capability-tag matching against the request's
  required capabilities (derived in Classify/Plan), replacing keyword substring scoring.
- **Feasibility** = "is every required capability covered by some tool?" — a real check,
  not the model guessing.
- **Validation** uses `output_schema` to confirm a tool returned what the step expected.
- **Rendering** uses `output_kind` to choose the deterministic formatter.

A `CapabilityIndex` maps capability → tools, and exposes a compact catalog string for the
Classify/Plan prompts.

---

## 7. ResultBundle + deterministic renderers

```python
class ResultBundle(BaseModel):
    kind: Literal["table","list","ranking","scalar","prose","action"]
    title: str = ""
    columns: list[str] = []           # for table/ranking
    rows: list[list] = []             # verbatim from tool data
    items: list[str] = []             # for list
    value: str = ""                   # for scalar
    text: str = ""                    # for prose
```

Renderers (pure functions): `render_ranking`, `render_table`, `render_list`,
`render_scalar`, `render_prose`. Example for the failing case — "5 largest files":
Aggregate builds a `ranking` bundle straight from `fs.largest_files.data.largest_files`;
`render_ranking` prints each path + human size verbatim; the model adds only "Here are
the five largest files on your C: drive:". Hallucination is impossible because the model
never touches the rows.

---

## 8. Reasoning / thinking

- All model nodes append `/no_think` and the client strips/relegates `reasoning_content`
  to `scratch` (we already do this).
- Reasoning is allowed only where useful (Plan, Repair) and stays in `scratch`. It is
  never rendered or spoken.
- Because each node is single-purpose with a strict output contract, there is no room for
  the model to ramble in place of acting.

---

## 9. Control, errors, governors

- **Bounded everything:** per-node repair cap, one global replan, consecutive-failure
  governor (reset on progress), wall-clock/step/token budgets (carry over).
- **Constrained acting:** the model never free-picks a tool mid-run; it binds args to the
  planned tool. This removes wrong-name, narrate-instead-of-call, and premature-finish in
  one stroke (there is no "finish early" — the graph finishes when steps are done).
- **Confirmations, audit, rollback, kill-switch:** unchanged; CallTool reuses the
  existing executor and ConfirmBroker.

---

## 10. How each root cause is fixed

| Root cause | Fix in the graph |
|---|---|
| Thinking leaks / narrates intent | Phased nodes; reasoning in `scratch`; acting is arg-binding to a fixed tool |
| Hallucinated/retyped data | Deterministic renderers from `ResultBundle`; model writes only prose frame |
| Unconstrained control | Plan binds tools; BindArgs emits only that tool's args (schema-validated) |
| Flat registry / keyword select | Capability registry: tags + I/O schemas + examples; deterministic selection & feasibility |

---

## 11. Migration (full replacement)

Phased build, but the end state replaces `AgentLoop.run`:

1. **Registry upgrade** — add `capabilities`, `output_schema`, `output_kind`, `examples`
   to `ToolSpec` and annotate existing tools; build `CapabilityIndex`. (No behavior change yet.)
2. **Blackboard + contracts** — `Turn`, `Step`, `StepResult`, `ResultBundle` models.
3. **Graph runner** — a `GraphRunner` with the nodes in §4; reuse executor/confirm/events.
4. **Deterministic renderers** — formatters per `output_kind` + Aggregate.
5. **Node prompts** — Classify, Plan, BindArgs, (Verify), prose-frame — each tiny and
   single-purpose, replacing the monolithic SYSTEM_PROMPT.
6. **Cut over** — `Application` calls `GraphRunner` instead of `AgentLoop`; emit the same
   events (`plan.created`, `model.thought`, `tool.*`, `run.*`) so the existing UI works
   unchanged. Delete the ReAct loop once parity is confirmed.
7. **Verify end-to-end** — the four canonical cases: simple chat (direct), "what's on my
   screen" (single tool), "5 largest files" (ranking render), an infeasible request
   (refuse/offer).

Events stay backward-compatible so the hologram UI, tape, and timeline need no changes.

---

## 12. Risks / open questions

- **Plan quality on a 4B model.** Mitigation: small registry catalog in the prompt,
  validated tool ids, one replan. If still weak, add a few-shot plan example per capability.
- **BindArgs for multi-arg tools.** Mitigation: per-tool `examples` as few-shots; schema
  validation + one repair.
- **Over-rigid plans.** Repair/Replan edges and the consecutive-failure governor keep it
  adaptive without returning to free ReAct.
- **Output-kind misclassification.** Default to `prose` with grounding check when unsure.
- **Open:** whether Classify and Plan can be merged into one call (fewer round-trips) or
  must stay separate (cleaner contracts). Recommend separate to start, measure, then maybe fuse.
```
