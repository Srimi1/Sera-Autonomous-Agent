import { useEffect, useState } from "react";

const CORE_URL = "http://127.0.0.1:11111";

type ConsentStatus = Record<string, boolean>;

/**
 * Settings panel (P-67 + P-70 consent toggles). Renders redacted config
 * (sera.shell.viewmodels.settings_overview — secrets never sent in the clear)
 * and the per-feature OS consent switches. Flipping a consent toggle posts to
 * the core, which stores it in the encrypted vault (P-70). Revoke = one click,
 * effective on the next OS-hook call.
 */
export default function Settings({ bearer }: { bearer: string }) {
  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [consent, setConsent] = useState<ConsentStatus>({});

  async function refresh() {
    const [c, k] = await Promise.all([
      fetch(`${CORE_URL}/v1/settings`, { headers: { Authorization: `Bearer ${bearer}` } }).then((r) => r.json()),
      fetch(`${CORE_URL}/v1/consent`, { headers: { Authorization: `Bearer ${bearer}` } }).then((r) => r.json()),
    ]);
    setConfig(c.config ?? {});
    setConsent(k.status ?? {});
  }

  async function setConsentFeature(feature: string, granted: boolean) {
    await fetch(`${CORE_URL}/v1/consent/${feature}`, {
      method: "PUT",
      headers: { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json" },
      body: JSON.stringify({ granted }),
    });
    refresh();
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div style={{ fontFamily: "system-ui", padding: 16 }}>
      <h3>Settings</h3>

      <h4 style={{ marginBottom: 4 }}>OS consent</h4>
      {Object.entries(consent).map(([feature, granted]) => (
        <label key={feature} style={{ display: "flex", gap: 8, padding: "4px 0" }}>
          <input type="checkbox" checked={granted} onChange={(e) => setConsentFeature(feature, e.target.checked)} />
          {feature}
        </label>
      ))}

      <h4 style={{ marginTop: 16, marginBottom: 4 }}>Configuration</h4>
      <pre style={{ fontSize: 12, background: "#f5f5f5", padding: 8, overflow: "auto" }}>
        {JSON.stringify(config, null, 2)}
      </pre>
    </div>
  );
}
