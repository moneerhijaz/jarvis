// Thin client for the JARVIS backend. Uses relative /api paths (Vite proxies in
// dev; same-origin in prod). SSE is parsed manually because the backend emits
// custom event names (tool.started, etc.) that EventSource won't surface generically.

export type RunEvent = {
  id: string;
  run_id: string;
  seq: number;
  type: string;
  timestamp: string;
  payload: any;
};

export async function getHealth(): Promise<any> {
  const r = await fetch("/api/health");
  if (!r.ok) throw new Error("health failed");
  return r.json();
}

export async function createRun(message: string, opts: { working_directory?: string; project?: string } = {}) {
  const r = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, ...opts }),
  });
  if (!r.ok) throw new Error(`createRun failed: ${r.status}`);
  return r.json() as Promise<{ run_id: string; thread_id: string; events_url: string }>;
}

export async function cancelRun(runId: string) {
  await fetch(`/api/runs/${runId}/cancel`, { method: "POST" });
}

export async function getModels(): Promise<any> {
  const r = await fetch("/api/models");
  return r.json();
}

export async function selectModel(model: string) {
  await fetch("/api/models/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
}

// Stream a run's events. Calls onEvent for each event until a terminal one.
export async function streamRun(
  runId: string,
  onEvent: (ev: RunEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const resp = await fetch(`/api/runs/${runId}/events`, { headers: { Accept: "text/event-stream" }, signal });
  if (!resp.body) throw new Error("no event stream");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  const terminal = new Set(["run.completed", "run.failed", "run.cancelled"]);

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      try {
        const ev = JSON.parse(dataLine.slice(5).trim()) as RunEvent;
        onEvent(ev);
        if (terminal.has(ev.type)) return;
      } catch {
        /* ignore malformed frame */
      }
    }
  }
}

export async function transcribe(audio: Blob): Promise<{ text: string; language?: string }> {
  const form = new FormData();
  form.append("audio", audio, audio.type === "audio/wav" ? "speech.wav" : "speech.webm");
  const r = await fetch("/api/voice/transcribe", { method: "POST", body: form });
  if (!r.ok) throw new Error(`transcribe failed: ${r.status} ${await r.text()}`);
  return r.json();
}

// Ask the router whether a quick spoken filler is warranted; returns the phrase
// to speak, or null to stay quiet (e.g. for greetings / instant answers).
export async function ackDecision(message: string): Promise<string | null> {
  try {
    const r = await fetch("/api/ack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!r.ok) return null;
    const d = await r.json();
    return d.ack ? (d.phrase as string) : null;
  } catch {
    return null;
  }
}

export async function speak(text: string): Promise<Blob | null> {
  const r = await fetch("/api/voice/speak", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) return null; // TTS not configured -> caller can fall back to text only
  return r.blob();
}
