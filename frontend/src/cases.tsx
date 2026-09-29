/** Case authoring: Markdown DSL in, validated Test IR out, and the confirm gate that makes it runnable (§8.1, §13.2). */

import { useEffect, useMemo, useRef, useState } from "react";
import { Archive, Check, ClipboardCopy, GitBranch, Play, Plus, Save, Sparkles, Upload, X } from "lucide-react";

import { ApiFailure, api, listQuery } from "./api";
import { useSession } from "./session";
import type { CaseDetail, CaseRow, CompileArtifact } from "./types";
import { Busy, ErrorNote, Kpi, Pill, ReloadButton, SearchBox, shortDigest, statusTone, useAsync, usePaged } from "./ui";

const TEMPLATE = [
  "---",
  'dsl_version: "1.0"',
  "tags: [smoke]",
  "defaults:",
  "  timeout_ms: 15000",
  "---",
  "# 新测试用例",
  "",
  "## Step 1",
  "```yaml",
  "action: open",
  'url: "${env.base_url}/"',
  "```",
  "",
  "## Step 2",
  "```yaml",
  "action: assert",
  "condition:",
  "  kind: page_contains",
  "  expected: 关键词",
  "```",
].join("\n");

const STATUS_POLL_MS = 1200;
const COMPILING = new Set(["PENDING", "COMPILING", "QUEUED", "RUNNING"]);

function idempotencyKey(prefix: string): string {
  return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2, 10)}`;
}

export function CasesPage({ onRun }: { onRun: (executionId: string) => void }) {
  const { projectId, can } = useSession();
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [tag, setTag] = useState("");
  const [includeArchived, setIncludeArchived] = useState(false);

  const fetcher = useMemo(
    () => (cursor: string | null) =>
      api.get<{ items: CaseRow[]; next_cursor?: string | null }>(
        `/projects/${projectId}/cases`,
        listQuery({ limit: 20, cursor }, { search: search || undefined, tag: tag || undefined, include_archived: includeArchived ? "true" : undefined }),
      ),
    [projectId, search, tag, includeArchived],
  );
  const list = usePaged<CaseRow>(fetcher);

  return (
    <div className="split">
      <section className="pane">
        <div className="pane-head">
          <h2>测试用例</h2>
          <div className="pane-actions">
            <ReloadButton onClick={list.reload} disabled={list.loading} />
            {can("case_write") && (
              <NewCaseButton
                onCreate={async (payload) => {
                  const created = await api.post<{ case_id: string }>(`/projects/${projectId}/cases`, payload);
                  setSelected(created.case_id);
                  list.reload();
                }}
              />
            )}
          </div>
        </div>
        <div className="filter-row">
          <SearchBox value={search} onChange={setSearch} placeholder="按名称搜索" />
          <input className="mini" value={tag} onChange={(event) => setTag(event.target.value)} placeholder="标签" />
          <label className="check">
            <input type="checkbox" checked={includeArchived} onChange={(event) => setIncludeArchived(event.target.checked)} />
            含归档
          </label>
        </div>
        <ErrorNote error={list.error} />
        {list.loading && <Busy />}
        {!list.loading && list.items.length === 0 && <div className="empty">该项目还没有用例。</div>}
        <ul className="rows">
          {list.items.map((row) => (
            <li key={row.case_id} className={selected === row.case_id ? "row chosen" : "row"} onClick={() => setSelected(row.case_id)}>
              <div className="row-main">
                <strong>{row.title ?? row.name}</strong>
                <small>
                  {row.name} · r{row.revision_no} · {shortDigest(row.source_digest)}
                </small>
              </div>
              <div className="row-side">
                {row.tags.map((item) => (
                  <Pill key={item}>{item}</Pill>
                ))}
                <Pill tone={statusTone(row.compile_status)}>{row.compile_status}</Pill>
                {row.archived && <Pill tone="muted">归档</Pill>}
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
        {selected ? (
          <CaseEditor
            key={selected}
            caseId={selected}
            onRun={onRun}
            onDeleted={() => {
              setSelected(null);
              list.reload();
            }}
          />
        ) : (
          <div className="placeholder">
            <GitBranch size={22} />
            <p>选择一个用例开始编辑、编译与确认。</p>
          </div>
        )}
      </section>
    </div>
  );
}

function NewCaseButton({ onCreate }: { onCreate: (payload: Record<string, unknown>) => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  return (
    <>
      <button className="button primary" onClick={() => setOpen(true)}>
        <Plus size={14} />
        新建
      </button>
      {open && (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <h3>新建用例</h3>
            <p className="muted">名称在项目内唯一；Markdown 内容使用 DSL 1.0 的动作块。</p>
            <input autoFocus value={name} onChange={(event) => setName(event.target.value)} placeholder="例如 login-flow" />
            <ErrorNote error={error} />
            <div className="modal-actions">
              <button className="button" onClick={() => setOpen(false)}>
                取消
              </button>
              <button
                className="button primary"
                disabled={busy || !name.trim()}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  try {
                    await onCreate({ name: name.trim(), markdown: TEMPLATE, tags: [], dsl_version: "1.0" });
                    setOpen(false);
                    setName("");
                  } catch (error) {
                    setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? "创建中…" : "创建"}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function CaseEditor({ caseId, onRun, onDeleted }: { caseId: string; onRun: (id: string) => void; onDeleted: () => void }) {
  const { can, capabilities, projectId } = useSession();
  const state = useAsync<CaseDetail>(() => api.get<CaseDetail>(`/cases/${caseId}`), [caseId]);
  const [markdown, setMarkdown] = useState("");
  const [revisionBusy, setRevisionBusy] = useState(false);
  const [compileBusy, setCompileBusy] = useState(false);
  const [artifact, setArtifact] = useState<CompileArtifact | null>(null);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const loaded = useRef(false);

  useEffect(() => {
    if (!state.data || loaded.current) return;
    setMarkdown(state.data.markdown);
    loaded.current = true;
  }, [state.data]);

  useEffect(() => {
    const compile = state.data?.compile;
    if (!compile) return;
    if (artifact?.compile_artifact_id === compile.artifact_id) return;
    api.get<CompileArtifact>(`/compilations/${compile.artifact_id}`).then(setArtifact).catch(() => undefined);
  }, [state.data, artifact?.compile_artifact_id]);

  const dirty = Boolean(state.data) && markdown !== state.data?.markdown;
  const compileState = artifact?.status ?? state.data?.compile_status ?? "";
  const pollActive = COMPILING.has(compileState);

  useEffect(() => {
    if (!pollActive || !artifact) return;
    const timer = window.setInterval(() => {
      api
        .get<CompileArtifact>(`/compilations/${artifact.compile_artifact_id}`)
        .then((next) => {
          setArtifact(next);
          if (!COMPILING.has(next.status)) state.reload();
        })
        .catch(() => undefined);
    }, STATUS_POLL_MS);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line
  }, [pollActive, artifact?.compile_artifact_id]);

  async function saveRevision() {
    if (!state.data) return;
    setRevisionBusy(true);
    setActionError("");
    try {
      const created = await api.post<{ revision_id: string }>(
        `/cases/${caseId}/revisions`,
        { markdown, dsl_version: state.data.current_revision?.dsl_version ?? "1.0" },
        { "If-Match": state.data.etag },
      );
      setNotice(`已保存新版本 ${created.revision_id.slice(0, 8)}`);
      await api.post(`/case-revisions/${created.revision_id}/compile`, { use_ai: false, force: true }, { "Idempotency-Key": idempotencyKey("compile") });
      state.reload();
    } catch (error) {
      setActionError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    } finally {
      setRevisionBusy(false);
    }
  }

  async function compileCurrent(useAi: boolean) {
    const revision = state.data?.current_revision?.revision_id;
    if (!revision) return;
    setCompileBusy(true);
    setActionError("");
    try {
      const started = await api.post<{ compile_artifact_id: string }>(
        `/case-revisions/${revision}/compile`,
        { use_ai: useAi, force: true },
        { "Idempotency-Key": idempotencyKey("compile") },
      );
      setArtifact(await api.get<CompileArtifact>(`/compilations/${started.compile_artifact_id}`));
      state.reload();
    } catch (error) {
      setActionError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    } finally {
      setCompileBusy(false);
    }
  }

  async function confirmIr() {
    if (!artifact?.ir_digest) return;
    setActionError("");
    try {
      const confirmed = await api.post<CompileArtifact>(`/compilations/${artifact.compile_artifact_id}/confirm`, { ir_digest: artifact.ir_digest }, { "Idempotency-Key": idempotencyKey("confirm") });
      setArtifact(confirmed);
      setNotice("IR 已确认，可以发起运行");
      state.reload();
    } catch (error) {
      setActionError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }

  async function runNow() {
    if (!artifact?.compile_artifact_id) return;
    setActionError("");
    try {
      const environments = await api.get<{ items: { environment_id: string; current_revision: { environment_revision_id: string } | null }[] }>(`/projects/${projectId}/environments`);
      const chosen = environments.items.find((item) => item.current_revision);
      if (!chosen?.current_revision) {
        setActionError("该项目还没有可用的环境版本，请先在设置中发布一个。");
        return;
      }
      const started = await api.post<{ id: string }>(
        "/executions",
        { compile_artifact_id: artifact.compile_artifact_id, environment_revision_id: chosen.current_revision.environment_revision_id },
        { "Idempotency-Key": idempotencyKey("run") },
      );
      onRun(started.id);
    } catch (error) {
      setActionError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }

  async function archiveCase(archived: boolean) {
    if (!state.data) return;
    setActionError("");
    try {
      await api.patch(`/cases/${caseId}`, { archived }, { "If-Match": state.data.etag });
      if (archived) {
        onDeleted();
        return;
      }
      setNotice("已取消归档");
      state.reload();
    } catch (error) {
      setActionError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
    }
  }

  async function importFile(file?: File) {
    if (!file) return;
    const text = await file.text();
    setMarkdown(text);
    setNotice(`已导入 ${file.name}，保存后成为新版本`);
  }

  if (state.loading && !state.data) return <Busy label="载入用例…" />;
  if (state.error) return <ErrorNote error={state.error} />;
  const detail = state.data;
  if (!detail) return null;

  const steps = (artifact?.ir?.steps ?? detail.compile?.ir?.steps ?? []) as { id: string; action: string; target?: { description?: string } }[];
  const diagnostics = artifact?.diagnostics ?? detail.compile?.diagnostics ?? [];
  const reviewItems = artifact?.review_items ?? detail.compile?.review_items ?? [];
  const confirmedAt = artifact?.confirmed_at ?? detail.compile?.confirmed_at ?? null;

  return (
    <div className="case-editor">
      <div className="pane-head">
        <div>
          <h2>{detail.title ?? detail.name}</h2>
          <small className="muted">
            {detail.name} · r{detail.revision_no} · IR {detail.compile?.ir ? "1.0" : "—"}
          </small>
        </div>
        <div className="pane-actions">
          <Pill tone={statusTone(compileState)}>{compileState || "NOT_COMPILED"}</Pill>
          {can("case_write") && (
            <button className="button" disabled={revisionBusy || !dirty} onClick={saveRevision}>
              <Save size={14} />
              {revisionBusy ? "保存中…" : dirty ? "保存并编译" : "已保存"}
            </button>
          )}
          {can("case_compile") && (
            <button className="button" disabled={compileBusy} onClick={() => compileCurrent(false)}>
              <Sparkles size={14} />
              确定性编译
            </button>
          )}
          {can("case_compile") && capabilities?.features.ai_compilation === "available" && (
            <button className="button" disabled={compileBusy} onClick={() => compileCurrent(true)}>
              <Sparkles size={14} />
              AI 编译
            </button>
          )}
          {can("case_write") && (
            <button className="button" disabled={revisionBusy} onClick={() => archiveCase(!detail.archived)}>
              <Archive size={14} />
              {detail.archived ? "取消归档" : "归档"}
            </button>
          )}
        </div>
      </div>

      {actionError && <ErrorNote error={actionError} />}
      {notice && (
        <div className="callout ok">
          <Check size={15} />
          <span>{notice}</span>
        </div>
      )}

      <div className="grid-2">
        <section className="card">
          <header>
            <strong>Markdown DSL</strong>
            <div className="card-actions">
              <label className="file-input">
                <Upload size={13} />
                导入 .md
                <input type="file" accept=".md,text/markdown" onChange={(event) => importFile(event.target.files?.[0])} />
              </label>
              <button
                className="plain"
                onClick={() => navigator.clipboard?.writeText(markdown).then(() => setNotice("已复制到剪贴板"), () => setNotice("浏览器拒绝了剪贴板访问"))}
              >
                <ClipboardCopy size={13} />
              </button>
            </div>
          </header>
          <textarea
            className="code"
            spellCheck={false}
            value={markdown}
            onChange={(event) => setMarkdown(event.target.value)}
            rows={22}
            disabled={!can("case_write")}
          />
          <footer className="row-meta">
            <span>{markdown.length} 字符</span>
            <span>{(markdown.match(/^## Step /gm) ?? []).length} 个步骤</span>
            <span>上限 {capabilities?.limits.max_case_bytes ?? "—"} 字符 / {capabilities?.limits.max_case_steps ?? "—"} 步</span>
          </footer>
        </section>

        <section className="card">
          <header>
            <strong>Test IR</strong>
            <div className="card-actions">
              {artifact?.ir_digest && (
                <button className="button primary" disabled={Boolean(confirmedAt) || !artifact.executable} onClick={confirmIr}>
                  <Check size={14} />
                  {confirmedAt ? "已确认" : "确认 IR"}
                </button>
              )}
              {confirmedAt && artifact?.executable && (
                <button className="button primary" onClick={runNow}>
                  <Play size={14} />
                  运行
                </button>
              )}
            </div>
          </header>
          <div className="ir-strip">
            {steps.length === 0 && <div className="empty">尚未生成 IR，请先编译。</div>}
            {steps.map((step, index) => (
              <div className="ir-chip" key={step.id ?? index}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{step.action}</strong>
                  <small>{step.target?.description ?? step.id}</small>
                </div>
              </div>
            ))}
          </div>
          <dl className="kv">
            <div>
              <dt>IR 摘要</dt>
              <dd>{shortDigest(artifact?.ir_digest)}</dd>
            </div>
            <div>
              <dt>编译器</dt>
              <dd>
                {artifact?.compiler_mode ?? "—"} · {artifact?.compiler_version ?? "—"}
              </dd>
            </div>
            <div>
              <dt>模型</dt>
              <dd>{artifact?.model ?? "未使用"}</dd>
            </div>
            <div>
              <dt>确认</dt>
              <dd>{confirmedAt ? new Date(confirmedAt).toLocaleString("zh-CN", { hour12: false }) : "未确认"}</dd>
            </div>
          </dl>
          {diagnostics.length > 0 && (
            <ul className="list danger">
              {diagnostics.map((item, index) => (
                <li key={`${item.code}-${index}`}>
                  <X size={13} />
                  <span>
                    <strong>{item.code}</strong> {item.message}
                    {item.source_range ? ` (第 ${item.source_range.start_line} 行)` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {reviewItems.length > 0 && (
            <ul className="list warn">
              {reviewItems.map((item, index) => (
                <li key={`${item.code}-${index}`}>
                  <Sparkles size={13} />
                  <span>
                    <strong>{item.code}</strong> {item.message}
                    {item.step_id ? ` [${item.step_id}]` : ""}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <details className="raw">
            <summary>查看 IR JSON</summary>
            <pre>{JSON.stringify(artifact?.ir ?? detail.compile?.ir ?? {}, null, 2)}</pre>
          </details>
        </section>
      </div>

      <div className="kpi-row">
        <Kpi label="修订版本" value={String(detail.revision_no)} hint={shortDigest(detail.source_digest)} />
        <Kpi label="步骤" value={String(steps.length)} />
        <Kpi label="诊断" value={String(diagnostics.length)} />
        <Kpi label="待审核" value={String(reviewItems.length)} />
        <Kpi label="AI 用量" value={String(artifact?.usage?.calls ?? 0)} hint={`tokens ${artifact?.usage?.prompt_tokens ?? 0}/${artifact?.usage?.completion_tokens ?? 0}`} />
      </div>
    </div>
  );
}
