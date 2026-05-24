import { useState } from "react";

const CORE_URL = "http://127.0.0.1:11111";

interface Provenance {
  chunk_id: number;
  source: string;
  summary: string;
  confidence: number;
}
interface Relation {
  kind: string;
  dst: string;
  dst_type: string | null;
  confidence: number;
  provenance: Provenance | null;
}
interface EntityCard {
  entity: { id: number; name: string; type: string; first_seen: number; last_seen: number };
  relations: Relation[];
}

/**
 * Memory Tree browser (P-65). Search an entity → see its typed relations, each
 * with a provenance breadcrumb: the source chunk that asserted it. The data
 * comes from sera.shell.viewmodels.entity_card.
 */
export default function MemoryTree({ bearer }: { bearer: string }) {
  const [name, setName] = useState("");
  const [card, setCard] = useState<EntityCard | null>(null);
  const [miss, setMiss] = useState(false);

  async function search() {
    setMiss(false);
    const res = await fetch(`${CORE_URL}/v1/memory/entity?name=${encodeURIComponent(name)}`, {
      headers: { Authorization: `Bearer ${bearer}` },
    });
    if (res.status === 404) {
      setCard(null);
      setMiss(true);
      return;
    }
    setCard(await res.json());
  }

  return (
    <div style={{ fontFamily: "system-ui", padding: 16 }}>
      <div style={{ display: "flex", gap: 8 }}>
        <input value={name} onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && search()}
               placeholder="search an entity (e.g. Alice)" style={{ flex: 1, padding: 8 }} />
        <button onClick={search}>search</button>
      </div>
      {miss && <div style={{ opacity: 0.6, marginTop: 12 }}>No such entity.</div>}
      {card && (
        <div style={{ marginTop: 12 }}>
          <h3>{card.entity.name} <span style={{ opacity: 0.5, fontSize: 13 }}>{card.entity.type}</span></h3>
          {card.relations.map((r, i) => (
            <div key={i} style={{ borderLeft: "2px solid #ccc", paddingLeft: 10, marginBottom: 10 }}>
              <div><b>{r.kind}</b> → {r.dst} <span style={{ opacity: 0.5 }}>({(r.confidence * 100) | 0}%)</span></div>
              {r.provenance && (
                <div style={{ fontSize: 12, opacity: 0.7 }}>
                  ↳ from <code>{r.provenance.source}</code>: {r.provenance.summary}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
