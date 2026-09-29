/** Workbench: quality numbers the server computed, recent runs, and what is waiting for a human (§12.3, §13.5). */

import { useEffect, useMemo, useState } from "react";
import { Activity, Bot, Hand, Play, ShieldCheck, Sparkles, TrendingUp } from "lucide-react";

import { api } from "./api";
import { useSession } from "./session";
import type { ExecutionSummary, HumanTask, Metrics } from "./types";
import { Busy, ErrorNote, Kpi, Pill, ReloadButton, fmtMs, fmtWhen, statusTone, useAsync } from "./ui";

export function OverviewPage({ onOpenRun, onOpenAssist }: { onOpenRun: (id: string) => void; onOpenAssist: () => void }) {
  const { projectId, project, capabilities, whoami } = useSession();
  const metrics = useAsync<Metrics>(() => api.get(`/projects/${projectId}/metrics`), [projectId]);
  const recent = useAsync<{ items: ExecutionSummary[] }>(
    () => api.get(`/projects/${projectId}/executions`, { limit: 8 }),
    [projectId],
  );
  const [waiting, setWaiting] = useState<HumanTask[]>([]);

  useEffect(() => {
    if (!projectId) return;
    api
      .get<{ items: HumanTask[] }>(`/projects/${projectId}/human-tasks`, { status: "open", limit: 10 })
      .then((page) => setWaiting(page.items))
      .catch(() => setWaiting([]));
  }, [projectId, recent.data]);

  const passRate = useMemo(() => {
    const value = metrics.data;
    if (!value) return "—";
    return `${(value.pass_rate * 100).toFixed(1)}%`;
  }, [metrics.data]);

  if (!projectId) {
    return (
      <div className="pane">
        <div className="empty">还没有项目。创建项目后即可编写用例并运行。</div>
      </div>
    );
  }

  return (
    <div className="overview">
      <div className="pane-head">
        <div>
          <h2>{project?.display_name ?? project?.name ?? "项目"}</h2>
          <small className="muted">
            {whoami?.display_name} · 角色 {whoami?.roles?.["*"] ?? whoami?.roles?.[projectId] ?? "—"} · {projectId.slice(0, 8)}
          </small>
        </div>
        <div className="pane-actions">
          <ReloadButton
            onClick={() => {
              metrics.reload();
              recent.reload();
            }}
            disabled={metrics.loading || recent.loading}
          />
        </div>
      </div>

      <ErrorNote error={metrics.error || recent.error} />

      <div className="kpi-row">
        <Kpi label="通过率" value={passRate} hint={metrics.data ? `${metrics.data.passed}/${metrics.data.pass_rate_denominator} 次有结论` : "—"} />
        <Kpi label="执行次数" value={String(metrics.data?.total ?? 0)} hint={metrics.data ? `失败 ${metrics.data.failed}` : "—"} />
        <Kpi label="平均执行" value={fmtMs(metrics.data?.avg_active_ms)} hint={metrics.data ? `P95 ${fmtMs(metrics.data.p95_active_ms)}` : "—"} />
        <Kpi label="定位降级" value={metrics.data ? `${(metrics.data.locator.degradation_rate * 100).toFixed(1)}%` : "—"} hint={metrics.data ? `${metrics.data.locator.degraded_steps}/${metrics.data.locator.located_steps} 步` : "—"} />
        <Kpi label="人工介入" value={String(metrics.data?.human_interventions ?? 0)} hint={metrics.data ? `占比 ${(metrics.data.human_intervention_rate * 100).toFixed(1)}%` : "—"} />
        <Kpi label="基础设施" value={String((metrics.data?.infrastructure.ERROR ?? 0) + (metrics.data?.infrastructure.TIMED_OUT ?? 0) + (metrics.data?.infrastructure.CANCELLED ?? 0))} hint="ERROR / 超时 / 取消" />
      </div>

      <div className="grid-2">
        <section className="card">
          <header>
            <strong>
              <Activity size={14} /> 最近运行
            </strong>
            <span className="muted">队列与执行状态由服务端给出</span>
          </header>
          {recent.loading && !recent.data ? <Busy /> : null}
          {(recent.data?.items ?? []).length === 0 && !recent.loading && <div className="empty">还没有运行记录，去用例页确认后发起第一次运行。</div>}
          <ul className="rows tight">
            {(recent.data?.items ?? []).map((row) => (
              <li key={row.id} className="row" onClick={() => onOpenRun(row.id)}>
                <div className="row-main">
                  <strong>{row.case_name}</strong>
                  <small>
                    {row.browser} · {fmtMs(row.active_ms)} · {fmtWhen(row.queued_at)}
                    {row.error_code ? ` · ${row.error_code}` : ""}
                  </small>
                </div>
                <div className="row-side">
                  <Pill tone={statusTone(row.outcome ?? row.status)}>{row.outcome ?? row.status}</Pill>
                  {row.artifact_status && <Pill tone={statusTone(row.artifact_status)}>{row.artifact_status}</Pill>}
                </div>
              </li>
            ))}
          </ul>
        </section>

        <div className="stack">
          <section className="card">
            <header>
              <strong>
                <Hand size={14} /> 等待接管
              </strong>
              <span className="muted">{waiting.length} 个任务</span>
            </header>
            {waiting.length === 0 ? (
              <div className="empty">没有步骤在等待人工处理。</div>
            ) : (
              <ul className="rows tight">
                {waiting.map((task) => (
                  <li key={task.human_task_id} className="row" onClick={onOpenAssist}>
                    <div className="row-main">
                      <strong>
                        步骤 {task.step_id} · {task.reason}
                      </strong>
                      <small>剩余 {task.remaining_seconds}s · {task.holds_control ? "你控制中" : task.controller_id ? "他人控制" : "无人认领"}</small>
                    </div>
                    <Pill tone={statusTone(task.status)}>{task.status}</Pill>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="card">
            <header>
              <strong>
                <TrendingUp size={14} /> 失败类型分布
              </strong>
              <span className="muted">规则分类结果</span>
            </header>
            {Object.keys(metrics.data?.failure_types ?? {}).length === 0 ? (
              <div className="empty">当前窗口没有失败样本。</div>
            ) : (
              <ul className="bars">
                {Object.entries(metrics.data?.failure_types ?? {})
                  .sort((a, b) => b[1] - a[1])
                  .map(([type, count]) => (
                    <li key={type}>
                      <span>{type}</span>
                      <span className="bar">
                        <i style={{ width: `${Math.min(100, (count / Math.max(1, metrics.data?.failed ?? 1)) * 100)}%` }} />
                      </span>
                      <span>{count}</span>
                    </li>
                  ))}
              </ul>
            )}
          </section>

          <section className="card">
            <header>
              <strong>
                <ShieldCheck size={14} /> 部署能力
              </strong>
              <span className="muted">/capabilities</span>
            </header>
            <dl className="kv">
              <div>
                <dt>API / IR</dt>
                <dd>
                  {capabilities?.api_version ?? "—"} / {capabilities?.ir_version ?? "—"} · 编译器 {capabilities?.compiler_version ?? "—"}
                </dd>
              </div>
              <div>
                <dt>鉴权模式</dt>
                <dd>{capabilities?.auth_mode ?? "—"}</dd>
              </div>
              <div>
                <dt>AI 编译</dt>
                <dd>{capabilities?.features.ai_compilation ?? "—"}</dd>
              </div>
              <div>
                <dt>视觉定位</dt>
                <dd>
                  {capabilities?.features.ai_vision ?? "—"}
                  {capabilities?.features.ai_vision === "disabled" ? "（配置关闭）" : ""}
                </dd>
              </div>
              <div>
                <dt>人工接管</dt>
                <dd>{capabilities?.features.human_handoff ?? "—"}</dd>
              </div>
              <div>
                <dt>浏览器通道</dt>
                <dd>{(capabilities?.browsers ?? []).join(", ")}</dd>
              </div>
            </dl>
            <div className="chips">
              {(capabilities?.actions ?? []).map((action) => (
                <Pill key={action}>
                  <Play size={9} />
                  {action}
                </Pill>
              ))}
            </div>
            <small className="muted">
              <Sparkles size={11} /> 支持 {capabilities?.conditions.length ?? 0} 种断言条件，单步上限 {capabilities?.limits.step_timeout_max_ms ?? "—"}ms
            </small>
          </section>
        </div>
      </div>

      <p className="footnote">
        <Bot size={12} /> 页面展示的运行、证据与结论全部来自服务端接口；本控制台不执行被测页面的 HTML 或脚本。
      </p>
    </div>
  );
}
