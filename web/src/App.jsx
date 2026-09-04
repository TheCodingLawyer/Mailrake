import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, formatBytes, formatDate, subscribe } from "./api";

export default function App() {
  const [tab, setTab] = useState("unsubscribe");
  const [session, setSession] = useState(null);
  const [senders, setSenders] = useState([]);
  const [storage, setStorage] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [focus, setFocus] = useState(0);
  const [progress, setProgress] = useState(null);
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState([]);
  const [dryRun, setDryRun] = useState(true);
  const [trash, setTrash] = useState(true);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const [s, sn] = await Promise.all([api.session(), api.senders()]);
      setSession(s);
      setSenders(sn.senders);
      setStorage(await api.storage());
    } catch (e) {
      setError(e.message);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  useEffect(() => subscribe((event) => {
    if (event.type === "progress") {
      setProgress(event.phase === "idle" ? null : event);
    } else if (event.type === "done") {
      setProgress(null);
      setLog((l) => [{
        ok: true,
        text: `Scan complete — ${event.fetched} messages, ${event.senders} senders`
          + (event.dropped ? `, ${event.dropped} unreadable` : ""),
      }, ...l]);
      refresh();
    } else if (event.type === "error") {
      setProgress(null);
      setError(event.message);
    }
  }), [refresh]);

  const startScan = async (fresh = false) => {
    setError(null);
    try {
      await api.scan({ fresh });
      setProgress({ phase: "listing", done: 0, total: 0 });
    } catch (e) { setError(e.message); }
  };

  const act = async (fn, emails, extra = {}) => {
    if (!emails.length) return;
    setBusy(true);
    setError(null);
    try {
      const { results } = await fn({ emails, trash, dry_run: dryRun, ...extra });
      setLog((l) => [...results.map((r) => ({
        ok: r.ok,
        text: `${r.email} — ${r.skipped === "sensitive"
          ? `skipped (sensitive: ${r.detail})`
          : r.detail || (r.ok ? "done" : "failed")}`
          + (r.trashed ? ` · trashed ${r.trashed}` : ""),
      })), ...l]);
      setSelected(new Set());
      await refresh();
    } catch (e) { setError(e.message); }
    setBusy(false);
  };

  const visible = useMemo(
    () => senders.filter((s) => s.can_unsubscribe),
    [senders],
  );

  const toggle = useCallback((email) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(email) ? next.delete(email) : next.add(email);
      return next;
    });
  }, []);

  // Keyboard model deliberately mirrors the CLI: j/k to move, space to
  // select, u to unsubscribe. Anyone who used the terminal tool already
  // knows how to drive this.
  useEffect(() => {
    const onKey = (e) => {
      if (e.target.tagName === "INPUT" || tab !== "unsubscribe") return;
      const max = visible.length - 1;
      if (e.key === "j" || e.key === "ArrowDown") { setFocus((f) => Math.min(max, f + 1)); e.preventDefault(); }
      else if (e.key === "k" || e.key === "ArrowUp") { setFocus((f) => Math.max(0, f - 1)); e.preventDefault(); }
      else if (e.key === " ") { if (visible[focus]) toggle(visible[focus].email); e.preventDefault(); }
      else if (e.key === "a") { setSelected(new Set(visible.map((s) => s.email))); }
      else if (e.key === "Escape") { setSelected(new Set()); }
      else if (e.key === "u" && selected.size) {
        act(api.unsubscribe, [...selected]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [visible, focus, selected, tab, toggle, dryRun, trash]);

  const sensitiveSelected = [...selected].filter(
    (e) => senders.find((s) => s.email === e)?.sensitive);

  return (
    <div className="app">
      <div className="header">
        <h1>Gmail Control Panel</h1>
        <span className="badge">local only</span>
      </div>
      <p className="subtitle">
        Your mail never leaves this machine. Nothing is deleted permanently —
        everything goes to Trash, recoverable for 30 days.
      </p>

      {error && <div className="banner danger">{error}</div>}

      <div className="tabs" role="tablist">
        <button className="tab" role="tab" aria-selected={tab === "unsubscribe"}
                onClick={() => setTab("unsubscribe")}>
          Unsubscribe<span className="count">{visible.length}</span>
        </button>
        <button className="tab" role="tab" aria-selected={tab === "storage"}
                onClick={() => setTab("storage")}>
          Storage<span className="count">
            {storage ? formatBytes(storage.totals.bytes) : "—"}</span>
        </button>
        <button className="tab" role="tab" aria-selected={tab === "activity"}
                onClick={() => setTab("activity")}>
          Activity<span className="count">{log.length || ""}</span>
        </button>
      </div>

      {progress && <Progress progress={progress} />}

      {tab === "unsubscribe" && (
        <>
          <div className="toolbar">
            <button className="primary" onClick={() => startScan(false)}
                    disabled={!!progress}>
              {senders.length ? "Scan for new" : "Scan mailbox"}
            </button>
            <button onClick={() => startScan(true)} disabled={!!progress}>
              Full rescan
            </button>
            <span className="spacer" />
            <label className="check">
              <input type="checkbox" checked={dryRun}
                     onChange={(e) => setDryRun(e.target.checked)} />
              Preview only
            </label>
            <label className="check">
              <input type="checkbox" checked={trash}
                     onChange={(e) => setTrash(e.target.checked)} />
              Also trash existing
            </label>
            <button className="primary" disabled={!selected.size || busy}
                    onClick={() => act(api.unsubscribe, [...selected])}>
              Unsubscribe {selected.size || ""}
            </button>
          </div>

          {sensitiveSelected.length > 0 && (
            <div className="banner warn">
              {sensitiveSelected.length} of your selected senders look sensitive
              (banks, government, security alerts). They will be skipped unless you
              confirm each one deliberately.{" "}
              <button className="danger"
                      onClick={() => act(api.unsubscribe, sensitiveSelected,
                                         { force_sensitive: true })}>
                Unsubscribe them anyway
              </button>
            </div>
          )}

          {visible.length === 0 ? (
            <div className="empty">
              <h3>No senders scanned yet</h3>
              <p>Run a scan to find everyone who is emailing you in bulk.</p>
            </div>
          ) : (
            <>
              <div className="list">
                {visible.map((s, i) => (
                  <SenderRow key={s.email} sender={s} index={i}
                             focused={i === focus}
                             selected={selected.has(s.email)}
                             onClick={() => { setFocus(i); toggle(s.email); }} />
                ))}
              </div>
              <p className="hint" style={{ marginTop: 12 }}>
                <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>space</kbd> select ·{" "}
                <kbd>a</kbd> all · <kbd>u</kbd> unsubscribe · <kbd>esc</kbd> clear
              </p>
            </>
          )}
        </>
      )}

      {tab === "storage" && <Storage storage={storage} />}
      {tab === "activity" && <Activity log={log} session={session} />}
    </div>
  );
}

function Progress({ progress }) {
  const pct = progress.total ? (progress.done / progress.total) * 100 : 0;
  const label = progress.phase === "listing" ? "Finding messages" : "Reading metadata";
  return (
    <div className="progress">
      <div style={{ display: "flex", justifyContent: "space-between" }}>
        <span>{label}…</span>
        <span className="hint">{progress.done}{progress.total ? ` / ${progress.total}` : ""}</span>
      </div>
      <div className="track"><div className="fill" style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

function SenderRow({ sender, focused, selected, onClick }) {
  return (
    <div className="row" data-focused={focused} data-selected={selected} onClick={onClick}>
      <input type="checkbox" checked={selected} readOnly />
      <div>
        <span className="name">{sender.name || sender.email}</span>
        {sender.sensitive && <span className="pill sensitive"
          title={sender.sensitive_reasons.join(", ")}>sensitive</span>}
        {sender.trusted && <span className="pill trusted">trusted</span>}
        <div className="email">{sender.email}</div>
        {sender.sample_subjects[0] && (
          <div className="subject">{sender.sample_subjects[0]}</div>
        )}
      </div>
      <div className="meta">
        {sender.count} msgs<br />
        {formatBytes(sender.bytes)}<br />
        <span className="method">{sender.method}</span><br />
        <span className="method">{formatDate(sender.last_date)}</span>
      </div>
    </div>
  );
}

function Storage({ storage }) {
  if (!storage || !storage.totals.messages) {
    return (
      <div className="empty">
        <h3>Nothing scanned yet</h3>
        <p>Run a scan from the Unsubscribe tab to see where your storage went.</p>
      </div>
    );
  }
  const rows = storage.by_sender;
  const max = rows[0]?.bytes || 1;
  const reclaimable = rows.filter((r) => r.has_unsub)
    .reduce((sum, r) => sum + r.bytes, 0);

  return (
    <>
      <div className="stat-grid">
        <div className="stat">
          <div className="value">{formatBytes(storage.totals.bytes)}</div>
          <div className="label">scanned</div>
        </div>
        <div className="stat">
          <div className="value">{storage.totals.messages.toLocaleString()}</div>
          <div className="label">messages</div>
        </div>
        <div className="stat">
          <div className="value" style={{ color: "var(--ok)" }}>
            {formatBytes(reclaimable)}
          </div>
          <div className="label">from senders you can unsubscribe</div>
        </div>
      </div>

      <div className="list">
        {rows.slice(0, 40).map((r) => (
          <div className="bar-row" key={r.email}>
            <div>
              <div>
                {r.name || r.email}
                <span className="hint"> · {r.count} msgs</span>
              </div>
              <div className="bar-track">
                <div className={`bar-fill${r.has_unsub ? " unsub" : ""}`}
                     style={{ width: `${(r.bytes / max) * 100}%` }} />
              </div>
            </div>
            <div className="size">{formatBytes(r.bytes)}</div>
          </div>
        ))}
      </div>
      <p className="hint" style={{ marginTop: 12 }}>
        Green bars are senders with a working unsubscribe link.
      </p>
    </>
  );
}

function Activity({ log, session }) {
  if (!log.length) {
    return (
      <div className="empty">
        <h3>Nothing yet this session</h3>
        <p>
          Every action is also written to a permanent local ledger
          {session ? ` (${session.trusted} trusted senders, ${session.failures} pending failures)` : ""}.
        </p>
      </div>
    );
  }
  return (
    <div className="list log">
      {log.map((entry, i) => (
        <div className="row" key={i} style={{ gridTemplateColumns: "16px 1fr" }}>
          <span className={entry.ok ? "ok" : "fail"}>{entry.ok ? "✓" : "✗"}</span>
          <span>{entry.text}</span>
        </div>
      ))}
    </div>
  );
}
