/** Runs: launch from a confirmed IR, then follow the §9.1 state machine through a ticketed SSE journal. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Ban, Pause, Play, RotateCcw, Sparkles } from "lucide-react";

import { ApiFailure, absoluteUrl, api, listQuery } from "./api";
import { useSession } from "./session";
import type { CompileArtifact, Environment, ExecutionDetail, ExecutionSummary, HumanTask } from "./types";
import { Busy, ErrorNote, Kpi, Pill, ReloadButton, fmtMs, fmtTime, shortDigest, statusTone, useAsync, usePaged } from "./ui";

const ACTIVE = new Set(["QUEUED", "PREPARING", "RUNNING", "PAUSING", "PAUSED", "NEEDS_HUMAN", "FINALIZING", "ANALYZING"]);
/** The journal emits named SSE events, so every name the backend can write has to be subscribed to (§13.4). */
const JOURNAL_EVENTS = [
  "stream.started",
  "stream.end",
  "error",
  "execution.status_changed",
  "execution.cancel_requested",
  "step.started",
  "step.finished",
  "step.skipped",
  "artifact.ready",
  "human.created",
  "human.resumed",
  "human.resume_rejected",
  "analysis.ready",
];

function key(prefix: string): string {
  return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 10)}`;
}

export function RunsPage({ focusId, onOpen }: { focusId: string | null; onOpen: (id: string) => void }) {
  const { projectId, can } = useSession();
  const [status, setStatus] = useState("");
  const [outcome, setOutcome] = useState("");

  const fetcher = useMemo(
    () => (cursor: string | null) =>
      api.get<{ items: ExecutionSummary[]; next_cursor?: string | null }>(
        `/projects/${projectId}/executions`,
        listQuery({ limit: 20, cursor }, { status: status || undefined, outcome: outcome || undefined }),
      ),
    [projectId, status, outcome],
  );
  const list = usePaged<ExecutionSummary>(fetcher);

  return (
    <div className="split">
      <section className="pane">
        <div className="pane-head">
          <h2>运行</h2>
          <div className="pane-actions">
            <ReloadButton onClick={list.reload} disabled={list.loading} />
            {can("execution_run") && <LaunchForm projectId={projectId} onLaunched={onOpen} />}
          </div>
        </div>
        <div className="filter-row">
          <select className="mini" value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">全部状态</option>
            {["QUEUED", "RUNNING", "PAUSED", "NEEDS_HUMAN", "FINALIZING", "FINISHED", "CANCELLED", "TIMED_OUT", "ERROR"].map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <select className="mini" value={outcome} onChange={(event) => setOutcome(event.target.value)}>
            <option value="">全部结果</option>
            {["PASSED", "FAILED", "ERROR", "CANCELLED", "TIMED_OUT"].map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </div>
        <ErrorNote error={list.error} />
        {list.loading && <Busy />}
        {!list.loading && list.items.length === 0 && <div className="empty">还没有运行记录。</div>}
        <ul className="rows">
          {list.items.map((row) => (
            <li key={row.id} className={focusId === row.id ? "row chosen" : "row"} onClick={() => onOpen(row.id)}>
              <div className="row-main">
                <strong>{row.case_name}</strong>
                <small>
                  r{row.revision_no} · {row.browser} · {fmtMs(row.active_ms)} · {fmtTime(row.queued_at)}
                </small>
              </div>
              <div className="row-side">
                {row.outcome && <Pill tone={statusTone(row.outcome)}>{row.outcome}</Pill>}
                <Pill tone={statusTone(row.status)}>{row.status}</Pill>
                {row.cancel_requested && <Pill tone="muted">取消中</Pill>}
                {row.human_tasks_used > 0 && <Pill tone="warn">人工 {row.human_tasks_used}</Pill>}
              </div>
            </li>
          ))}
        </ul>
        <div className="pager">
          <button className="button" disabled={list.page === 0} onClick={list.prev}>
            上一页
          </button>
          <span>第 {list.page + 1} 页</span>
          <button className="button" disabled={!list.hasNext} onClick={list.next}>
            下一页
          </button>
        </div>
      </section>

      <section className="pane wide">
        {focusId ? <RunDetail key={focusId} executionId={focusId} onOpen={onOpen} /> : <div className="placeholder">选择一次运行查看步骤、事件与证据。</div>}
      </section>
    </div>
  );
}

function LaunchForm({ projectId, onLaunched }: { projectId: string; onLaunched: (id: string) => void }) {
  const { capabilities } = useSession();
  const [open, setOpen] = useState(false);
  const [cases, setCases] = useState<{ case_id: string; name: string; title: string | null }[]>([]);
  const [caseId, setCaseId] = useState("");
  const [artifact, setArtifact] = useState<CompileArtifact | null>(null);
  const [artifactNote, setArtifactNote] = useState("");
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [revisionId, setRevisionId] = useState("");
  const [browser, setBrowser] = useState("");
  const [variables, setVariables] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    api
      .get<{ items: { case_id: string; name: string; title: string | null }[] }>(`/projects/${projectId}/cases`, { limit: 100 })
      .then((page) => setCases(page.items))
      .catch(() => undefined);
    api
      .get<{ items: Environment[] }>(`/projects/${projectId}/environments`)
      .then((page) => {
        setEnvironments(page.items);
        setRevisionId((current) => current || page.items.find((item) => item.current_revision)?.current_revision?.environment_revision_id || "");
      })
      .catch(() => undefined);
  }, [open, projectId]);

  useEffect(() => {
    if (!caseId) {
      setArtifact(null);
      return;
    }
    setArtifact(null);
    setArtifactNote("正在读取该用例的编译产物…");
    api
      .get<{ compile: { artifact_id: string } | null }>(`/cases/${caseId}`)
      .then((detail) => {
        if (!detail.compile) {
          setArtifactNote("该用例还没有编译产物，请先在用例页编译并确认。");
          return;
        }
        return api.get<CompileArtifact>(`/compilations/${detail.compile.artifact_id}`).then((item) => {
          if (item.status === "SUCCEEDED" && item.executable) {
            setArtifact(item);
            setArtifactNote("");
          } else {
            setArtifactNote(`最新编译产物状态为 ${item.status}，可运行标记为 ${item.executable ? "是" : "否"}。请先完成确认。`);
          }
        });
      })
      .catch(() => setArtifactNote("读取编译产物失败。"));
  }, [caseId]);

  return (
    <>
      <button className="button primary" onClick={() => setOpen(true)}>
        <Play size={14} />
        发起运行
      </button>
      {open && (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div className="modal tall" onClick={(event) => event.stopPropagation()}>
            <h3>发起运行</h3>
            <p className="muted">只能运行已确认的 IR；运行会冻结 IR 与环境版本，报告因此始终可复现（§3.2）。</p>
            <label className="field">
              <span>用例</span>
              <select value={caseId} onChange={(event) => setCaseId(event.target.value)}>
                <option value="">请选择</option>
                {cases.map((item) => (
                  <option key={item.case_id} value={item.case_id}>
                    {item.title ?? item.name}
                  </option>
                ))}
              </select>
            </label>
            <div className="field">
              <span>已确认的 IR</span>
              {artifact ? (
                <div className="picked">
                  {artifact.compiler_mode} · {shortDigest(artifact.ir_digest)} · {artifact.status}
                </div>
              ) : (
                <div className="empty">{artifactNote || "请选择用例"}</div>
              )}
            </div>
            <label className="field">
              <span>环境版本</span>
              <select value={revisionId} onChange={(event) => setRevisionId(event.target.value)}>
                <option value="">请选择</option>
                {environments.map((item) =>
                  item.current_revision ? (
                    <option key={item.current_revision.environment_revision_id} value={item.current_revision.environment_revision_id}>
                      {item.name} v{item.current_revision.version} · {item.current_revision.config.base_url}
                    </option>
                  ) : null,
                )}
              </select>
            </label>
            <label className="field">
              <span>浏览器</span>
              <select value={browser} onChange={(event) => setBrowser(event.target.value)}>
                <option value="">跟随环境配置</option>
                {(capabilities?.browsers ?? []).map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span>变量覆盖（JSON 对象）</span>
              <textarea rows={3} value={variables} onChange={(event) => setVariables(event.target.value)} placeholder='{"username":"qa_user"}' />
            </label>
            <ErrorNote error={error} />
            <div className="modal-actions">
              <button className="button" onClick={() => setOpen(false)}>
                取消
              </button>
              <button
                className="button primary"
                disabled={busy || !artifact || !revisionId}
                onClick={async () => {
                  let parsed: Record<string, unknown> = {};
                  if (variables.trim()) {
                    try {
                      const candidate = JSON.parse(variables) as unknown;
                      if (candidate === null || typeof candidate !== "object" || Array.isArray(candidate)) throw new Error("not an object");
                      parsed = candidate as Record<string, unknown>;
                    } catch {
                      setError("变量覆盖必须是合法的 JSON 对象。");
                      return;
                    }
                  }
                  setBusy(true);
                  setError("");
                  try {
                    const started = await api.post<{ id: string }>(
                      "/executions",
                      {
                        compile_artifact_id: artifact?.compile_artifact_id,
                        environment_revision_id: revisionId,
                        variables: parsed,
                        browser: browser || undefined,
                      },
                      { "Idempotency-Key": key("run") },
                    );
                    setOpen(false);
                    onLaunched(started.id);
                  } catch (error) {
                    setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? "排队中…" : "运行"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

export function RunDetail({ executionId, onOpen }: { executionId: string; onOpen: (id: string) => void }) {
  const { can } = useSession();
  const state = useAsync<ExecutionDetail>(() => api.get<ExecutionDetail>(`/executions/${executionId}`), [executionId]);
  const [events, setEvents] = useState<string[]>([]);
  const [actionError, setActionError] = useState("");
  const tail = useRef<HTMLDivElement>(null);
  const detail = state.data;
  const running = ACTIVE.has(detail?.status ?? "");
  const reload = state.reload;

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => reload(), 2500);
    return () => window.clearInterval(timer);
  }, [running, reload]);

  const push = useCallback((line: string) => {
    setEvents((prev) => [...prev.slice(-199), line]);
    window.requestAnimationFrame(() => {
      if (tail.current) tail.current.scrollTop = tail.current.scrollHeight;
    });
  }, []);

  useEffect(() => {
    setEvents([]);
    let socket: EventSource | null = null;
    let disposed = false;

    api
      .post<{ url: string }>(`/executions/${executionId}/events/ticket`)
      .then((issued) => {
        if (disposed) return;
        socket = new EventSource(absoluteUrl(issued.url));
        for (const name of JOURNAL_EVENTS) {
          socket.addEventListener(name, (message) => {
            const data = (message as MessageEvent).data as string | undefined;
            // A transport fault shares the name `error` with the journal frame of that name.
            if (data === undefined) return;
            push(`${name} ${data}`);
            if (name === "execution.status_changed" || name === "step.finished") reload();
            if (name === "stream.end" || name === "error") {
              // The journal tail is spent: an automatic reconnect would replay the ticket (§14.3).
              socket?.close();
              if (name === "stream.end") reload();
            }
          });
        }
        socket.onerror = () => {
          push("stream: 事件连接中断，改用快照轮询");
        };
      })
      .catch(() => push("stream: 无法签发事件票据"));

    return () => {
      disposed = true;
      socket?.close();
    };
  }, [executionId, push, reload]);

  async function act(path: "cancel" | "pause" | "rerun" | "analysis", body: Record<string, unknown> = {}) {
    setActionError("");
    try {
      const result = await api.post<{ id?: string }>(`/executions/${executionId}/${path}`, body, { "Idempotency-Key": key(path) });
      if (path === "rerun" && result.id) {
        onOpen(result.id);
        return;
      }
      reload();
    } catch (error) {
      setActionError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }

  if (state.loading && !detail) return <Busy label="载入运行…" />;
  if (state.error && !detail) return <ErrorNote error={state.error} />;
  if (!detail) return null;

  const summary = detail.step_summary ?? { total: 0, by_status: {}, passed: 0, failed: 0 };
  const settled = !ACTIVE.has(detail.status);

  return (
    <div className="run-detail">
      <div className="pane-head">
        <div>
          <h2>
            {detail.case_name} <span className="muted">r{detail.revision_no}</span>
          </h2>
          <small className="muted">
            {detail.id.slice(0, 8)} · {detail.browser} {detail.browser_version ?? ""} · 触发 {detail.trigger}
          </small>
        </div>
        <div className="pane-actions">
          {detail.outcome && <Pill tone={statusTone(detail.outcome)}>{detail.outcome}</Pill>}
          <Pill tone={statusTone(detail.status)}>{detail.status}</Pill>
          <ReloadButton onClick={reload} />
          <button className="button" onClick={() => onOpen(`report:${detail.id}`)}>
            查看报告
          </button>
          {can("execution_cancel_own") && !settled && !detail.cancel_requested && (
            <button className="button" onClick={() => act("cancel", { reason: "operator cancelled from the console" })}>
              <Ban size={14} />
              取消
            </button>
          )}
          {can("human_control") && detail.status === "RUNNING" && (
            <button className="button" onClick={() => act("pause", { reason: "operator requested a pause" })}>
              <Pause size={14} />
              暂停
            </button>
          )}
          {settled && (
            <>
              <button className="button" onClick={() => act("rerun")}>
                <RotateCcw size={14} />
                重跑
              </button>
              {detail.outcome === "FAILED" && (
                <button className="button" onClick={() => act("analysis")}>
                  <Sparkles size={14} />
                  重新分析
                </button>
              )}
            </>
          )}
        </div>
      </div>

      <ErrorNote error={actionError} />

      <div className="kpi-row">
        <Kpi label="执行耗时" value={fmtMs(detail.active_ms)} />
        <Kpi label="人工耗时" value={fmtMs(detail.human_ms)} hint={`接管 ${detail.human_tasks_used} 次`} />
        <Kpi label="步骤" value={`${summary.passed}/${summary.total}`} hint={`失败 ${summary.failed}`} />
        <Kpi label="证据" value={detail.artifact_status ?? "—"} hint={`分析 ${detail.analysis_status ?? "—"}`} />
        <Kpi label="状态版本" value={String(detail.state_version)} hint={`事件游标 ${detail.last_event_seq}`} />
      </div>

      <div className="grid-2">
        <section className="card">
          <header>
            <strong>步骤</strong>
            <span className="muted">策略与耗时来自服务器</span>
          </header>
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>动作</th>
                <th>目标</th>
                <th>状态</th>
                <th>耗时</th>
              </tr>
            </thead>
            <tbody>
              {(detail.steps ?? []).map((step) => (
                <tr key={step.step_id}>
                  <td>{step.step_no}</td>
                  <td>{step.action}</td>
                  <td className="clip">{step.description ?? "—"}</td>
                  <td>
                    <Pill tone={statusTone(step.status)}>{step.status}</Pill>
                    {step.error_code && <small className="bad-text">{step.error_code}</small>}
                  </td>
                  <td>{fmtMs(step.duration_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <header>
            <strong>事件流</strong>
            <span className="muted">SSE · 单次票据 · {events.length} 条</span>
          </header>
          <div className="log" ref={tail}>
            {events.length === 0 && <div className="empty">暂无事件；运行结束后快照会自动刷新。</div>}
            {events.map((line, index) => (
              <div key={`${index}-${line.slice(0, 12)}`}>{line}</div>
            ))}
          </div>
          {detail.human_task && <HumanCard task={detail.human_task} />}
        </section>
      </div>

      {detail.error && (
        <details className="raw" open>
          <summary>错误详情 · {detail.error_code ?? "ERROR"}</summary>
          <pre>{JSON.stringify(detail.error, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function HumanCard({ task }: { task: HumanTask }) {
  return (
    <div className="callout warn">
      <Pause size={15} />
      <span>
        步骤 {task.step_id} 需要人工接管 · {task.status} · 剩余 {task.remaining_seconds}s ·{" "}
        {task.holds_control ? "当前由你控制" : task.controller_id ? "他人控制中" : "无人控制"}
      </span>
    </div>
  );
}
