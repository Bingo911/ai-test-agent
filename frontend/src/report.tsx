/** The report is the evidence surface: fixed-denominator outcomes, locator attempts, ticketed artifacts (§12.1). */

import { useMemo, useState } from "react";
import { CheckCircle2, Download, Eye, Play, Sparkles, XCircle } from "lucide-react";

import { ApiFailure, api, localPath } from "./api";
import type { EvidenceItem, Report, StepDetail } from "./types";
import { Busy, ErrorNote, Kpi, Pill, fmtBytes, fmtMs, fmtTime, shortDigest, statusTone, useAsync } from "./ui";
import { useSession } from "./session";

function artifactIdOf(ref: string): string {
  return ref.startsWith("artifact:") ? ref.slice("artifact:".length) : ref;
}

/** Mirrors the backend's ANALYSABLE_OUTCOMES: only these runs have a failure worth diagnosing. */
const ANALYSABLE_OUTCOMES = new Set(["FAILED", "ERROR", "TIMED_OUT"]);

export function ReportView({ executionId, onOpenRun }: { executionId: string; onOpenRun: (id: string) => void }) {
  const state = useAsync<Report>(() => api.get<Report>(`/executions/${executionId}/report`), [executionId]);
  const [tab, setTab] = useState<"steps" | "evidence" | "analysis">("steps");

  if (state.loading && !state.data) return <Busy label="载入报告…" />;
  if (state.error && !state.data) return <ErrorNote error={state.error} />;
  const report = state.data;
  if (!report) return null;

  const execution = report.execution;
  const failed = (report.steps ?? []).filter((step) => step.status === "FAILED" || step.status === "ERROR");

  return (
    <div className="report">
      <div className="pane-head">
        <div>
          <h2>
            {report.case.case_name} <span className="muted">r{report.case.revision_no}</span>
          </h2>
          <small className="muted">
            {execution.id.slice(0, 8)} · {execution.browser} {execution.browser_version ?? ""} · 报告阶段 {report.report_phase} · 生成于 {fmtTime(report.generated_at)}
          </small>
        </div>
        <div className="pane-actions">
          <Pill tone={statusTone(execution.outcome ?? execution.status)}>{execution.outcome ?? execution.status}</Pill>
          <button className="button" onClick={() => onOpenRun(execution.id)}>
            <Play size={14} />
            运行详情
          </button>
        </div>
      </div>

      <div className="kpi-row">
        <Kpi label="排队" value={fmtMs(execution.durations?.queued_ms)} />
        <Kpi label="执行" value={fmtMs(execution.durations?.active_ms)} />
        <Kpi label="人工" value={fmtMs(execution.durations?.human_ms)} hint={`接管 ${execution.human_tasks_used} 次`} />
        <Kpi label="总计" value={fmtMs(execution.durations?.total_ms)} />
        <Kpi label="证据" value={`${report.evidence.total} 项`} hint={`${fmtBytes(report.evidence.bytes)} · ${report.evidence.status}`} />
      </div>

      {report.warnings.length > 0 && (
        <div className="callout warn">
          <XCircle size={15} />
          <span>{report.warnings.join("；")}</span>
        </div>
      )}

      <div className="repro">
        <div>
          <small>源摘要</small>
          <strong>{shortDigest(report.case.source_digest)}</strong>
        </div>
        <div>
          <small>IR 摘要</small>
          <strong>{shortDigest(report.case.ir_digest)}</strong>
        </div>
        <div>
          <small>编译方式</small>
          <strong>
            {report.case.compile?.compiler_mode ?? "—"} · {report.case.compile?.compiler_version ?? "—"}
          </strong>
        </div>
        <div>
          <small>环境</small>
          <strong>
            {report.environment.base_url} · v{report.environment.environment_revision_id.slice(0, 8)}
          </strong>
        </div>
        <div>
          <small>允许域名</small>
          <strong>{(report.environment.allowed_domains ?? []).join(", ")}</strong>
        </div>
      </div>

      <div className="tabs">
        <button className={tab === "steps" ? "tab on" : "tab"} onClick={() => setTab("steps")}>
          步骤 ({report.steps.length})
        </button>
        <button className={tab === "evidence" ? "tab on" : "tab"} onClick={() => setTab("evidence")}>
          证据 ({report.evidence.total})
        </button>
        <button className={tab === "analysis" ? "tab on" : "tab"} onClick={() => setTab("analysis")}>
          失败分析
        </button>
      </div>

      {tab === "steps" && (
        <section className="card">
          {failed.length === 0 && <div className="empty">所有步骤均通过，没有失败步骤。</div>}
          {report.steps.map((step) => (
            <StepBlock key={step.step_id} step={step} />
          ))}
        </section>
      )}

      {tab === "evidence" && <EvidencePanel report={report} />}

      {tab === "analysis" && <AnalysisPanel report={report} executionId={execution.id} onReload={state.reload} />}

      {(report.human ?? []).length > 0 && (
        <section className="card">
          <header>
            <strong>人工接管</strong>
            <span className="muted">接管只影响该步，其余步骤仍按 IR 执行（§10.3）</span>
          </header>
          <table className="table">
            <thead>
              <tr>
                <th>步骤</th>
                <th>原因</th>
                <th>状态</th>
                <th>开始</th>
                <th>结束</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {report.human.map((item) => (
                <tr key={item.human_task_id}>
                  <td>{item.step_id}</td>
                  <td>{item.reason}</td>
                  <td>
                    <Pill tone={statusTone(item.status)}>{item.status}</Pill>
                  </td>
                  <td>{fmtTime(item.started_at)}</td>
                  <td>{fmtTime(item.ended_at)}</td>
                  <td className="clip">{item.note ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}

function StepBlock({ step }: { step: StepDetail }) {
  const failed = step.status === "FAILED" || step.status === "ERROR";
  return (
    <article className={`step-block ${failed ? "bad" : ""}`}>
      <header>
        <span className="step-no">{String(step.step_no).padStart(2, "0")}</span>
        <div className="step-title">
          <strong>
            {step.action} · {step.description ?? "—"}
          </strong>
          <small>
            {step.step_id} · {fmtMs(step.duration_ms)}
            {step.locator_strategy ? ` · 策略 ${step.locator_strategy}` : ""}
            {step.passed_by_human ? " · 人工判定通过" : ""}
            {step.resume_phase ? ` · 恢复阶段 ${step.resume_phase}` : ""}
          </small>
        </div>
        <Pill tone={statusTone(step.status)}>{step.status}</Pill>
      </header>
      {(step.locator_attempts ?? []).length > 0 && (
        <table className="table compact">
          <thead>
            <tr>
              <th>策略</th>
              <th>来源</th>
              <th>命中</th>
              <th>结果</th>
              <th>耗时</th>
            </tr>
          </thead>
          <tbody>
            {step.locator_attempts.map((attempt, index) => (
              <tr key={`${attempt.strategy}-${index}`}>
                <td>{attempt.strategy}</td>
                <td>{attempt.source}</td>
                <td>{attempt.matched}</td>
                <td>
                  <Pill tone={statusTone(attempt.outcome)}>{attempt.outcome}</Pill>
                </td>
                <td>{fmtMs(attempt.elapsed_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {step.error && (
        <pre className="step-error">
          {JSON.stringify(step.error, null, 2)}
        </pre>
      )}
      {(step.artifacts ?? []).length > 0 && (
        <div className="chips">
          {step.artifacts.map((item) => (
            <Pill key={item.ref}>
              {item.kind} · {item.name}
            </Pill>
          ))}
        </div>
      )}
    </article>
  );
}

function EvidencePanel({ report }: { report: Report }) {
  const { can } = useSession();
  const evidence = report.evidence;

  return (
    <section className="card">
      <header>
        <strong>证据</strong>
        <span className="muted">
          状态 {evidence.status} · 预算 {fmtBytes(evidence.budget_bytes)} · {evidence.access}
        </span>
      </header>
      <div className="chips">
        {Object.entries(evidence.by_kind ?? {}).map(([kind, count]) => (
          <Pill key={kind}>
            {kind} × {count}
          </Pill>
        ))}
        {evidence.privacy_excluded_kinds.map((kind) => (
          <Pill key={`privacy-${kind}`} tone="muted">
            隐私排除 {kind}
          </Pill>
        ))}
        {evidence.missing.map((ref) => (
          <Pill key={`missing-${ref}`} tone="bad">
            缺失 {shortDigest(ref)}
          </Pill>
        ))}
      </div>
      {evidence.items.length === 0 && <div className="empty">这次运行没有留存证据。</div>}
      <table className="table">
        <thead>
          <tr>
            <th>类型</th>
            <th>名称</th>
            <th>步骤</th>
            <th>大小</th>
            <th>敏感级</th>
            <th>上传</th>
            <th>到期</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {evidence.items.map((item) => (
            <EvidenceRow key={item.ref} item={item} canSensitive={can("sensitive_artifact_read")} />
          ))}
        </tbody>
      </table>
    </section>
  );
}

function EvidenceRow({ item, canSensitive }: { item: EvidenceItem; canSensitive: boolean }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<string>("");
  const blocked = item.requires_authorization && !canSensitive;

  async function fetchBlob(usage: string): Promise<Blob> {
    const issued = await api.post<{ download_url: string }>(`/artifacts/${artifactIdOf(item.ref)}/download-ticket`, { usage });
    return api.blob(localPath(issued.download_url));
  }

  async function download() {
    setBusy(true);
    setError("");
    try {
      const blob = await fetchBlob("view");
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = item.name || `${item.kind}-${item.step_id ?? "artifact"}`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    } finally {
      setBusy(false);
    }
  }

  async function inspect() {
    setBusy(true);
    setError("");
    try {
      const blob = await fetchBlob("debug");
      if (item.media_type.startsWith("image/")) {
        setPreview(URL.createObjectURL(blob));
      } else {
        const text = await blob.text();
        setPreview(text.slice(0, 200_000));
      }
    } catch (error) {
      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <tr>
        <td>{item.kind}</td>
        <td className="clip">{item.name}</td>
        <td>{item.step_id ?? "—"}</td>
        <td>{fmtBytes(item.size)}</td>
        <td>
          <Pill tone={item.sensitivity === "NORMAL" ? "" : "warn"}>{item.sensitivity}</Pill>
        </td>
        <td>
          <Pill tone={statusTone(item.upload_status)}>{item.upload_status}</Pill>
        </td>
        <td>{fmtTime(item.expires_at)}</td>
        <td className="row-actions">
          {blocked ? (
            <span className="muted">需要 SENSITIVE_ARTIFACT_READ</span>
          ) : (
            <>
              <button className="plain" disabled={busy || !item.available} onClick={inspect} title="查看内容">
                <Eye size={14} />
              </button>
              <button className="plain" disabled={busy || !item.available} onClick={download} title="下载">
                <Download size={14} />
              </button>
            </>
          )}
        </td>
      </tr>
      {error && (
        <tr>
          <td colSpan={8}>
            <ErrorNote error={error} />
          </td>
        </tr>
      )}
      {preview && (
        <tr>
          <td colSpan={8} className="preview">
            {item.media_type.startsWith("image/") ? (
              <img src={preview} alt={`${item.name} 预览`} />
            ) : (
              <pre>{preview}</pre>
            )}
            <button className="plain close-preview" onClick={() => setPreview("")}>
              关闭预览
            </button>
          </td>
        </tr>
      )}
    </>
  );
}

function AnalysisPanel({ report, executionId, onReload }: { report: Report; executionId: string; onReload: () => void }) {
  const { can, capabilities } = useSession();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const analysis = report.analysis;
  const analysable = ANALYSABLE_OUTCOMES.has(report.execution.outcome ?? "");

  const evidenceByRef = useMemo(() => {
    const map = new Map<string, EvidenceItem>();
    for (const item of report.evidence.items) map.set(item.ref, item);
    return map;
  }, [report.evidence.items]);

  if (!analysis) {
    const status = report.execution.analysis_status;
    let note = "这次运行还没有分析记录。";
    if (status === "PENDING" || status === "RUNNING") note = "分析进行中，完成后会自动追加到这份报告。";
    else if (!analysable) note = "这次运行没有失败，不需要分析。";
    return (
      <section className="card">
        <header>
          <strong>失败分析</strong>
          <span className="muted">规则先给出结论，AI 结论始终标记为假设（§12.2）</span>
        </header>
        <div className="empty">{note}</div>
      </section>
    );
  }

  return (
    <section className="card">
      <header>
        <strong>失败分析</strong>
        <div className="card-actions">
          <Pill tone={statusTone(analysis.status)}>{analysis.status}</Pill>
          <Pill>{`revision ${analysis.revision}`}</Pill>
          {analysable && can("execution_run") && capabilities?.features.failure_analysis === "available" && (
            <button
              className="button"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError("");
                try {
                  await api.post(`/executions/${executionId}/analysis`, {}, { "Idempotency-Key": `analysis:${executionId}:${Date.now()}` });
                  window.setTimeout(onReload, 2500);
                } catch (error) {
                  setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                } finally {
                  setBusy(false);
                }
              }}
            >
              <Sparkles size={14} />
              {busy ? "分析中…" : "重新分析"}
            </button>
          )}
        </div>
      </header>
      <ErrorNote error={error} />
      <div className="analysis">
        <div className="verdict">
          {report.execution.outcome === "PASSED" ? <CheckCircle2 size={17} /> : <XCircle size={17} />}
          <div>
            <strong>{analysis.failure_type ?? "UNKNOWN"}</strong>
            <small>{analysis.reason ?? "—"}</small>
          </div>
          <span className="confidence">{Math.round((analysis.confidence ?? 0) * 100)}%</span>
        </div>
        <p className="suggestion">{analysis.suggestion ?? "—"}</p>
        <dl className="kv">
          <div>
            <dt>来源</dt>
            <dd>
              {analysis.source}
              {analysis.is_hypothesis ? " · 假设，非结论" : ""}
            </dd>
          </div>
          <div>
            <dt>模型</dt>
            <dd>{analysis.model ?? "未使用"}</dd>
          </div>
          <div>
            <dt>提示词版本</dt>
            <dd>{analysis.prompt_version ?? "—"}</dd>
          </div>
          <div>
            <dt>错误码</dt>
            <dd>{analysis.error_code ?? "—"}</dd>
          </div>
        </dl>
        {analysis.evidence_refs.length > 0 && (
          <div className="chips">
            {analysis.evidence_refs.map((ref) => (
              <Pill key={ref}>{evidenceByRef.get(ref)?.name ?? ref}</Pill>
            ))}
          </div>
        )}
        {analysis.history.length > 1 && (
          <details className="raw">
            <summary>分析历史 ({analysis.history.length})</summary>
            <pre>{JSON.stringify(analysis.history, null, 2)}</pre>
          </details>
        )}
      </div>
    </section>
  );
}
