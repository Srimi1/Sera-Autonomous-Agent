import { useRef, useState } from "react";

const CORE_URL = "http://127.0.0.1:11111";

interface ToolEvent {
  name: string;
  phase: "start" | "end";
}

/** Parse a text/event-stream chunk buffer into complete SSE frames. */
function* parseFrames(buffer: string): Generator<{ event: string; data: string }> {
  const parts = buffer.split("\n\n");
  for (const part of parts) {
    if (!part.trim()) continue;
    let event = "message";
    let data = "";
    for (const line of part.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    yield { event, data };
  }
}

export default function Chat() {
  const [input, setInput] = useState("");
  const [reply, setReply] = useState("");
  const [tools, setTools] = useState<ToolEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const bearer = useRef<string>(import.meta.env.VITE_SERA_API_KEY ?? "");

  async function send() {
    if (!input.trim() || busy) return;
    setBusy(true);
    setReply("");
    setTools([]);
    const text = input;
    setInput("");

    try {
      const res = await fetch(`${CORE_URL}/v1/turn/stream`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${bearer.current}`,
        },
        body: JSON.stringify({ text, user_id: "shell", channel_id: "shell", platform: "desktop" }),
      });
      if (!res.body) throw new Error("no stream body");

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      // Glass-box: render tokens AND the live tool trace as they arrive.
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lastBreak = buf.lastIndexOf("\n\n");
        if (lastBreak === -1) continue;
        const ready = buf.slice(0, lastBreak + 2);
        buf = buf.slice(lastBreak + 2);
        for (const { event, data } of parseFrames(ready)) {
          const parsed = data ? JSON.parse(data) : {};
          if (event === "token") setReply((r) => r + parsed.text);
          else if (event === "tool_start") setTools((t) => [...t, { name: parsed.name, phase: "start" }]);
          else if (event === "tool_end") setTools((t) => [...t, { name: parsed.name, phase: "end" }]);
          else if (event === "error") setReply((r) => r + `\n[error: ${parsed.error}]`);
        }
      }
    } catch (e) {
      setReply((r) => r + `\n[shell error: ${String(e)}]`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ fontFamily: "system-ui", padding: 16 }}>
      <div style={{ minHeight: 160, whiteSpace: "pre-wrap" }}>{reply}</div>
      {tools.length > 0 && (
        <div style={{ fontSize: 12, opacity: 0.6, marginTop: 8 }}>
          {tools.map((t, i) => (
            <div key={i}>
              {t.phase === "start" ? "→" : "←"} {t.name}
            </div>
          ))}
        </div>
      )}
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="talk to Sera"
          style={{ flex: 1, padding: 8 }}
        />
        <button onClick={send} disabled={busy}>
          {busy ? "…" : "send"}
        </button>
      </div>
    </div>
  );
}
