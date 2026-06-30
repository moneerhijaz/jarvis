# JARVIS PLANv5 — The Pipeline (clean slate)

A fresh runtime built **only** from this pipeline. Nothing is carried over from the previous
engine; this is the single source of truth for how a request is handled.

The logic is exactly the flowchart, run as a **dataflow pipeline**: a request recurses into
many small tasks, each task is one simple prompt, tasks wait on the tasks they depend on,
independent tasks run in parallel, and their results **converge back** into one coherent
answer. Coherence comes from how the small tasks are wired together, not from any single
prompt doing everything.

---

## 1. The pipeline (as drawn)

```
                              ┌───────────┐
                              │  Request  │
                              └─────┬─────┘
                                    v
                        ┌────────────────────────┐
                        │ Check tool requirements │
                        └───────────┬────────────┘
                                    v
                     ◇ Does it have all necessary tools? ◇
                          │ No                       │ Yes
                          v                          v
                 ┌──────────────────┐    ◇ Is it double verification? ◇  (read the flag)
                 │ FAIL, explain why│                │
                 └────────┬─────────┘                v
                          │              ┌────────────────────────┐
                          │              │  Check reasoning level │
                          │              └───────────┬────────────┘
                          │                          v
                          │            SINGLE (one model)        DOUBLE (brain + vision,
                          │            run MODULE once           same request, in parallel)
                          │                 │                          │
                          │            ┌────v────┐                ┌────v────┐
                          │            │ MODULE  │                │MODULE ×2│
                          │            └────┬────┘                └────┬────┘
                          │            ┌────v─────┐               ┌────v─────┐
                          │            │ Draft A  │               │Draft A+B │
                          │            └────┬─────┘               └────┬─────┘
                          │                 └──────────┬───────────────┘
                          │                            v
                          │               ◇ Was it double verification? ◇
                          │                   │ No                  │ Yes
                          │                   │                     v
                          │                   │        ◇ Do the two responses agree? ◇
                          │                   │            │ No (FAIL)     │ Yes
                          │                   v            v               v
                          │        ┌───────────────────────────────────────────┐
                          │        │ Verification layer:                         │
                          │        │ does the response answer the request?       │
                          │        └───────────┬──────────────────┬─────────────┘
                          │                Yes │              No  │
                          │                    v                  v
                          │               ┌─────────┐        ┌─────────┐
                          │               │ Answer  │        │  FAIL   │
                          │               └────▲────┘        └────┬────┘
                          │                    │                  │
                          └────────────────────┴──────────────────┘
                                   (every FAIL is delivered as the Answer, with its reason)
```

**MODULE** — "Check reasoning level" picks exactly one:

```
  ┌─ Direct ───────────► Answer once or twice (by verification level) ─────────────► Draft
  │
  ├─ Multistep ────────► Formulate multi-step plan ─► Loop until plan finishes ⟳ ──► Draft
  │
  └─ Multiresponse ───► Formulate multi-step plan      ─► Loop & pause until client ⟳
                        (incl. WHEN to ask for info)       answers ─► After loops done,
                                                           finish answer ──────────► Draft
```

---

## 2. Locked decisions

1. **Fail visibly. No reconciliation.** When something is wrong the run FAILS and says where
   and why. The model never gets a chance to argue itself right.
2. **Compare facts, not format.** Agreement checks extract the facts and compare those —
   "four" equals "4".
3. **Double = brain + vision, same request, in parallel, neither idle.** Compare the two
   independent responses. A task that *uses* vision to do its work is forced **single** (the
   vision model is busy being the eyes). A visual task can still be double if one screenshot
   is taken and handed to both, so each *sees* it and reasons on its own.
4. **Every Multiresponse question must be answered, or the run fails.** No guessing, no
   assumed default, no proceed-without-answer.
5. **The plan loop fails only when genuinely incapable.** If it can't do a step at all, it
   fails and explains why. It does not fabricate progress.
6. **Confirm before risky actions whenever NOT in full-permissions mode.**
7. **One explicit blackboard.** A single typed state object carries the request, the tasks,
   their results, and "the two responses" — every task declares what it reads and writes.

---

## 3. Stages (one node = one simple task)

- **Request** — the incoming message.
- **Check tool requirements** — work out what doing this would need.
- **Does it have all necessary tools?** — `No → FAIL (explain why)`; `Yes → continue`.
- **Verification level (single / double)** — read the flag for this request.
- **Check reasoning level** — choose `Direct | Multistep | Multiresponse`.
- **MODULE** — runs the chosen reasoning level (see §1).
- **Draft** — the response produced by the module (one draft if single, two if double).
- **Was it double?** — `No → Verification layer`; `Yes → Do the two responses agree?`
- **Do the two responses agree?** — `No → FAIL (show both)`; `Yes → Verification layer`.
- **Verification layer (does the response answer the request?)** — `Yes → Answer`;
  `No → FAIL`.
- **Answer** — the final response. **FAIL** is also delivered here, as an answer that states
  the reason.

---

## 4. Blackboard + tasks

```python
class Pipe:                       # the one state object for a request
    run_id; request
    feasible; missing             # from "does it have all necessary tools"
    verification: "single"|"double"
    level: "direct"|"multistep"|"multiresponse"
    tasks: dict[id, Task]         # the small tasks and their wiring
    results: dict[id, Result]     # each task's output (the converge inputs)
    draft_a; draft_b              # the one or two responses
    answer; status; failure       # failure = where + why (decision #1)

class Task:
    id; intent                    # the one simple thing this task does
    tool                          # the action it performs, if any
    depends_on: list[id]          # the tasks whose results it needs (dataflow edges)
    result; state                 # pending|ready|running|done|failed

class Question:
    task_id; text; answer         # answer filled when the client replies (Multiresponse)
```

A task's `result` is the small, finished piece it contributes. The converge step reads the
results it depends on and combines them — that's how the simple tasks become coherent logic.

---

## 5. How the pipelining runs (recurse → converge)

A request is executed as a graph of tasks:

1. A task is **ready** when every task in its `depends_on` is `done`.
2. Ready tasks run; a task that needs another's output **waits** on that task's result
   before it proceeds. This is "a prompt waiting on another prompt to answer."
3. Tasks with no dependency between them run **in parallel**; a converge task waits on
   several and merges them.
4. A task may itself be too big — it then **recurses**: it spawns its own small pipeline
   (its own plan → loop → draft) and waits for that to return a single result, which becomes
   its output. Recurse outward into simple tasks, converge their results back inward.
5. Recursion and fan-out are **bounded** by a shared budget (depth + total work) so a branch
   can't run away, and a task can't recurse into a copy of itself.

> Parallel reality: two prompts to the *same* model serialize. Real concurrency happens
> across the two hosts (brain on one, vision on the other) and while a task waits on an
> external action. The brain+vision **double** is genuinely parallel because they are
> different hosts — which is the point of "never let one sit idle while the other works."

---

## 6. Double verification (brain + vision)

- Used when vision is free to act as the second reasoner. A task that needs vision to do its
  job stays **single**.
- The same request (and, for the visual variant, the same single screenshot) goes to **both
  models at once**; each reasons independently and returns its own response.
- The two responses are compared on **facts** (decision #2). Equal facts → continue to the
  verification layer. **Any factual disagreement → FAIL, showing both responses** so it's
  clear exactly where they diverged (decision #1).
- Because the models are on separate hosts, the two passes finish in roughly the time of one.

---

## 7. Multiresponse (ask the client, then continue)

- The plan may include **ask** tasks: points where it needs information only the client has.
- An ask task poses the question, then **pauses the run** and waits for the answer; the tasks
  that depend on it stay blocked until it arrives.
- **The question must be answered for the run to proceed. If it isn't, the run FAILS**
  (decision #4) — nothing is assumed.
- Once answered, the dependent tasks become ready and the pipeline continues; after all such
  loops finish, the answer is finished and drafted.

---

## 8. Failure model

- **No reconciliation.** Disagreement, an unanswered question, or an incapable step ends the
  run with a precise `failure` (which task, why), delivered to the client as the answer
  (decision #1).
- **The plan loop is terminal only on true incapability** (decision #5) — it can't do the
  step at all. It never invents a result to keep going.
- Every task records its own state and error, so it's always visible *which* task failed.

---

## 9. Safety

- Before any risky action, if **not** in full-permissions mode, the run **pauses for
  confirmation** and proceeds only on approval (decision #6).

---

## 10. Build order (fresh runtime)

1. `Pipe` / `Task` / `Question` state objects.
2. The task scheduler: ready-set, run-in-parallel, wait-on-dependency, recursion + shared
   budget. Testable on its own with stub tasks.
3. The stage tasks: Check-tool-requirements, Check-reasoning-level, the MODULE (Direct /
   Multistep / Multiresponse), Draft, the verification layer, and FAIL→Answer.
4. Double verification: run brain + vision in parallel, fact-compare, fail-on-disagreement.
5. Multiresponse: the ask/pause/answer channel (pose question → park run → resume on reply →
   fail if unanswered).
6. Confirm-before-risky (non-full-permissions).
7. Make this the engine the app runs; verify the whole flow end to end.

---

## 11. Open questions
- Comparing facts for free-form prose (vs structured results) on a small model — start with
  structured/extractable facts only?
- Recursion depth default for your two hosts.
- Voice handling of a Multiresponse question (spoken question → spoken answer round-trip).
