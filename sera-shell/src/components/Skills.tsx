import { useEffect, useState } from "react";

const CORE_URL = "http://127.0.0.1:11111";

interface SkillRow {
  name: string;
  trigger: string;
  permission: string;
  version: string;
  council: boolean;
  state: string;
  enabled: boolean;
  score: number | null;
}

/**
 * Skill manager (P-67). Lists skills with lifecycle state + quality score
 * (from sera.shell.viewmodels.skills_overview). Toggling enable posts to the
 * core, which flips the skill's lifecycle state; the A/B harness (P-26) then
 * governs promotion.
 */
export default function Skills({ bearer }: { bearer: string }) {
  const [skills, setSkills] = useState<SkillRow[]>([]);

  async function refresh() {
    const res = await fetch(`${CORE_URL}/v1/skills`, {
      headers: { Authorization: `Bearer ${bearer}` },
    });
    const data = await res.json();
    setSkills(data.skills ?? []);
  }

  async function toggle(name: string, enabled: boolean) {
    await fetch(`${CORE_URL}/v1/skills/${encodeURIComponent(name)}`, {
      method: "PATCH",
      headers: { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    refresh();
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div style={{ fontFamily: "system-ui", padding: 16 }}>
      <h3>Skills</h3>
      {skills.map((s) => (
        <div key={s.name} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0" }}>
          <input type="checkbox" checked={s.enabled} onChange={(e) => toggle(s.name, e.target.checked)} />
          <span style={{ flex: 1 }}>
            {s.name} <span style={{ opacity: 0.5, fontSize: 12 }}>v{s.version} · {s.state}</span>
          </span>
          {s.score != null && <span style={{ opacity: 0.6, fontSize: 12 }}>{s.score.toFixed(2)}</span>}
        </div>
      ))}
    </div>
  );
}
