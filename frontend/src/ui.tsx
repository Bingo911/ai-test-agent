/** Small shared primitives: the console renders only what the server actually reported (§13.5). */

import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { AlertTriangle, Loader2, RefreshCw, Search } from "lucide-react";

import { ApiFailure } from "./api";

export type Async<T> = { data: T | null; error: string; loading: boolean };

/** Runs an async loader, ignores responses from a superseded call, and never throws into React. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[], initial: T | null = null): Async<T> & { reload: () => void } {
  const [state, setState] = useState<Async<T>>({ data: initial, error: "", loading: true });
  const run = useRef(0);

  const reload = useCallback(() => {
    const ticket = ++run.current;
    setState((prev) => ({ data: prev.data, error: "", loading: true }));
    loader()
      .then((data) => {
        if (ticket === run.current) setState({ data, error: "", loading: false });
      })
      .catch((error: unknown) => {
        if (ticket !== run.current) return;
        const failure = error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error);
        setState({ data: null, error: failure, loading: false });
      });
  }, deps);

  useEffect(() => {
    reload();
  }, [reload]);

  return { ...state, reload };
}

export function usePoll<T>(loader: () => Promise<T>, everyMs: number, active: boolean): Async<T> {
  const [state, setState] = useState<Async<T>>({ data: null, error: "", loading: active });
  const stopped = useRef(false);

  useEffect(() => {
    if (!active) return;
    stopped.current = false;
    let timer = 0;
    const tick = async () => {
      try {
        const data = await loader();
        if (stopped.current) return;
        setState({ data, error: "", loading: false });
      } catch (error) {
        if (stopped.current) return;
        setState({ data: null, error: error instanceof ApiFailure ? error.message : String(error), loading: false });
      }
      timer = window.setTimeout(tick, everyMs);
    };
    void tick();
    return () => {
      stopped.current = true;
      window.clearTimeout(timer);
    };
  }, [active, everyMs]);

  return state;
}

/** One-line notice for a failed call: the request id is the thing support asks for. */
export function ErrorNote({ error }: { error: string }) {
  if (!error) return null;
  return (
    <div className="callout danger">
      <AlertTriangle size={15} />
      <span>{error}</span>
    </div>
  );
}

export function Busy({ label }: { label?: string }) {
  return (
    <div className="busy">
      <Loader2 size={15} className="spin" />
      <span>{label ?? "载入中…"}</span>
    </div>
  );
}

export function Empty({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

export function Kpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="kpi">
      <small>{label}</small>
      <strong>{value}</strong>
      {hint && <span>{hint}</span>}
    </div>
  );
}

export function Pill({ tone, children }: { tone?: string; children: ReactNode }) {
  return <span className={`pill ${tone ?? ""}`}>{children}</span>;
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}

/** `tone` colours the status word only; the text itself always comes from the API. */
export function statusTone(value: string | null | undefined): string {
  const text = (value ?? "").toUpperCase();
  if (text === "PASSED" || text === "SUCCEEDED" || text === "COMPLETE" || text === "READY" || text === "CLEAN") return "ok";
  if (text === "FAILED" || text === "ERROR" || text === "TIMED_OUT" || text === "EXPIRED") return "bad";
  if (text === "RUNNING" || text === "QUEUED" || text === "PENDING" || text === "COMPILING" || text === "NEEDS_REVIEW") return "warn";
  if (text === "CANCELLED" || text === "ARCHIVED" || text === "REVOKED" || text === "PAUSED") return "muted";
  return "";
}

export function shortDigest(value: string | null | undefined): string {
  if (!value) return "—";
  const hex = value.startsWith("sha256:") ? value.slice(7) : value;
  return `${hex.slice(0, 8)}…${hex.slice(-4)}`;
}

export function fmtMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} s`;
}

export function fmtBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

export function fmtWhen(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "刚刚";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return date.toLocaleDateString("zh-CN");
}

export function SearchBox({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <div className="search-box">
      <Search size={14} />
      <input value={value} placeholder={placeholder} onChange={(event) => onChange(event.target.value)} />
    </div>
  );
}

export function ReloadButton({ onClick, disabled }: { onClick: () => void; disabled?: boolean }) {
  return (
    <button className="button" onClick={onClick} disabled={disabled}>
      <RefreshCw size={14} />
      刷新
    </button>
  );
}

/** Cursor paging: the server owns the cursor, the console only remembers the trail it walked. */
export function usePaged<T>(fetcher: (cursor: string | null) => Promise<{ items: T[]; next_cursor?: string | null }>) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);
  const [items, setItems] = useState<T[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(
    (index: number) => {
      setLoading(true);
      setError("");
      fetcher(cursors[index] ?? null)
        .then((page) => {
          setItems(page.items);
          setNext(page.next_cursor ?? null);
          setLoading(false);
        })
        .catch((error: unknown) => {
          setError(error instanceof ApiFailure ? error.message : String(error));
          setItems([]);
          setNext(null);
          setLoading(false);
        });
    },
    [cursors, fetcher],
  );

  useEffect(() => {
    load(page);
  }, [load, page]);

  return {
    items,
    error,
    loading,
    page,
    hasNext: Boolean(next),
    reload: () => load(page),
    first: () => setPage(0),
    prev: () => setPage((index) => Math.max(0, index - 1)),
    next: () => {
      if (next) setCursors((trail) => [...trail.slice(0, page + 1), next]);
      setPage((index) => index + 1);
    },
  };
}
