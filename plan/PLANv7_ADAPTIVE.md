# JARVIS PLANv7 — Adaptive Pipeline

Diagram: `plan/pipeline_v7.drawio`. Builds on v6 (`PLANv6_HARDENED_PIPELINE.md`); this doc
records only the v7 deltas. The v7 thesis: **every retry must carry new information** (an
error, a critique, a workaround) and **the two hosts must never idle sequentially**.

## 1. Classification split (was: one mega-prompt)

`_classify` now does ONE small job: fill the flags (`needs_tools`, `feasible`, `missing`,
`workaround`, `needs_user_info`, `objective`, `grounded`). It no longer drafts the direct
answer inline — drafting happens in the module via `_answer_direct`, where the validity gate
already knows how to redraft it with a critique. Smaller prompt → fewer misparses on a local
model; one extra model call on the direct path is the accepted cost.

## 2. Routing fix

`needs_user_info` now forces **multiresponse regardless of tools**. Previously a tool-free
request needing clarification ("write the email" — to whom?) routed to direct, which cannot
ask. Priority: needs_user_info → multiresponse; needs_tools → multistep; else direct.

## 3. Workaround pass (was: blind feasibility re-checks ×3)

Infeasible + a plausible workaround (`web|shell|build_script`) → exactly ONE re-classify with
the workaround declared in the prompt ("you may accomplish this by running shell commands…").
Still infeasible → refuse immediately, and the failed workaround is NOT offered in the
refusal. The old loop asked the same question up to 3× and got the same answer.

## 4. Parallel double verification (was: sequential, `concurrency` unused)

`_double_eligible(pipe)` (double AND objective AND vision-free) is decided BEFORE drafting:

- **direct**: brain and vision draft the same request via `asyncio.gather`.
- **multistep**: the compose task fires brain-compose and vision-compose over the same
  evidence via `asyncio.gather`; draft B lands on the blackboard in parallel.

`pipe.concurrency = 2` now describes what actually happens. Since the hosts are separate
machines, the second opinion costs ~zero extra wall time.

## 5. Match: one regeneration max (was: retry ×3)

Re-rolling draft B until it happens to agree is agreement-by-exhaustion — it silently defeats
double verification. v7: compare the parallel drafts; on mismatch regenerate the second pass
ONCE; still disagreeing → FAIL showing both drafts and the differing facts. Unusable second
pass still degrades to single.

## 6. Unchanged from v6

Bounded replan-on-failure, arg repair, scheduler watchdog, ask timeout, cycle detection,
validity gate (redraft-with-critique ×retries), fail-as-answer, budgets.

## 7. Tests

v7 section in `tests/test_pipeline_engine.py`: multiresponse routing without tools, classify
no longer drafts, workaround single informed re-check (prompt carries the workaround), refuse
after one check when no workaround, failed workaround not re-offered, match regenerates once
then fails showing both, pre-produced agreeing draft B passes with zero regens, compose fires
brain+vision in parallel and stores draft B, subjective compose stays single.
