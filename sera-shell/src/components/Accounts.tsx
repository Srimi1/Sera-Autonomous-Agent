import { useEffect, useState } from "react";

const CORE_URL = "http://127.0.0.1:11111";

interface Account {
  app: string;
  connected: boolean;
  tool_count: number;
}

/**
 * Accounts panel (P-66). Lists connected Composio integrations and their tool
 * counts (from sera.shell.viewmodels.accounts_overview). "Connect" opens the
 * Composio OAuth URL in the system browser; the new integration appears here
 * after the round-trip.
 */
export default function Accounts({ bearer }: { bearer: string }) {
  const [accounts, setAccounts] = useState<Account[]>([]);

  async function refresh() {
    const res = await fetch(`${CORE_URL}/v1/accounts`, {
      headers: { Authorization: `Bearer ${bearer}` },
    });
    const data = await res.json();
    setAccounts(data.accounts ?? []);
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div style={{ fontFamily: "system-ui", padding: 16 }}>
      <h3>Accounts</h3>
      {accounts.length === 0 && <div style={{ opacity: 0.6 }}>No integrations connected yet.</div>}
      {accounts.map((a) => (
        <div key={a.app} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0" }}>
          <span>{a.app}</span>
          <span style={{ opacity: 0.6 }}>{a.tool_count} tools</span>
        </div>
      ))}
    </div>
  );
}
