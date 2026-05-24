import { useState } from "react";

/**
 * Approval dialog for DANGEROUS tool calls.
 *
 * The decision is persisted by the core's encrypted vault (sera/safety/vault.py):
 * "Always allow this shape" → the identical (tool, args) auto-approves next
 * time; a plain Deny arms a 24h cooldown. Nothing here is stored client-side —
 * the shell only renders the request and POSTs the verdict back to the core.
 */
export interface ApprovalRequest {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  reason?: string;
}

interface Props {
  request: ApprovalRequest;
  onRespond: (id: string, decision: boolean, remember: boolean) => void;
}

export default function Approvals({ request, onRespond }: Props) {
  const [remember, setRemember] = useState(false);

  return (
    <div
      style={{
        border: "1px solid #c00",
        borderRadius: 8,
        padding: 16,
        margin: 12,
        background: "#fff6f6",
        fontFamily: "system-ui",
      }}
    >
      <div style={{ fontWeight: 600, color: "#c00" }}>Approval required</div>
      <div style={{ marginTop: 8 }}>
        <code>{request.tool}</code>
        <pre style={{ fontSize: 12, overflow: "auto", maxHeight: 160 }}>
          {JSON.stringify(request.args, null, 2)}
        </pre>
      </div>
      {request.reason && <div style={{ fontSize: 12, opacity: 0.7 }}>{request.reason}</div>}

      <label style={{ display: "flex", gap: 6, fontSize: 13, marginTop: 8 }}>
        <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
        Always allow this exact shape (stored encrypted)
      </label>

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button
          onClick={() => onRespond(request.id, true, remember)}
          style={{ flex: 1, background: "#0a0", color: "#fff", padding: 8, border: "none", borderRadius: 4 }}
        >
          Allow
        </button>
        <button
          onClick={() => onRespond(request.id, false, false)}
          style={{ flex: 1, background: "#c00", color: "#fff", padding: 8, border: "none", borderRadius: 4 }}
        >
          Deny (24h cooldown)
        </button>
      </div>
    </div>
  );
}
