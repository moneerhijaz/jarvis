# JARVIS PLANv6 — The Hardened Pipeline

v6 is v5's flowchart with the failsafes that were already in the code, now documented, plus a
second round of hardening that fixes why the failsafes weren't enough. The v5 doc
(`PLANv5_PIPELINE.md`) remains the reference for the flowchart, the blackboard, and the locked
decisions; this doc records only what v6 changes.

---

## 1. What v6 already added over v5 (previously undocumented)

These were in the code before this doc existed:

- **Stage retries** (`autonomy.max_retries`, default 3): feasibility classification, planning,
  double-match, and the final validity gate each re-attempt before failing with explanation.
- **Final-answer validity gate on EVERY request**: a model judge checks the draft against the
  request (strict correctness when `objective`, faithfulness-to-evidence when `grounded`,
  substance/non-refusal otherwise). Invalid → re-draft with the critique → re-check.
- **Deterministic grounding guard**: paths cited in the answer must appear in the tool
  evidence (`ungrounded_facts`), checked before the model judge.
- **Arg-repair loop**: a leaf whose tool call fails validation gets its arguments repaired
  (deterministic first, then model) up to 3 times before the leaf fails.
- **Double-verify gating**: cross-check only when `double` AND `objective`; vision-using runs
  forced single; degrade to single when no second host / unusable second pass.
- **Denial re-route**: a "direct" answer that falsely claims incapability is forced to
  multistep instead of being delivered.

**Why this still wasn't useful:** every retry re-rolled the same dice. The plan was a static
DAG made before anything ran, and the first leaf failure (after arg repair) killed the whole
run. No retry ever received new information. Separately, the scheduler could hang forever, and
two silent bugs (below) undermined verification and debuggability.

---

## 2. The v6 hardening round (this change)

### 2.1 Bounded re-planning — the "make it useful" change

When a step fails, the run no longer dies immediately. The runner re-plans the REMAINDER
(`autonomy.max_replans`, default 2), feeding the planner:

- the request,
- what already completed (task ids + their results — kept, never redone),
- exactly what failed and why,
- an instruction to NOT repeat the failed step unchanged.

Fail-visible (decision #1) is preserved: after the replans are spent, the run fails with the
original precise reason. Non-replannable failures stay terminal immediately: **cancelled,
denied by user, unanswered question, exhausted time/task budget, recursion limit, dependency
cycle** — user intent and budgets are never argued with.

Mechanics: completed tasks and their results stay on the blackboard; unfinished tasks are
dropped; new step ids that collide with kept ids are suffixed (`t1` → `t1_r1`); the compose
task is re-wired to converge old + new results. All replans spend from the same shared
`Budget`, so re-planning cannot run away.

### 2.2 Scheduler watchdog

The engine previously awaited task completion with no timeout — cancellation and the deadline
were only observed *between* completions, so one hung tool or an unanswered question froze the
run forever (the 1800 s wall clock was never enforced mid-flight). The scheduler now wakes
every second: cancel and `budget.expired()` are enforced while tasks are in flight, and
in-flight tasks are cancelled when the run dies.

### 2.3 Ask-question timeout (decision #4, actually enforced)

A Multiresponse question that goes unanswered now FAILS the run after
`autonomy.question_timeout_s` (default 600) instead of parking it forever. Answered questions
resume exactly as before.

### 2.4 Cycle detection up front

A planned dependency cycle is detected before scheduling and fails with the actual cycle
(`a -> b -> a`) instead of a late generic "stuck" error.

### 2.5 Fact-comparison fixes

- **Empty-set hole**: an answer with zero extractable facts was a subset of anything, so
  double-verification auto-passed. A factless pass can no longer vouch for a factful one — the
  comparison falls through to key terms.
- **Path styles**: `C:\a\b.txt` and `c:/a/b.txt` now normalize to the same fact (the
  forward-slash drive form wasn't recognized as a Windows path).

### 2.6 No more silent failures

- `_json()` (every classify/plan/judge call) logs the exception or the unparseable reply
  instead of silently returning `{}` — a dead model no longer masquerades as an empty plan.
- `_bind_args` logs when bound arguments fail the tool schema instead of swallowing it.

---

## 3. New config

```yaml
autonomy:
  question_timeout_s: 600   # unanswered Multiresponse question fails the run after this
  max_replans: 2            # re-plans of the remainder after a step fails (0 = v5 behavior)
```

---

## 4. Failure model (v6)

| Failure                                   | Behavior                                          |
| ----------------------------------------- | ------------------------------------------------- |
| Missing capability                        | Re-check ×retries → FAIL with what's missing + workaround offer |
| Empty/malformed plan                      | Re-plan blind ×retries → FAIL                     |
| Step fails (tool error)                   | Arg-repair ×3 → **re-plan remainder ×max_replans** → FAIL with step + reason |
| User denies a confirm                     | FAIL immediately (never replanned)                |
| Question unanswered                       | FAIL after `question_timeout_s` (never replanned) |
| Cancel / budget / depth                   | FAIL immediately (never replanned)                |
| Double passes disagree                    | Retry second pass ×retries → FAIL showing both    |
| Answer fails validity gate                | Re-draft with critique ×retries → FAIL with why   |

---

## 5. Known gaps (candidates for v7)

1. **Classification is one mega-prompt.** Feasibility, objectivity, groundedness,
   needs-user-info, and a full direct answer in one JSON reply is a lot for a small local
   model; a misparse silently defaults (`feasible=true`, `needs_tools=false`). Split into two
   tiny prompts, or derive `needs_tools` deterministically from the capability index.
2. **Args are bound blind.** `_bind_args` guesses arguments from the intent before execution;
   the repair loop compensates. An observe→act step (bind args AFTER reading dependency
   results, which is only partially done via `prior`) would cut most repairs.
3. **The click-regex layer** (~100 lines of English-only regexes forcing `ui.click` plans) is
   compensation for weak planning; as the planner improves it should shrink or move behind an
   intent-classifier task.
4. **`pipe.concurrency` is informational** — it's set and displayed but nothing schedules by
   it; the double pass is currently sequential after draft A. True brain+vision parallelism
   (fire both at MODULE time) is designed but not wired.
5. **Store statuses**: a refused run is persisted as `completed`; worth a `refused` status.

---

## 6. Tests

`tests/test_pipeline_engine.py` (no LM Studio needed) covers: dependency order + parallelism,
failure fails the run and cancels siblings, watchdog (deadline + cancel enforced mid-flight),
cycle detection, stuck detection, task budget, mid-run graph growth, re-plan recovery
(alpha fails → beta plan succeeds → compose converges), bounded re-plans then fail-visible,
non-replannable failures, id-collision renaming, ask timeout + ask resume, and the facts
fixes. Existing suites (`test_pipeline_arg_repair.py`, `test_agent_and_api.py`) still apply.
