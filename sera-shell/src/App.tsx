import { useState } from "react";
import { invoke } from "@tauri-apps/api/tauri";

interface TurnResponse {
  ok: boolean;
  text: string;
  profile_used: string | null;
  latency_ms: number;
  error: string | null;
}

export default function App() {
  const [input, setInput] = useState("");
  const [reply, setReply] = useState<string>("");
  const [busy, setBusy] = useState(false);

  async function send() {
    if (!input.trim() || busy) return;
    setBusy(true);
    try {
      const res = await invoke<TurnResponse>("turn", { text: input });
      setReply(res.ok ? res.text : `error: ${res.error ?? "unknown"}`);
    } catch (e) {
      setReply(`shell error: ${String(e)}`);
    } finally {
      setBusy(false);
      setInput("");
    }
  }

  return (
    <main style={{ fontFamily: "system-ui", padding: 16 }}>
      <h1 style={{ fontSize: 18 }}>Sera</h1>
      <div style={{ minHeight: 120, whiteSpace: "pre-wrap" }}>{reply}</div>
      <div style={{ display: "flex", gap: 8 }}>
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
    </main>
  );
}
