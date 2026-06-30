"""PLANv5 — the PipelineRunner: the stages wired onto the dataflow engine.

Lifecycle (exactly the flowchart):
  Normalize -> Classify (feasibility + reasoning level) -> [direct | plan->run->converge]
  -> (single | double) -> Verify -> Answer ;  any failure -> FAIL delivered as the answer.

Tasks run on the engine (jarvis.pipeline.engine). Each task is one small job:
  leaf=run a tool, subtask=recurse a sub-pipeline, ask=pause for the user, compose=converge.
Tool execution, confirm gate, events, and the model client are reused as substrate; all the
decision logic here is fresh per PLANv5 (nothing from the old engine).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid

from jarvis.pipeline.engine import run_pipeline
from jarvis.pipeline.facts import facts_agree, strip_reasoning, ungrounded_facts
from jarvis.pipeline.state import Budget, Pipe, Question, Result, Task
from jarvis.pipeline.types import RunResult
from jarvis.model.client import Message, ToolCall
from jarvis.security.redaction import redact_obj
from jarvis.tools.base import ToolContext

logger = logging.getLogger("jarvis.pipeline.runner")

QUESTION_ASKED = "question.asked"
RUN_STARTED = "run.started"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
PLAN_CREATED = "plan.created"
THOUGHT = "model.thought"
ACTION_CONFIRM_REQUESTED = "action.confirm_requested"
ACTION_CONFIRM_RESOLVED = "action.confirm_resolved"
RUN_CANCELLED = "run.cancelled"


class _Cancelled(Exception):
    pass


# Meta-tools from the old ReAct/graph engine. The v5 pipeline converges deterministically via
# the compose node, so the planner must NEVER see or bind to these — otherwise it plans a
# `final.answer` step that can't be given a valid `content` and the run crashes.
_META = {"final.answer", "plan.update", "tools.list"}
_THINK = re.compile(r"(?is)<think>.*?</think>")
_VISIBLE_CLICK = re.compile(
    r"\b(click|clicking|clicked|tap|tapping|tapped|select|close)\b|"
    r"\bopen\b(?=[^.\n]{0,120}\b(tab|folder|file|item|entry|button|icon|option)\b)",
    re.I,
)
_NAMED_TARGET = re.compile(r"\b(?:named|called|label(?:ed|led))\s+[\"'`]?([A-Za-z0-9][\w -]{0,80})[\"'`]?", re.I)
_CLICK_TARGET = re.compile(r"\b(?:click|tap|select|close)\s+(?:on\s+)?(?:the\s+)?[\"'`]?([^\"'`.,;\n]+)", re.I)
_OPEN_TARGET = re.compile(
    r"(?:^|[.!?]\s+|\b(?:please|can you|could you|i want you to|i need you to|jarvis)\s+)"
    r"open\s+(?:up\s+)?(?:the\s+)?[\"'`]?([^\"'`.,;\n]+)",
    re.I,
)
_ORDINAL_VISIBLE_TARGET = re.compile(
    r"\b((?:first|second|third|fourth|fifth|last|next)\s+"
    r"[A-Za-z0-9][\w -]{0,70}?\s+"
    r"(?:tab|folder|file|item|entry|button|icon|option))\b",
    re.I,
)
_TARGET_SUFFIX = re.compile(
    r"\b(?:for me|please|thanks|thank you|that's it|so that\b.*|so it\b.*|to open\b.*|and then\b.*)$",
    re.I,
)
_LANDMARK_CONTEXT = re.compile(
    r"\b(?:which|that)\s+(?:shows|has|says|contains|is labeled|is labelled)\s+([^.,;\n]{1,90})",
    re.I,
)
_VISUAL_CORRECTION = re.compile(
    r"\b("
    r"miss(?:ed|es|ing)?|wrong|incorrect|not\s+click(?:ing|ed)?|didn'?t\s+click|"
    r"doesn'?t\s+click|keep\s+(?:on\s+)?miss(?:ing)?|try\s+again|that\s+(?:tab|button|folder|item|entry|icon)"
    r")\b",
    re.I,
)
_VISUAL_NOUN = re.compile(r"\b(tab|button|folder|item|entry|icon|browser|screen|monitor|top|bottom|left|right|youtube)\b", re.I)
_DESCRIBED_TARGET = re.compile(
    r"\b(?:first|second|third|fourth|fifth|last|next)\s+(?:one|tab|folder|item|entry)?\s*"
    r"(?:is|=|was|will be)\s+(?:just\s+)?(?:a\s+|an\s+)?(?:simple\s+)?[\"'`]?"
    r"([A-Za-z0-9][\w -]{0,80})[\"'`]?",
    re.I,
)
_THERE_IS_TARGET = re.compile(
    r"\bthere\s+(?:is|will be|is going to be)\s+(?:an?\s+|one\s+)?(?:named\s+|called\s+)?[\"'`]?"
    r"([A-Za-z0-9][\w -]{0,80})[\"'`]?",
    re.I,
)
_WEAK_CLICK_TARGET = re.compile(r"^((that|it|this|one)\b.*|so that\b.*|to open\b.*|so it\b.*)$", re.I)


def _extract_json(text: str) -> dict:
    """Pull the first balanced {...} that parses as JSON out of a model reply — tolerating
    reasoning/prose around it (and a leaked <think> block). Brace-matching beats a greedy regex
    when the model rambles before/after the JSON, which is the main cause of empty plans."""
    t = _THINK.sub("", text or "")
    start = t.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(t)):
            c = t[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start:i + 1])
                    except Exception:
                        break          # malformed -> try the next "{"
        start = t.find("{", start + 1)
    return {}


# A "direct" answer that falsely claims incapability the tools cover -> re-route to tooled.
_DENIAL = re.compile(
    r"\b(can(?:no|')?t|cannot|unable to|do(?:n'?t| not)? have (?:access|the ability)|no access|"
    r"i'?m (?:just )?(?:a|an) (?:text|language))\b.{0,40}\b"
    r"(see|view|access|screen|display|your (?:screen|computer|files|system))\b", re.I)


class PipelineRunner:
    def __init__(self, settings, model, registry, executor, events, store=None, vault=None,
                 retriever=None, rollback=None, policy=None) -> None:
        self.settings = settings
        self.model = model
        self.registry = registry
        self.executor = executor
        self.events = events
        self.store = store
        self.vault = vault
        self.retriever = retriever
        self.rollback = rollback
        self.policy = policy
        self._cancelled: set[str] = set()
        self._contexts: dict[str, ToolContext] = {}
        self._questions: dict[str, asyncio.Future] = {}   # question id -> future (suspend/resume)
        self.resolve_model = None      # set by Application
        self.confirm = None            # set by Application: ConfirmBroker

    # ===================== entry =====================
    async def run(self, req) -> RunResult:
        pipe = Pipe(run_id=req.run_id, thread_id=getattr(req, "thread_id", None), request=req.message)
        model = self._pick_model(req)
        budget = Budget(max_tasks=(self.settings.limits.max_run_steps or 40),
                        max_depth=getattr(self.settings.autonomy, "max_depth", 2),
                        deadline_s=self.settings.limits.max_wall_clock_s)
        ctx = ToolContext(run_id=req.run_id, working_directory=getattr(req, "working_directory", None),
                          settings=self.settings, rollback=self.rollback, policy=self.policy,
                          vault=self.vault, retriever=self.retriever, store=self.store,
                          registry=self.registry, project=getattr(req, "project", None))
        ctx._cancelled = lambda: req.run_id in self._cancelled
        self._contexts[req.run_id] = ctx
        if self.store:
            self.store.update_run(req.run_id, status="running", started_at=time.time(),
                                  model_provider=getattr(self.model, "provider", ""))
        await self.events.emit(req.run_id, RUN_STARTED, {"request": req.message}, pipe.thread_id)

        retries = max(1, getattr(self.settings.autonomy, "max_retries", 3))
        try:
            self._normalize(pipe, req)
            # Stage 1 (v6): tool requirements + feasibility — re-checked up to `retries` times
            # before failing with explanation.
            for attempt in range(retries):
                pipe.tool_fail = attempt
                await self._classify(pipe, model)
                if pipe.feasible:
                    break
                if attempt < retries - 1:
                    await self.events.emit(req.run_id, THOUGHT,
                                           {"content": f"Re-checking tool requirements ({attempt + 1}/{retries})…"})
            if not pipe.feasible:
                await self._say(pipe, f"Feasibility: NO — missing {pipe.missing}. Failing with explanation.")
                pipe.answer = self._refusal(pipe)
                pipe.status = "refused"
                return await self._respond(pipe, ctx)

            # v6: concurrency = 2 only when a second pass is actually worth it — double on AND an
            # objective (single-correct-answer) request. Subjective requests have no "right" answer,
            # so there's nothing to cross-check: run one pass and take it.
            pipe.concurrency = 2 if (pipe.verification == "double" and pipe.objective) else 1
            vmode = "objective" if pipe.objective else ("grounded" if pipe.grounded else "open")
            await self._say(pipe, f"Tools: {'none' if pipe.level == 'direct' else 'needed'} · "
                                  f"objective: {'yes' if pipe.objective else 'no'} · "
                                  f"grounded: {'yes' if pipe.grounded else 'no'} · feasible: yes")
            await self._say(pipe, f"Reasoning level: {pipe.level} · validation: {vmode}")
            await self._say(pipe, f"Verification: {pipe.verification} (concurrency {pipe.concurrency})")
            await self.events.emit(req.run_id, PLAN_CREATED,
                                   {"selected_tools": [], "steps": [], "approach": pipe.level, "assessment": ""})

            if pipe.level == "direct":
                await self._say(pipe, "Direct: answering without tools.")
                pipe.draft_a = pipe.draft_a or Result(kind="prose", text="", source="brain")
            else:
                # 3-try planner: planning is nondeterministic (a reasoning leak or malformed JSON
                # yields no tasks), so re-plan up to `retries` before failing.
                for attempt in range(retries):
                    pipe.tasks.clear()
                    await self._plan(pipe, model)
                    if pipe.tasks:
                        break
                    if attempt < retries - 1:
                        await self._say(pipe, f"Planning produced nothing; retrying ({attempt + 1}/{retries}).")
                if not pipe.tasks:
                    return await self._fail(pipe, "could not form a plan for this request after retries")
                steps = [t.intent for t in pipe.tasks.values() if t.kind != "compose"]
                await self.events.emit(req.run_id, PLAN_CREATED,
                                       {"selected_tools": sorted({t.tool for t in pipe.tasks.values() if t.tool}),
                                        "steps": [t.intent for t in pipe.tasks.values()],
                                        "approach": pipe.level, "assessment": ""})
                await self._say(pipe, f"Plan ready: {len(steps)} step(s). Executing.")
                await run_pipeline(pipe, self._run_task, budget, lambda: req.run_id in self._cancelled)
                if pipe.status == "failed":
                    return await self._fail(pipe, pipe.failure or "execution failed")
                await self._say(pipe, "All steps complete. Drafting answer.")
                pipe.draft_a = self._terminal_result(pipe)

            # v6 double-match: gated on double AND objective; retry up to `retries`, else FAIL.
            if self._wants_double(pipe):
                await self._say(pipe, "Double verification: cross-checking brain vs vision.")
                if not await self._match_passes(pipe, retries):
                    return await self._fail(pipe, pipe.failure or "the two passes disagree")
            elif pipe.verification == "double":
                await self._say(pipe, "Double verification: skipped (not an objective single-answer request).")

            # v6 final-answer validity gate: runs on EVERY request. A model judges the answer;
            # if not valid, re-draft with the critique, up to `retries`, then FAIL with reason.
            await self._say(pipe, "Verification layer: checking the answer.")
            if not await self._finalize_valid(pipe, model, retries):
                return await self._fail(pipe, pipe.failure or "answer failed validation")
            await self._say(pipe, "Verification layer: passed.")

            pipe.answer = ((pipe.draft_a.text if pipe.draft_a else "") or "").strip() or "Done."
            return await self._respond(pipe, ctx)
        except _Cancelled:
            return await self._cancel(pipe)
        except Exception as e:
            logger.exception("pipeline run failed")
            return await self._fail(pipe, f"engine error: {e}")
        finally:
            self._contexts.pop(req.run_id, None)
            self._cancelled.discard(req.run_id)

    # ===================== Normalize =====================
    _FOLLOWUP = re.compile(r"\b(it|its|that|this|those|these|them|they|again|same|previous|last|"
                           r"continue|resume|redo|retry|also|too|instead|he|she|him|her|the one)\b", re.I)

    def _normalize(self, pipe: Pipe, req) -> None:
        pipe.normalized = " ".join((req.message or "").split())
        g = pipe.normalized
        if (self._FOLLOWUP.search(g) or len(g.split()) <= 4) and self.store and pipe.thread_id:
            lines = []
            for m in self.store.list_messages(pipe.thread_id, limit=6):
                if m.get("content") and m.get("role") in ("user", "assistant"):
                    who = "User asked" if m["role"] == "user" else "You answered"
                    lines.append(f"- {who}: {' '.join(str(m['content']).split())[:240]}")
            if lines:
                pipe.history_context = ("Earlier in this conversation (context only, already "
                                        "handled):\n" + "\n".join(lines))

    # ===================== Stage 1: tool requirements + feasibility (per your graph) =====================
    # The model only reports WHAT the task needs. The code derives the reasoning level, so the
    # tool gate always runs first and ANY tool requirement forces multistep — "direct" is reserved
    # strictly for requests that need no tools at all.
    async def _classify(self, pipe: Pipe, model) -> None:
        caps = ", ".join(self.registry.capability_index().capabilities())
        sysp = (
            "Work out what this request NEEDS before answering. You CAN see the screen "
            "(screen.look / screen.read_text through the vision model), read files, run commands, control apps, and browse "
            "the web — all through tools. NEVER claim you cannot see the screen or access this "
            "computer; that is false. Anything about the user's screen, files, apps, windows, "
            "system, or anything 'right now' / 'currently' / 'my …' on this PC, or that needs the "
            "web or running something, REQUIRES tools. Respond ONLY JSON:\n"
            '{"needs_tools": true|false,        // does it need ANY tool/action on the computer or web?\n'
            ' "feasible": true|false,           // can it be done with the available capabilities?\n'
            ' "missing": "<what is lacking, if not feasible>",\n'
            ' "workaround": "web|shell|build_script|none",\n'
            ' "needs_user_info": true|false,    // must you ask the user for info you cannot get yourself?\n'
            ' "objective": true|false,          // single non-arguable correct answer (math/fact/count/path)?\n'
            ' "grounded": true|false,           // must the answer reflect what the tools find '
            '(e.g. summarize/describe my screen or a file)? true whenever the answer depends on tool output\n'
            ' "answer": "<full answer — ONLY if needs_tools is false>"}\n'
            f"Available capabilities: {caps} /no_think")
        data = await self._json(model, sysp, self._user(pipe))
        pipe.feasible = bool(data.get("feasible", True))
        pipe.objective = bool(data.get("objective", False))
        pipe.grounded = bool(data.get("grounded", False)) or bool(data.get("needs_tools", False))
        if not pipe.feasible:
            pipe.missing = str(data.get("missing") or "the capability this needs").strip()
            wk = str(data.get("workaround", "none")).lower()
            pipe.workaround = wk if wk in ("web", "shell", "build_script", "none") else "none"
            return
        # Derive the reasoning level in CODE (the model doesn't pick it):
        #   needs tools           -> multistep (or multiresponse if it must ask the user)
        #   no tools needed       -> direct, using the model's answer
        needs_tools = bool(data.get("needs_tools", False))
        if self._visual_correction_request(pipe):
            needs_tools = True
            pipe.grounded = True
        ans = strip_reasoning(str(data.get("answer") or "").strip())
        if not needs_tools and ans and not _DENIAL.search(ans):
            pipe.level = "direct"
            pipe.draft_a = Result(kind="prose", text=ans, source="brain")
        elif bool(data.get("needs_user_info")):
            pipe.level = "multiresponse"
        else:
            pipe.level = "multistep"     # any tool requirement (or empty/denial answer) -> multistep
        # verification level: "single" forces one pass; "double"/"auto" need a 2nd model host.
        pref = getattr(self.settings.autonomy, "verification", "auto")
        pipe.verification = "single" if pref == "single" else (
            "double" if self._has_second_model() else "single")

    def _refusal(self, pipe: Pipe) -> str:
        offer = {"web": " I could search the web for a way if you'd like.",
                 "shell": " I might manage it with a custom command — want me to try?",
                 "build_script": " I could try writing a small script — want me to attempt that?",
                 }.get(pipe.workaround, "")
        return f"I don't think I can do this one — I'm missing {pipe.missing}.{offer}"

    # ===================== Plan -> DAG =====================
    async def _plan(self, pipe: Pipe, model) -> None:
        if self._force_visible_click_plan(pipe):
            return
        names = [n for n in self.registry.names() if n not in _META]   # hide meta-tools from the planner
        catalog = self.registry.capability_index().catalog(names)
        ask_note = (' For a step that needs information only the user has, use '
                    '{"kind":"ask","question":"..."} and make later steps depend on it.'
                    if pipe.level == "multiresponse" else "")
        sysp = (
            "Break the request into a small graph of steps. Respond ONLY JSON: "
            '{"tasks":[{"id":"t1","intent":"...","tool":"<exact tool id>","depends_on":[]}, ...]}. '
            "Each step binds to ONE real tool id (they contain a dot, e.g. screen.look, "
            "input.press). depends_on lists the ids whose results this step needs. Keep it minimal."
            + ask_note + "\nTools:\n" + catalog + " /no_think")
        data = await self._json(model, sysp, self._user(pipe), max_tokens=700)
        raw = data.get("tasks") if isinstance(data, dict) else None
        if not raw and isinstance(data, list):
            raw = data
        valid_ids: set[str] = set()
        for s in (raw or [])[:8]:
            if not isinstance(s, dict):
                continue
            kind = str(s.get("kind", "leaf")).lower()
            tid = str(s.get("id") or f"t{len(pipe.tasks) + 1}")
            if kind == "ask":
                pipe.add(Task(id=tid, kind="ask", intent=str(s.get("intent") or "ask the user"),
                              question=str(s.get("question") or s.get("intent") or "Could you clarify?"),
                              depends_on=[d for d in (s.get("depends_on") or []) if isinstance(d, str)]))
                valid_ids.add(tid)
                continue
            tool = self._resolve_tool(str(s.get("tool", "")))
            if not tool:
                continue
            pipe.add(Task(id=tid, kind="leaf", intent=str(s.get("intent") or tool), tool=tool,
                          args=s.get("args") if isinstance(s.get("args"), dict) else {},
                          depends_on=[d for d in (s.get("depends_on") or []) if isinstance(d, str)]))
            valid_ids.add(tid)
        # prune dangling deps, then add a final compose task that converges everything
        for t in pipe.tasks.values():
            t.depends_on = [d for d in t.depends_on if d in valid_ids]
        if pipe.tasks:
            cid = "compose"
            pipe.add(Task(id=cid, kind="compose", intent="compose the answer",
                          depends_on=list(valid_ids)))

    def _force_visible_click_plan(self, pipe: Pipe) -> bool:
        """Explicit visible target actions should use the screen-targeting click tool.

        This avoids any window-list/focus shortcut for screen UI targets.
        ui.click is vision-only: whole-screen screenshot -> local grounding model -> click.
        """
        text = pipe.normalized or ""
        is_click = bool(_VISIBLE_CLICK.search(text))
        is_retry = self._visual_correction_request(pipe)
        if not self._has_tool("ui.click") or not (is_click or is_retry):
            return False
        target = self._visible_click_target(text)
        if not target and pipe.history_context:
            target = self._visible_click_target(self._user(pipe))
        target = self._enrich_visual_target(target, text)
        if not target:
            return False
        click = pipe.add(Task(
            id="t1",
            kind="leaf",
            intent=f"Click the visible screen target '{target}'",
            tool="ui.click",
            args={"target": target},
            depends_on=[],
        ))
        pipe.add(Task(id="compose", kind="compose", intent="compose the answer", depends_on=[click.id]))
        return True

    def _visual_correction_request(self, pipe: Pipe) -> bool:
        text = self._user(pipe)
        current = pipe.normalized or ""
        return bool(_VISUAL_CORRECTION.search(current) and _VISUAL_NOUN.search(text))

    def _has_tool(self, name: str) -> bool:
        try:
            return bool(self.registry.get(name))
        except Exception:
            return False

    @staticmethod
    def _visible_click_target(text: str) -> str:
        fallback = PipelineRunner._described_click_target(text)
        ordinal = list(_ORDINAL_VISIBLE_TARGET.finditer(text))
        if ordinal:
            return PipelineRunner._enrich_visual_target(PipelineRunner._clean_click_target(ordinal[-1].group(1)), text)
        named = _NAMED_TARGET.search(text)
        if named:
            return PipelineRunner._clean_click_target(named.group(1))
        clicked = _CLICK_TARGET.search(text)
        if clicked:
            raw = clicked.group(1).strip()
            if _WEAK_CLICK_TARGET.match(raw.lower()):
                return fallback
            return PipelineRunner._clean_click_target(raw)
        opened = _OPEN_TARGET.search(text)
        if opened:
            raw = opened.group(1).strip()
            if _WEAK_CLICK_TARGET.match(raw.lower()):
                return fallback
            return PipelineRunner._clean_click_target(raw)
        return fallback

    @staticmethod
    def _enrich_visual_target(target: str, text: str) -> str:
        target = " ".join((target or "").split()).strip()
        if not target:
            return ""
        extras: list[str] = []
        landmark = list(_LANDMARK_CONTEXT.finditer(text or ""))
        if landmark:
            val = PipelineRunner._clean_click_target(landmark[-1].group(1))
            if val and val.lower() not in target.lower():
                extras.append(f"with {val}")
        low = (text or "").lower()
        for phrase in ("right half", "left half", "top", "bottom"):
            if phrase in low and phrase not in target.lower():
                extras.append(f"on the {phrase}")
                break
        if extras:
            target = f"{target} {' '.join(extras)}"
        return target

    @staticmethod
    def _clean_click_target(raw: str) -> str:
        target = _TARGET_SUFFIX.sub("", raw).strip(" '\"`:-")
        words = target.split()
        if len(words) > 14:
            target = " ".join(words[:14])
        return target

    @staticmethod
    def _described_click_target(text: str) -> str:
        candidates = []
        for match in _DESCRIBED_TARGET.finditer(text):
            candidates.append(match.group(1))
        for match in _THERE_IS_TARGET.finditer(text):
            candidates.append(match.group(1))
        if not candidates:
            return ""
        target = PipelineRunner._clean_click_target(candidates[-1])
        noun = PipelineRunner._click_context_noun(text)
        if noun and noun not in target.lower().split():
            return f"{target} {noun}"
        return target

    @staticmethod
    def _click_context_noun(text: str) -> str:
        low = text.lower()
        for noun in ("tab", "folder", "button", "icon", "entry", "item"):
            if re.search(rf"\b{noun}s?\b", low):
                return noun
        return ""

    # ===================== task runner (dispatch) =====================
    async def _run_task(self, task: Task, pipe: Pipe, budget: Budget) -> Result:
        if pipe.run_id in self._cancelled:
            raise _Cancelled()
        if task.kind == "leaf":
            return await self._exec_leaf(task, pipe)
        if task.kind == "subtask":
            return await self._exec_subtask(task, pipe, budget)
        if task.kind == "ask":
            return await self._exec_ask(task, pipe)
        if task.kind == "compose":
            return await self._exec_compose(task, pipe)
        # pass-through
        ins = pipe.inputs_for(task)
        return next(iter(ins.values()), Result(kind="none"))

    async def _exec_leaf(self, task: Task, pipe: Pipe) -> Result:
        spec = self.registry.get(task.tool)
        if spec is None:
            return Result(ok=False, error=f"no such tool '{task.tool}'")     # incapable -> fail (decision #5)
        ctx = self._contexts[pipe.run_id]
        model = self._pick_default_model()
        args = task.args or await self._bind_args(model, task, spec, pipe)
        repair_retries = 3
        last_error = "tool failed"
        for attempt in range(repair_retries + 1):
            call = ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=task.tool, arguments=args)
            if not await self._confirm(call, spec, pipe.run_id):
                return Result(ok=False, error="denied by user")
            result = await self.executor.execute(call, ctx)
            if getattr(result, "ok", False):
                for a in getattr(result, "artifacts", []) or []:
                    pipe.artifacts.append(a.path)
                return Result(ok=True, kind=getattr(spec, "output_kind", "prose") or "prose",
                              data=getattr(result, "data", None) or {}, source=task.tool)

            err_obj = getattr(result, "error", None)
            last_error = getattr(err_obj, "message", None) or "tool failed"
            if attempt >= repair_retries or not self._repairable_tool_error(task, err_obj):
                break

            fixed_args, why = self._deterministic_repair_args(task, pipe, args, last_error)
            if fixed_args is None:
                fixed_args, why = await self._repair_args(
                    model, task, spec, pipe,
                    previous_args=args,
                    previous_error=last_error,
                    attempt=attempt + 1,
                    max_attempts=repair_retries,
                )
            await self.events.emit(
                pipe.run_id,
                THOUGHT,
                {
                    "content": (
                        f"{task.tool} arguments failed validation; repairing "
                        f"({attempt + 1}/{repair_retries}).\n"
                        f"Why: {why}\n"
                        f"Previous args: {self._json_preview(args)}\n"
                        f"Repaired args: {self._json_preview(fixed_args)}"
                    ),
                    "tool": task.tool,
                    "why": why,
                    "previous_args": args,
                    "repaired_args": fixed_args,
                    "attempt": attempt + 1,
                    "retries": repair_retries,
                },
            )
            args = fixed_args

        return Result(ok=False, error=f"{task.tool}: {last_error}")

    async def _exec_subtask(self, task: Task, pipe: Pipe, budget: Budget) -> Result:
        if not budget.depth_ok(pipe.depth + 1):
            return Result(ok=False, error="recursion limit reached")
        child = Pipe(run_id=f"{pipe.run_id}.{task.id}", thread_id=pipe.thread_id,
                     request=task.goal or task.intent, depth=pipe.depth + 1)
        model = self._pick_default_model()
        await self._classify(child, model)
        if not child.feasible:
            return Result(ok=False, error=f"sub-task infeasible: {child.missing}")
        if child.level != "direct":
            await self._plan(child, model)
            if not child.tasks:
                return Result(ok=False, error="sub-task could not be planned")
            await run_pipeline(child, self._run_task, budget, lambda: pipe.run_id in self._cancelled)
            if child.status == "failed":
                return Result(ok=False, error=child.failure or "sub-task failed")
            child.draft_a = self._terminal_result(child)
        return child.draft_a or Result(ok=False, error="sub-task produced nothing")

    async def _exec_ask(self, task: Task, pipe: Pipe) -> Result:
        qid = f"q_{uuid.uuid4().hex[:8]}"
        q = Question(id=qid, task_id=task.id, text=task.question)
        pipe.questions.append(q)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._questions[qid] = fut
        pipe.status = "awaiting_user"
        await self.events.emit(pipe.run_id, QUESTION_ASKED, {"question_id": qid, "text": task.question})
        try:
            answer = await fut                       # parked until the API resolves it
        except asyncio.CancelledError:
            return Result(ok=False, error="question was not answered")
        finally:
            self._questions.pop(qid, None)
        if answer is None:
            return Result(ok=False, error="question was not answered")   # decision #4
        pipe.status = "running"
        q.answer = answer
        q.answered = True
        return Result(ok=True, kind="prose", text=str(answer), data={"answer": answer}, source="user")

    async def _exec_compose(self, task: Task, pipe: Pipe) -> Result:
        inputs = pipe.inputs_for(task)
        action_answer = self._action_answer(pipe, inputs)
        if action_answer:
            evidence = "\n".join(f"- {r.source or k}: {json.dumps(r.data, default=str)[:600] or r.text}"
                                 for k, r in inputs.items())
            return Result(ok=True, kind="prose", text=action_answer,
                          data={"evidence": evidence}, source="brain")
        return await self._compose(self._pick_default_model(), pipe, inputs, source="brain")

    def _action_answer(self, pipe: Pipe, inputs: dict[str, Result]) -> str:
        ui_clicks = [r for r in inputs.values() if r.ok and r.source == "ui.click"]
        if not ui_clicks:
            return ""
        data = ui_clicks[-1].data or {}
        target = str(data.get("clicked") or self._visible_click_target(pipe.normalized or "") or "that").strip()
        label = self._friendly_target_label(target)
        req = (pipe.normalized or "").lower()
        if "close" in req:
            return f"Done, I clicked {label} for you."
        if any(word in req for word in ("open", "opens", "opened")) and "tab" in req:
            return f"Done, I clicked the {label} and opened it for you."
        return f"Done, I clicked the {label}."

    @staticmethod
    def _friendly_target_label(target: str) -> str:
        clean = " ".join((target or "target").split()).strip(" .")
        low = clean.lower()
        if low.startswith(("the ", "a ", "an ")):
            return clean
        if any(noun in low.split() for noun in ("tab", "folder", "button", "icon", "entry", "item")):
            return clean
        return clean

    # ===================== converge / draft =====================
    async def _compose(self, model, pipe: Pipe, inputs: dict[str, Result], source: str,
                       critique: str = "") -> Result:
        if not inputs:
            return Result(kind="prose", text="", source=source)
        evidence = "\n".join(f"- {r.source or k}: {json.dumps(r.data, default=str)[:600] or r.text}"
                             for k, r in inputs.items())
        crit = f" A previous attempt was judged insufficient: {critique}. Fix that." if critique else ""
        sysp = ("Write the final answer to the user using ONLY the results below. Copy any file "
                "paths, names and numbers EXACTLY from them — never invent. Plain text, concise."
                + crit + " /no_think")
        user = f"Request: {pipe.normalized}\nResults:\n{evidence}"
        try:
            resp = await asyncio.to_thread(lambda: model.chat(
                [Message(role="system", content=sysp), Message(role="user", content=user)],
                None, temperature=0.2, max_tokens=700))
            txt = strip_reasoning((resp.content or "").strip())
        except Exception as e:
            return Result(ok=False, error=f"compose failed: {e}")
        if not txt:
            return Result(ok=False, error="model returned only reasoning — disable thinking in LM Studio")
        return Result(ok=True, kind="prose", text=txt,
                      data={"evidence": evidence}, source=source)

    def _terminal_result(self, pipe: Pipe) -> Result:
        """The compose task's output, or the last successful result if there's no compose."""
        if "compose" in pipe.results:
            return pipe.results["compose"]
        oks = [r for r in pipe.results.values() if r.ok]
        return oks[-1] if oks else Result(ok=False, error="no result produced")

    # ===================== double verification (brain + vision) =====================
    def _has_second_model(self) -> bool:
        m = self.settings.models
        return bool(getattr(m, "vision_base_url", "")) and self.resolve_model is not None

    def _wants_double(self, pipe: Pipe) -> bool:
        if pipe.verification != "double":
            return False
        if not pipe.objective:           # only cross-check single-answer requests (decision)
            return False
        # vision-using runs are forced single (the VLM is busy being the eyes)
        used = {t.tool for t in pipe.tasks.values() if t.tool}
        vision_caps = {"screen.describe", "screen.understand", "ui.locate"}
        for tool in used:
            spec = self.registry.get(tool)
            if spec and (set(getattr(spec, "capabilities", [])) & vision_caps):
                return False
        return pipe.draft_a is not None and bool((pipe.draft_a.text or "").strip())

    async def _match_passes(self, pipe: Pipe, retries: int) -> bool:
        """v6 double-match: brain (draft_a) vs an independent vision pass (draft_b), compared on
        FACTS (or by a model judge, per the agreement toggle). Retry up to `retries`; if they
        never agree, FAIL with both shown. If no second model is available, degrade to single."""
        vmodel = self._vision_model()
        if vmodel is None:
            return True                             # can't get a second opinion -> keep single
        mode = getattr(self.settings.autonomy, "agreement", "facts")
        detail: dict = {}
        for i in range(retries):
            draft_b = await self._second_pass(pipe, vmodel)
            pipe.draft_b = draft_b
            if not draft_b.ok:
                await self._say(pipe, "Double verification: no usable second opinion — using single.")
                return True                         # second pass unusable -> degrade to single
            if mode == "model":
                agree, detail = await self._agree_model(pipe.draft_a.text, draft_b.text)
            else:
                agree, detail = facts_agree(pipe.draft_a.text, draft_b.text)
            if agree:
                await self._say(pipe, f"Double verification: passed ({mode}).")
                return True
            await self._say(pipe, f"Double verification: mismatch (attempt {i + 1}/{retries}).")
        pipe.fail("double-verify", f"the two passes disagree after {retries} tries — {detail}\n\n"
                  f"[A] {pipe.draft_a.text}\n\n[B] {pipe.draft_b.text if pipe.draft_b else ''}")
        return False

    async def _second_pass(self, pipe: Pipe, vmodel) -> Result:
        """The independent second opinion (the vision model), over the same evidence."""
        if pipe.level == "direct":
            return await self._answer_direct(vmodel, pipe, source="vision")
        inputs = {k: v for k, v in pipe.results.items() if v.ok and k != "compose"}
        return await self._compose(vmodel, pipe, inputs, source="vision")

    async def _agree_model(self, a: str, b: str) -> tuple[bool, dict]:
        """Model-judge agreement (agreement='model'): asks whether the two answers say the same
        thing. It only JUDGES — it cannot rewrite either answer (not reconciliation)."""
        sysp = ('Do these two answers to the same question say the SAME thing — agree on the '
                'facts/conclusion, ignoring wording? Respond ONLY JSON: {"agree": true|false, '
                '"why": "<short>"}. /no_think')
        data = await self._json(self._pick_default_model(), sysp, f"Answer A: {a}\nAnswer B: {b}",
                                max_tokens=120)
        return bool(data.get("agree")), {"mode": "model", "why": str(data.get("why") or "")}

    # ===================== v6 final-answer validity gate =====================
    async def _finalize_valid(self, pipe: Pipe, model, retries: int) -> bool:
        """Runs on EVERY request. A model (sub-request) judges whether the draft actually answers
        the request; a deterministic grounding check guards against fabricated paths first. If
        invalid, re-draft with the critique and re-check — up to `retries` — else FAIL. Never
        returns an unchecked answer (the 'never returns early' gate)."""
        why = ""
        for attempt in range(retries):
            draft = (pipe.draft_a.text if pipe.draft_a else "").strip()
            evidence = "\n".join(json.dumps(r.data, default=str) for r in pipe.results.values()
                                 if r.ok and r.data)
            bad = ungrounded_facts(draft, evidence) if evidence else []
            if not draft:
                why = "no answer was produced"
            elif bad:
                why = "cited paths/values not in the tool results: " + "; ".join(bad[:5])
            elif pipe.objective:
                # Single correct answer -> strict correctness judge.
                ok, why = await self._validity_check(model, pipe, draft, evidence)
                if ok:
                    return True
            elif pipe.grounded and evidence:
                # Open-ended WORDING but must reflect the tool evidence (e.g. a screen summary):
                # judge faithfulness to the evidence, not a single correct answer. Catches junk
                # that a lenient check would pass.
                ok, why = await self._grounded_check(model, pipe, draft, evidence)
                if ok:
                    return True
            else:
                # Purely open-ended (e.g. "write a random paragraph"): accept any substantive,
                # non-refusing answer.
                if _DENIAL.search(draft):
                    why = "the answer refuses or dodges the request"
                else:
                    return True
            draft_preview = self._draft_preview(draft)
            retrying = attempt < retries - 1
            action = f"redrafting ({attempt + 1}/{retries})" if retrying else f"failing after {retries} tries"
            await self.events.emit(
                pipe.run_id,
                THOUGHT,
                {
                    "content": (
                        f"Answer not valid yet ({why}); {action}.\n"
                        f"Draft attempted: {draft_preview}"
                    ),
                    "why": why,
                    "draft": draft_preview,
                    "attempt": attempt + 1,
                    "retries": retries,
                    "will_retry": retrying,
                },
            )
            if retrying:
                await self._redraft(pipe, model, why)
        pipe.fail("validate", f"answer failed validation after {retries} tries: {why}")
        return False

    async def _validity_check(self, model, pipe: Pipe, draft: str, evidence: str) -> tuple[bool, str]:
        sysp = ('Judge whether the draft answer correctly and completely answers the user request, '
                'using the tool evidence. Respond ONLY JSON: {"valid": true|false, "why": "<short>"}. '
                'Invalid if it dodges the question, is incomplete, contradicts the evidence, or says '
                'it cannot do something it was actually given the means to do. /no_think')
        user = f"Request: {pipe.normalized}\nEvidence:\n{evidence or '(none)'}\nDraft answer: {draft}"
        data = await self._json(model, sysp, user, max_tokens=160)
        return bool(data.get("valid")), str(data.get("why") or "not valid")

    async def _grounded_check(self, model, pipe: Pipe, draft: str, evidence: str) -> tuple[bool, str]:
        """For open-ended-but-grounded answers (summaries/descriptions): is the draft a real,
        faithful answer SUPPORTED by the evidence — not meta-text, not invented, not a dodge?
        Wording is free; faithfulness is required."""
        sysp = ('Judge whether the draft is a genuine answer to the request that is faithfully '
                'supported by the evidence. Respond ONLY JSON: {"valid": true|false, "why": "<short>"}. '
                'Invalid if it is empty, refuses, restates the instructions instead of answering, '
                'or states things not supported by the evidence. Wording may differ freely. /no_think')
        user = f"Request: {pipe.normalized}\nEvidence:\n{evidence or '(none)'}\nDraft answer: {draft}"
        data = await self._json(model, sysp, user, max_tokens=160)
        return bool(data.get("valid")), str(data.get("why") or "not faithful to the evidence")

    async def _redraft(self, pipe: Pipe, model, critique: str) -> None:
        """Re-produce the draft with the validity critique fed back in."""
        if pipe.level == "direct":
            r = await self._answer_direct(model, pipe, source="brain", critique=critique)
        else:
            inputs = {k: v for k, v in pipe.results.items() if v.ok and k != "compose"}
            r = await self._compose(model, pipe, inputs, source="brain", critique=critique)
        if r.ok:
            pipe.draft_a = r

    @staticmethod
    def _draft_preview(text: str, limit: int = 900) -> str:
        compact = " ".join((text or "").split())
        if not compact:
            return "<empty>"
        return compact if len(compact) <= limit else compact[: limit - 3] + "..."

    async def _answer_direct(self, model, pipe: Pipe, source: str, critique: str = "") -> Result:
        crit = f" A previous attempt was judged insufficient: {critique}. Fix that." if critique else ""
        sysp = ("Answer the user's request directly and concisely, in plain text." + crit
                + " /no_think")
        try:
            resp = await asyncio.to_thread(lambda: model.chat(
                [Message(role="system", content=sysp), Message(role="user", content=self._user(pipe))],
                None, temperature=0.2, max_tokens=500))
            txt = strip_reasoning((resp.content or "").strip())
            if not txt:
                return Result(ok=False, error="returned only reasoning")
            return Result(ok=True, kind="prose", text=txt, source=source)
        except Exception as e:
            return Result(ok=False, error=str(e))

    # ===================== terminal =====================
    async def _respond(self, pipe: Pipe, ctx) -> RunResult:
        if pipe.status not in ("failed", "refused"):
            pipe.status = "done"
        if self.store:
            self.store.update_run(pipe.run_id, status="completed", ended_at=time.time(),
                                  summary=pipe.answer[:2000])
            if pipe.thread_id:
                try:
                    self.store.add_message(pipe.thread_id, pipe.run_id, "user", pipe.request)
                    self.store.add_message(pipe.thread_id, pipe.run_id, "assistant", pipe.answer)
                except Exception:
                    logger.warning("persist turn failed", exc_info=True)
        await self.events.emit(pipe.run_id, RUN_COMPLETED,
                               {"final": pipe.answer, "steps": len(pipe.results), "artifacts": pipe.artifacts})
        return RunResult(run_id=pipe.run_id, status="completed", final_answer=pipe.answer,
                         steps=len(pipe.results), artifacts=pipe.artifacts)

    async def _fail(self, pipe: Pipe, why: str) -> RunResult:
        # Decision #1: deliver the failure as the answer, with where + why.
        pipe.status = "failed"
        pipe.answer = pipe.answer or f"I couldn't complete that — {why}"
        if self.store:
            self.store.update_run(pipe.run_id, status="failed", ended_at=time.time(),
                                  error_code="pipeline_fail", error_message=why)
        await self.events.emit(pipe.run_id, RUN_FAILED, {"code": "pipeline_fail", "message": why,
                                                         "steps": len(pipe.results)})
        await self.events.emit(pipe.run_id, RUN_COMPLETED,
                               {"final": pipe.answer, "steps": len(pipe.results), "artifacts": pipe.artifacts})
        return RunResult(run_id=pipe.run_id, status="failed", final_answer=pipe.answer,
                         steps=len(pipe.results), error=why)

    async def _cancel(self, pipe: Pipe) -> RunResult:
        if self.store:
            self.store.update_run(pipe.run_id, status="cancelled", ended_at=time.time())
        await self.events.emit(pipe.run_id, RUN_CANCELLED, {"steps": len(pipe.results)})
        return RunResult(run_id=pipe.run_id, status="cancelled", steps=len(pipe.results))

    # ===================== suspend/resume question channel =====================
    def answer_question(self, question_id: str, text: str | None) -> bool:
        """Called by the API when the user answers a Multiresponse question (text=None cancels)."""
        fut = self._questions.get(question_id)
        if fut is None or fut.done():
            return False
        fut.get_loop().call_soon_threadsafe(fut.set_result, text)
        return True

    # ===================== shared helpers =====================
    async def _say(self, pipe: Pipe, text: str) -> None:
        """Emit a verbose step-by-step status line to the timeline (model.thought)."""
        await self.events.emit(pipe.run_id, THOUGHT, {"content": text})

    def _user(self, pipe: Pipe) -> str:
        return ((pipe.history_context + "\n\n") if pipe.history_context else "") + pipe.normalized

    def _pick_model(self, req):
        model = self.model
        if self.resolve_model is not None and getattr(req, "model_id", None):
            try:
                model = self.resolve_model(req.model_id)
            except Exception:
                model = self.model
        return model

    def _pick_default_model(self):
        return self.model

    def _vision_model(self):
        if self.resolve_model is None:
            return None
        try:
            from jarvis.tools import vision as _vis
            base, key = _vis.lm_endpoint(self.settings)
            vm = _vis.resolve_vision_model(self.settings, base, key)
            return self.resolve_model(vm) if vm else None
        except Exception:
            return None

    async def _json(self, model, system: str, user: str, max_tokens: int = 500) -> dict:
        try:
            resp = await asyncio.to_thread(lambda: model.chat(
                [Message(role="system", content=system), Message(role="user", content=user)],
                None, temperature=0, max_tokens=max_tokens))
            return _extract_json(resp.content or "")
        except Exception:
            return {}

    async def _bind_args(self, model, task: Task, spec, pipe: Pipe) -> dict:
        schema = spec.args_model.model_json_schema()
        ex = "\n".join(f"  {e.get('request','')} -> {json.dumps(e.get('args', {}))}"
                       for e in (spec.examples or [])[:3])
        prior = "\n".join(f"{k}: {json.dumps(v.data, default=str)[:300]}"
                          for k, v in pipe.inputs_for(task).items())
        sysp = (f"Produce arguments for `{task.tool}` to: \"{task.intent}\". ONLY a JSON object "
                f"matching: {json.dumps(schema.get('properties', {}))}.\n"
                + (f"Examples:\n{ex}\n" if ex else "")
                + (f"Prior results:\n{prior}\n" if prior else "")
                + f"Request: {pipe.normalized} /no_think")
        data = await self._json(model, sysp, "", max_tokens=300)
        if not isinstance(data, dict):
            data = {}
        try:
            spec.args_model(**data)
        except Exception:
            pass
        return data

    async def _repair_args(self, model, task: Task, spec, pipe: Pipe, *,
                           previous_args: dict, previous_error: str,
                           attempt: int, max_attempts: int) -> tuple[dict, str]:
        schema = spec.args_model.model_json_schema()
        ex = "\n".join(f"  {e.get('request','')} -> {json.dumps(e.get('args', {}))}"
                       for e in (spec.examples or [])[:3])
        prior = "\n".join(f"{k}: {json.dumps(v.data, default=str)[:1200]}"
                          for k, v in pipe.inputs_for(task).items())
        sysp = (
            f"Repair the arguments for `{task.tool}`. The previous attempt failed. "
            'Respond ONLY JSON: {"why":"<short cause>","arguments":{...corrected args...}}.\n'
            f"Tool schema properties: {json.dumps(schema.get('properties', {}), default=str)}\n"
            f"Required fields: {json.dumps(schema.get('required', []), default=str)}\n"
            "Use the prior tool results to fill missing fields. If focusing a window, infer a stable "
            "substring from the visible window titles. Do not leave required fields blank.\n"
            + (f"Examples:\n{ex}\n" if ex else "")
            + f"Repair attempt: {attempt}/{max_attempts} /no_think"
        )
        user = (
            f"User request: {pipe.normalized}\n"
            f"Task intent: {task.intent}\n"
            f"Previous arguments: {json.dumps(previous_args, default=str)}\n"
            f"Tool error: {previous_error}\n"
            + (f"Prior successful results:\n{prior}\n" if prior else "")
        )
        data = await self._json(model, sysp, user, max_tokens=500)
        why = str(data.get("why") or previous_error or "arguments did not match the tool schema").strip()
        candidate = data.get("arguments") if isinstance(data.get("arguments"), dict) else data
        if not isinstance(candidate, dict):
            candidate = {}
        try:
            spec.args_model(**candidate)
        except Exception as e:
            why = f"{why}; repaired args still failed validation: {e}"
        return candidate, why

    @staticmethod
    def _repairable_tool_error(task: Task, err_obj) -> bool:
        if err_obj is None:
            return False
        return bool(
            getattr(err_obj, "retryable", False)
            or getattr(err_obj, "category", "") == "validation"
            or getattr(err_obj, "code", "") in {"bad_args", "invalid_tool_arguments"}
        )

    @staticmethod
    def _deterministic_repair_args(task: Task, pipe: Pipe, previous_args: dict, previous_error: str) -> tuple[dict | None, str]:
        return None, ""

    @staticmethod
    def _json_preview(value, limit: int = 700) -> str:
        text = json.dumps(value, default=str, ensure_ascii=True)
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def _resolve_tool(self, name: str) -> str | None:
        valid = set(self.registry.names()) - _META   # meta-tools are never plannable leaves
        n = (name or "").strip().strip("`\"'")
        if n in valid:
            return n
        if "." not in n and "_" in n and n.replace("_", ".", 1) in valid:
            return n.replace("_", ".", 1)
        low = n.lower().replace("_", ".")
        for v in valid:
            if v.lower() == low:
                return v
        leaf = low.split(".")[-1]
        matches = [v for v in valid if v.split(".")[-1] == leaf]
        return matches[0] if len(matches) == 1 else None

    # -- confirm gate (active unless full-permissions) -- #
    def _needs_confirm(self, spec) -> bool:
        level = getattr(self.settings.autonomy, "confirm_level", "risky")
        if level == "never":
            return False
        c = getattr(spec, "confirm", "auto")
        if c == "always":
            return True
        if c == "never":
            return False
        if level == "every":
            return True
        return spec.risk in ("destructive", "system")

    async def _confirm(self, call: ToolCall, spec, run_id: str) -> bool:
        if spec is None or self.confirm is None or not self._needs_confirm(spec):
            return True
        await self.events.emit(run_id, ACTION_CONFIRM_REQUESTED, {
            "call_id": call.id, "tool_name": call.name, "risk": spec.risk,
            "arguments": redact_obj(call.arguments),
            "timeout": getattr(self.settings.autonomy, "confirm_timeout_s", 60)})
        approved = await self.confirm.request(call.id, getattr(self.settings.autonomy, "confirm_timeout_s", 60))
        await self.events.emit(run_id, ACTION_CONFIRM_RESOLVED, {"call_id": call.id, "approved": approved})
        return bool(approved)

    # -- cancel / panic -- #
    def request_cancel(self, run_id: str) -> None:
        self._cancelled.add(run_id)
        for qid, fut in list(self._questions.items()):       # unblock any parked question
            if not fut.done():
                fut.get_loop().call_soon_threadsafe(fut.set_result, None)
        ctx = self._contexts.get(run_id)
        if ctx:
            for p in list(getattr(ctx, "processes", [])):
                try:
                    p.kill()
                except Exception:
                    pass

    def kill_all_processes(self) -> None:
        for ctx in list(self._contexts.values()):
            for p in list(getattr(ctx, "processes", [])):
                try:
                    p.kill()
                except Exception:
                    pass
