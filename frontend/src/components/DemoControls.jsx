import { useState } from "react";
import { api } from "../api";

/* Only rendered on the replay feed. Steps the scripted timeline through a
   real ingest + detection pass at each point, so the spike-and-revert can
   be demonstrated on command instead of waiting an hour for it. Nothing is
   injected -- the detection layer reaches its own conclusions. */
export default function DemoControls({ onComplete, degraded }) {
  const [busy, setBusy] = useState(null);
  const [note, setNote] = useState(null);

  async function run(action, label, describe) {
    setBusy(label);
    setNote(null);
    try {
      const result = await action();
      setNote(describe(result));
      await onComplete();
    } catch (err) {
      setNote(err.message);
    } finally {
      setBusy(null);
    }
  }

  async function toggleChaos() {
    const next = !degraded;
    await run(
      async () => {
        const res = await api.setChaos(next);
        // Attempt an ingest so the outage (or the recovery) is immediately
        // visible, rather than waiting for the next cron tick.
        await api.runIngest().catch(() => {});
        return res;
      },
      "chaos",
      (res) =>
        res.chaos_enabled
          ? "Provider knocked out. Prices now age and are labelled stale — nothing is invented."
          : "Provider restored. Breaker closed and fresh prices are flowing again."
    );
  }

  return (
    <div className="demo-bar">
      <div className="demo-bar__label">
        <span className="demo-bar__tag">Demo</span>
        Replay the scripted market day through the live detection pipeline.
      </div>

      <div className="demo-bar__actions">
        <button
          type="button"
          className="demo-btn demo-btn--primary"
          disabled={!!busy}
          onClick={() =>
            run(api.runScenario, "run", (r) => {
              const events = r.steps.reduce((n, s) => n + s.events_touched, 0);
              return `Replayed ${r.steps.length} points · ${events} event updates`;
            })
          }
        >
          {busy === "run" ? "Replaying…" : "Run scenario"}
        </button>

        <button
          type="button"
          className={`demo-btn ${degraded ? "demo-btn--danger" : ""}`}
          disabled={!!busy}
          onClick={toggleChaos}
        >
          {busy === "chaos"
            ? "Switching…"
            : degraded
              ? "Restore provider"
              : "Kill provider"}
        </button>

        <button
          type="button"
          className="demo-btn"
          disabled={!!busy}
          onClick={() => run(api.resetDemo, "reset", () => "Cleared events and price history.")}
        >
          {busy === "reset" ? "Clearing…" : "Reset"}
        </button>
      </div>

      {note && <p className="demo-bar__note">{note}</p>}
    </div>
  );
}
