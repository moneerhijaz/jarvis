import { useEffect, useRef, useState } from "react";
import Hologram, { CoreState } from "./Hologram";
import { Recorder, toWav16k, SpeechController } from "./voice";
import * as api from "./api";

type TimelineItem = { id: string; kind: string; text: string; detail?: string };

export default function App() {
  const [state, setState] = useState<CoreState>("idle");
  const [level, setLevel] = useState(0);
  const [textMode, setTextMode] = useState(false); // voice is default
  const [textInput, setTextInput] = useState("");
  const [caption, setCaption] = useState("Tap the core and speak");
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [health, setHealth] = useState<any>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [brain, setBrain] = useState<string>("");
  const [showChat, setShowChat] = useState(false);

  const recorderRef = useRef<Recorder | null>(null);
  const levelRaf = useRef<number>(0);
  const speechRef = useRef<SpeechController | null>(null);

  useEffect(() => {
    api.getHealth().then((h) => { setHealth(h); setBrain(h?.model?.default_model ?? ""); }).catch(() => setHealth(null));
    const sc = new SpeechController(api.speak, setLevel, setCaption);  // caption = tape window
    sc.setOnIdle(() => setState("idle"));
    speechRef.current = sc;
    return () => sc.cancel();
  }, []);

  const push = (item: TimelineItem) => setTimeline((t) => [...t, item].slice(-40));

  // ---- run a goal through the agent, streaming events into the timeline ----
  async function runGoal(goal: string) {
    if (!goal.trim()) return;
    speechRef.current?.cancel();   // stop any prior speech before a new run
    setTimeline([]);
    push({ id: "you", kind: "user", text: goal });
    setState("thinking");
    setCaption("Working…");
    playAck(goal);                 // router decides whether to speak a quick filler
    let finalText = "", streamed = false, sbuf = "", allText = "";
    try {
      const { run_id } = await api.createRun(goal);
      setRunId(run_id);
      await api.streamRun(run_id, (ev) => {
        const pl: any = ev.payload || {};
        switch (ev.type) {
          case "answer.delta": {
            streamed = true; sbuf += pl.text || ""; allText += pl.text || "";
            setCaption(allText);   // show the entire output as it arrives (no animation)
            let m: RegExpMatchArray | null;
            while ((m = sbuf.match(/^([\s\S]*?[.!?])(\s+|$)/))) {
              setState("speaking");
              speechRef.current?.enqueue(m[1]);
              sbuf = sbuf.slice(m[0].length);
            }
            break;
          }
          case "answer.cancel":
            streamed = false; sbuf = ""; speechRef.current?.cancel();
            break;
          case "model.thought":
            if (pl.content) push({ id: ev.id, kind: "thought", text: pl.content });
            break;
          case "plan.created":
            if (pl.selected_tools) push({ id: ev.id, kind: "plan", text: "tools: " + pl.selected_tools.join(", ") });
            break;
          case "tool.started":
            push({ id: ev.id, kind: "tool", text: `→ ${pl.tool_name}`, detail: JSON.stringify(pl.arguments ?? {}) });
            break;
          case "tool.completed":
            push({ id: ev.id, kind: "tool-ok", text: `✓ ${pl.tool_name}` });
            break;
          case "tool.failed":
            push({ id: ev.id, kind: "tool-err", text: `✗ ${pl.tool_name}: ${pl.error?.message ?? ""}` });
            break;
          case "warning":
            push({ id: ev.id, kind: "warn", text: `⚠ ${pl.where}: ${pl.error}` });
            break;
          case "run.completed": finalText = pl.final ?? "Done."; break;
          case "run.failed": finalText = `Failed: ${pl.message ?? pl.code}`; break;
          case "run.cancelled": finalText = "Cancelled."; break;
        }
      });
      setRunId(null);
    } catch (e: any) {
      setState("error");
      setCaption("Error: " + (e?.message ?? e));
      setTimeout(() => setState("idle"), 2500);
      return;
    }
    if (streamed) {
      if (sbuf.trim()) speechRef.current?.enqueue(sbuf);   // flush trailing partial sentence
    } else {
      for (const s of speechRef.current?.split(finalText) ?? [finalText]) speechRef.current?.enqueue(s);
    }
    push({ id: "final", kind: "final", text: finalText });
    setCaption(finalText);   // entire answer shown at once
    if (!speechRef.current?.speaking) setState("idle");
  }

  async function playAck(goal: string) {
    try {
      const phrase = await api.ackDecision(goal);   // router decides; null = stay quiet
      if (!phrase) return;
      push({ id: "ack" + Date.now(), kind: "say", text: phrase });
      setCaption(phrase);
      setState("speaking");
      speechRef.current?.enqueue(phrase);
    } catch {
      /* no TTS / no router -> skip the spoken ack */
    }
  }

  // ---- voice push-to-talk ----
  async function toggleListening() {
    if (state === "listening") {
      cancelAnimationFrame(levelRaf.current);
      setLevel(0);
      const blob = await recorderRef.current!.stop();
      setState("thinking");
      setCaption("Transcribing…");
      try {
        let wav = blob;
        try { wav = await toWav16k(blob); } catch { /* fall back to raw */ }
        const { text } = await api.transcribe(wav);
        if (text) await runGoal(text);
        else {
          push({ id: "dc" + Date.now(), kind: "say", text: "Didn't catch that." });
          setCaption("Didn't catch that.");
          speechRef.current?.enqueue("Didn't catch that.");
          setState("idle");
        }
      } catch (e: any) {
        setCaption("Voice unavailable: " + (e?.message ?? e));
        setState("error");
        setTimeout(() => setState("idle"), 2500);
      }
      return;
    }
    // start listening (barge-in: stop speech AND any generating run)
    speechRef.current?.cancel();
    if (runId) { api.cancelRun(runId); setRunId(null); }
    try {
      const rec = new Recorder();
      await rec.start();
      recorderRef.current = rec;
      setState("listening");
      setCaption("Listening…");
      const tick = () => {
        setLevel(rec.level);
        levelRaf.current = requestAnimationFrame(tick);
      };
      tick();
    } catch {
      setCaption("Mic access denied.");
    }
  }

  function onCoreClick() {
    if (!textMode) toggleListening();   // tap to talk OR interrupt
  }

  async function stopRun() {
    if (runId) await api.cancelRun(runId);
  }

  const model = health?.model?.default_model ?? "—";
  const reachable = health?.model?.reachable;
  const voiceTts = health?.voice?.tts?.ready;

  return (
    <div className="app">
      <div className="topbar">
        <div className="brand">JARVIS</div>
        <div className="status">
          <span className={reachable ? "dot ok" : "dot bad"} />
          {reachable ? (
            <select
              className="ghost"
              title="Brain model"
              value={brain}
              onChange={(e) => { setBrain(e.target.value); api.selectModel(e.target.value); }}
            >
              <option value="auto">⚡ Auto (by task)</option>
              {(health?.model?.models ?? [])
                .filter((id: string) => !/embed/i.test(id))
                .map((id: string) => <option key={id} value={id}>{id}</option>)}
            </select>
          ) : (
            <span>LM Studio offline</span>
          )}
          <button className="ghost" onClick={() => setShowChat((s) => !s)}>💬 chat</button>
          <button className="ghost" onClick={() => setTextMode((m) => !m)}>
            {textMode ? "🎙 voice" : "⌨ text"}
          </button>
        </div>
      </div>

      <div className={"stage" + (showChat ? "" : " nochat")}>
        <div className={`core core-${state}`} onClick={onCoreClick} title="Tap to talk">
          <Hologram state={state} level={level} />
          <div className="caption">{caption}</div>
        </div>

        <aside className="timeline">
          {timeline.map((it) => (
            <div key={it.id + Math.random()} className={`tl tl-${it.kind}`}>
              <div className="tl-text">{it.text}</div>
              {it.detail && <div className="tl-detail">{it.detail}</div>}
            </div>
          ))}
        </aside>
      </div>

      {textMode && (
        <div className="dock">
          {state === "thinking" || runId ? (
            <button className="stop" onClick={stopRun}>◼ Stop</button>
          ) : (
            <form
              className="textbar"
              onSubmit={(e) => {
                e.preventDefault();
                const v = textInput;
                setTextInput("");
                runGoal(v);
              }}
            >
              <input
                autoFocus
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
                placeholder="Type a command for JARVIS…"
              />
              <button type="submit">Send</button>
            </form>
          )}
        </div>
      )}
    </div>
  );
}
