/** Settings: environments, write-only secrets, project RBAC, and the audit trail (§14.1, §14.3). */

import { useMemo, useState } from "react";
import { KeyRound, Lock, Plus, Save, ShieldCheck, Trash2, UserPlus } from "lucide-react";

import { ApiFailure, api, listQuery } from "./api";
import { useSession } from "./session";
import type { AuditRow, Environment, EnvironmentRevision, Grant, Member, SecretRow, UserRow } from "./types";
import { Busy, ErrorNote, Pill, ReloadButton, fmtTime, shortDigest, statusTone, useAsync, usePaged } from "./ui";

type Tab = "environments" | "secrets" | "members" | "audit" | "users";

const TABS: { id: Tab; label: string; permission: string | null }[] = [
  { id: "environments", label: "环境", permission: null },
  { id: "secrets", label: "密钥", permission: "secret_manage" },
  { id: "members", label: "成员与授权", permission: null },
  { id: "audit", label: "审计", permission: "audit_read" },
  { id: "users", label: "用户", permission: null },
];

const BLANK_CONFIG = {
  base_url: "https://example.com",
  allowed_domains: ["example.com"],
  allowed_protocols: ["https"],
  browsers: ["chromium"],
  viewport: { width: 1280, height: 720 },
  evidence: { mode: "NORMAL", trace: "off", video: "off" },
  variables: {},
};

export function SettingsPage() {
  const { projectId, can, whoami } = useSession();
  const [tab, setTab] = useState<Tab>("environments");

  const visible = TABS.filter((item) => {
    if (item.id === "users") return Boolean(whoami?.can_manage_users);
    if (!item.permission) return true;
    return can(item.permission);
  });

  return (
    <div className="pane">
      <div className="pane-head">
        <h2>项目设置</h2>
        <small className="muted">项目 {projectId ? projectId.slice(0, 8) : "未选择"} · 角色与授权由服务端判定</small>
      </div>
      {!projectId ? (
        <div className="empty">请先在左侧选择一个项目。</div>
      ) : (
        <>
          <div className="tabs">
            {visible.map((item) => (
              <button key={item.id} className={tab === item.id ? "tab on" : "tab"} onClick={() => setTab(item.id)}>
                {item.label}
              </button>
            ))}
          </div>
          {tab === "environments" && <EnvironmentsPanel projectId={projectId} />}
          {tab === "secrets" && <SecretsPanel projectId={projectId} />}
          {tab === "members" && <MembersPanel projectId={projectId} />}
          {tab === "audit" && <AuditPanel projectId={projectId} />}
          {tab === "users" && <UsersPanel />}
        </>
      )}
    </div>
  );
}

function EnvironmentsPanel({ projectId }: { projectId: string }) {
  const { can, capabilities } = useSession();
  const state = useAsync<{ items: Environment[] }>(() => api.get(`/projects/${projectId}/environments`), [projectId]);
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const revisions = useAsync<{ items: EnvironmentRevision[] }>(
    () => (selected ? api.get(`/environments/${selected}/revisions`, { limit: 20 }) : Promise.resolve({ items: [] })),
    [selected],
  );

  return (
    <section className="card">
      <header>
        <strong>环境</strong>
        <div className="card-actions">
          <ReloadButton onClick={state.reload} disabled={state.loading} />
          {can("env_manage") && (
            <button className="button primary" onClick={() => setOpen(true)}>
              <Plus size={14} />
              新建环境
            </button>
          )}
        </div>
      </header>
      <ErrorNote error={state.error || error} />
      {notice && <div className="callout ok"><Save size={15} /><span>{notice}</span></div>}
      {state.loading && !state.data ? (
        <Busy />
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>入口</th>
              <th>允许域名</th>
              <th>浏览器</th>
              <th>视口</th>
              <th>证据</th>
              <th>版本</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(state.data?.items ?? []).map((item) => (
              <tr key={item.environment_id}>
                <td>
                  <strong>{item.name}</strong>
                  {item.archived_at && <Pill tone="muted">归档</Pill>}
                </td>
                <td className="clip">{item.current_revision?.config.base_url ?? "—"}</td>
                <td className="clip">{(item.current_revision?.config.allowed_domains ?? []).join(", ")}</td>
                <td>{(item.current_revision?.config.browsers ?? []).join(", ")}</td>
                <td>
                  {item.current_revision?.config.viewport.width ?? "—"}×{item.current_revision?.config.viewport.height ?? "—"}
                </td>
                <td>{item.current_revision?.config.evidence.mode ?? "—"}</td>
                <td>
                  <Pill tone={statusTone(item.current_revision ? "READY" : "PENDING")}>
                    v{item.current_revision?.version ?? 0} · {shortDigest(item.current_revision?.digest)}
                  </Pill>
                </td>
                <td className="row-actions">
                  <button className="plain" onClick={() => setSelected(item.environment_id)}>
                    版本
                  </button>
                  {can("env_manage") && (
                    <button
                      className="plain"
                      title="归档环境"
                      onClick={async () => {
                        setError("");
                        try {
                          await api.del(`/environments/${item.environment_id}`);
                          setNotice(`已归档环境 ${item.name}`);
                          state.reload();
                        } catch (error) {
                          setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                        }
                      }}
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {selected && (
        <div className="subpanel">
          <header>
            <strong>环境版本历史</strong>
            <span className="muted">历史版本不可改写，运行始终绑定到一个具体版本</span>
          </header>
          <ErrorNote error={revisions.error} />
          {revisions.data?.items.map((revision) => (
            <div className="revision" key={revision.environment_revision_id}>
              <Pill tone={revision.current ? "ok" : "muted"}>{revision.current ? "当前" : `v${revision.version}`}</Pill>
              <span>{shortDigest(revision.digest)}</span>
              <span>{revision.config.base_url}</span>
              <span>{fmtTime(revision.created_at)}</span>
              <details className="raw">
                <summary>配置</summary>
                <pre>{JSON.stringify({ config: revision.config, secret_bindings: revision.secret_bindings }, null, 2)}</pre>
              </details>
            </div>
          ))}
        </div>
      )}
      {open && (
        <EnvironmentModal
          browsers={capabilities?.browsers ?? ["chromium"]}
          onClose={() => setOpen(false)}
          onSubmit={async (name, config, bindings) => {
            setError("");
            try {
              const created = await api.post<{ environment_id: string; etag?: string }>(`/projects/${projectId}/environments`, { name });
              await api.post(
                `/environments/${created.environment_id}/revisions`,
                { config, secret_bindings: bindings },
                { "If-Match": created.etag ?? "*", "Idempotency-Key": `env:${created.environment_id}:${Date.now().toString(36)}` },
              );
              setOpen(false);
              setNotice(`已创建环境 ${name}`);
              state.reload();
            } catch (error) {
              setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
              throw error;
            }
          }}
        />
      )}
    </section>
  );
}

function EnvironmentModal({
  browsers,
  onClose,
  onSubmit,
}: {
  browsers: string[];
  onClose: () => void;
  onSubmit: (name: string, config: Record<string, unknown>, bindings: Record<string, unknown>) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [config, setConfig] = useState(JSON.stringify(BLANK_CONFIG, null, 2));
  const [bindings, setBindings] = useState("{}");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal tall" onClick={(event) => event.stopPropagation()}>
        <h3>新建环境</h3>
        <p className="muted">配置字段严格校验：只有 base_url、allowed_domains、allowed_protocols、browsers、viewport、evidence、variables 会被接受。</p>
        <label className="field">
          <span>名称</span>
          <input value={name} onChange={(event) => setName(event.target.value)} placeholder="staging" />
        </label>
        <label className="field">
          <span>配置 JSON</span>
          <textarea rows={12} value={config} onChange={(event) => setConfig(event.target.value)} />
        </label>
        <label className="field">
          <span>密钥绑定 {"${'secrets.<name>:<version>'}"}</span>
          <textarea rows={3} value={bindings} onChange={(event) => setBindings(event.target.value)} placeholder='{"password": {"secret": "password", "version": 1}}' />
        </label>
        <small className="muted">可用浏览器通道：{browsers.join(", ")}</small>
        <ErrorNote error={error} />
        <div className="modal-actions">
          <button className="button" onClick={onClose}>
            取消
          </button>
          <button
            className="button primary"
            disabled={busy || !name.trim()}
            onClick={async () => {
              setError("");
              let parsedConfig: Record<string, unknown>;
              let parsedBindings: Record<string, unknown>;
              try {
                parsedConfig = JSON.parse(config) as Record<string, unknown>;
                parsedBindings = JSON.parse(bindings) as Record<string, unknown>;
              } catch {
                setError("配置与密钥绑定必须是合法的 JSON 对象。");
                return;
              }
              setBusy(true);
              try {
                await onSubmit(name.trim(), parsedConfig, parsedBindings);
              } catch (error) {
                setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "创建中…" : "创建并发布"}
          </button>
        </div>
      </div>
    </div>
  );
}

function SecretsPanel({ projectId }: { projectId: string }) {
  const state = useAsync<{ items: SecretRow[] }>(() => api.get(`/projects/${projectId}/secrets`), [projectId]);
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  return (
    <section className="card">
      <header>
        <strong>密钥</strong>
        <div className="card-actions">
          <ReloadButton onClick={state.reload} disabled={state.loading} />
        </div>
      </header>
      <p className="muted">密钥只写不读：列表里只有名称、版本与状态，明文永远不会回来（§14.3）。</p>
      <ErrorNote error={state.error || error} />
      {notice && <div className="callout ok"><KeyRound size={15} /><span>{notice}</span></div>}
      <div className="inline">
        <input value={name} onChange={(event) => setName(event.target.value)} placeholder="逻辑名称，如 password" />
        <input type="password" value={value} onChange={(event) => setValue(event.target.value)} placeholder="值（保存后不可再读取）" />
        <button
          className="button primary"
          disabled={!name.trim() || !value}
          onClick={async () => {
            setError("");
            try {
              await api.post(`/projects/${projectId}/secrets`, { logical_name: name.trim(), value });
              setNotice(`已写入 ${name.trim()} 的新版本`);
              setName("");
              setValue("");
              state.reload();
            } catch (error) {
              setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
            }
          }}
        >
          <Save size={14} />
          写入
        </button>
      </div>
      {state.loading && !state.data ? (
        <Busy />
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>版本</th>
              <th>提供方</th>
              <th>状态</th>
              <th>创建时间</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {(state.data?.items ?? []).map((item) => (
              <tr key={`${item.logical_name}-${item.version}`}>
                <td>{item.logical_name}</td>
                <td>v{item.version}</td>
                <td>{String(item.provider ?? "—")}</td>
                <td>
                  <Pill tone={statusTone(String(item.status ?? ""))}>{String(item.status ?? "—")}</Pill>
                </td>
                <td>{fmtTime(String(item.created_at ?? ""))}</td>
                <td className="row-actions">
                  <button
                    className="plain"
                    title="吊销该版本"
                    onClick={async () => {
                      setError("");
                      try {
                        await api.del(`/projects/${projectId}/secrets/${item.logical_name}/${item.version}`);
                        setNotice(`已吊销 ${item.logical_name} v${item.version}`);
                        state.reload();
                      } catch (error) {
                        setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                      }
                    }}
                  >
                    <Lock size={14} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {(state.data?.items ?? []).length === 0 && !state.loading && <div className="empty">还没有写入任何密钥。</div>}
    </section>
  );
}

function MembersPanel({ projectId }: { projectId: string }) {
  const { whoami, can } = useSession();
  const state = useAsync<{ members: Member[]; grants: Grant[] }>(() => api.get(`/projects/${projectId}/members`), [projectId]);
  const [userId, setUserId] = useState("");
  const [role, setRole] = useState("engineer");
  const [permission, setPermission] = useState("human_control");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const manageable = Boolean(whoami?.is_admin) || can("project_manage");

  const users = useAsync<{ items: UserRow[] }>(() => (whoami?.can_manage_users ? api.get("/admin/users", { limit: 100 }) : Promise.resolve({ items: [] })), [whoami]);

  return (
    <section className="card">
      <header>
        <strong>成员与授权</strong>
        <div className="card-actions">
          <ReloadButton onClick={state.reload} disabled={state.loading} />
        </div>
      </header>
      <ErrorNote error={state.error || error} />
      {notice && <div className="callout ok"><ShieldCheck size={15} /><span>{notice}</span></div>}
      <table className="table">
        <thead>
          <tr>
            <th>成员</th>
            <th>主体</th>
            <th>角色</th>
            <th>状态</th>
            <th>加入时间</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(state.data?.members ?? []).map((member) => (
            <tr key={member.user_id}>
              <td>{member.display_name}</td>
              <td className="clip">{member.subject}</td>
              <td>
                <select
                  className="mini"
                  value={member.role}
                  disabled={!manageable}
                  onChange={async (event) => {
                    setError("");
                    try {
                      await api.patch(`/projects/${projectId}/members/${member.user_id}`, { role: event.target.value });
                      setNotice(`已把 ${member.display_name} 调整为 ${event.target.value}`);
                      state.reload();
                    } catch (error) {
                      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                    }
                  }}
                >
                  {["admin", "engineer", "viewer"].map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <Pill tone={statusTone(member.status)}>{member.status}</Pill>
              </td>
              <td>{fmtTime(member.joined_at)}</td>
              <td className="row-actions">
                <button
                  className="plain"
                  disabled={!manageable}
                  onClick={async () => {
                    setError("");
                    try {
                      await api.del(`/projects/${projectId}/members/${member.user_id}`);
                      setNotice(`已移出 ${member.display_name}`);
                      state.reload();
                    } catch (error) {
                      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                    }
                  }}
                >
                  <Trash2 size={14} />
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {manageable && (
        <div className="inline">
          <select value={userId} onChange={(event) => setUserId(event.target.value)}>
            <option value="">选择用户</option>
            {(users.data?.items ?? []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name}（{item.subject}）
              </option>
            ))}
          </select>
          <select className="mini" value={role} onChange={(event) => setRole(event.target.value)}>
            {["admin", "engineer", "viewer"].map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <button
            className="button"
            disabled={!userId}
            onClick={async () => {
              setError("");
              try {
                await api.post(`/projects/${projectId}/members`, { user_id: userId, role });
                setNotice("成员已添加");
                state.reload();
              } catch (error) {
                setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
              }
            }}
          >
            <UserPlus size={14} />
            添加成员
          </button>
        </div>
      )}
      <header className="sub">
        <strong>项目级授权</strong>
        <span className="muted">人工接管与敏感证据读取需要显式授权，角色本身不给这两项</span>
      </header>
      {(state.data?.grants ?? []).length === 0 && <div className="empty">当前没有显式授权。</div>}
      <ul className="rows tight">
        {(state.data?.grants ?? []).map((grant) => (
          <li className="row" key={`${grant.user_id}-${grant.permission}`}>
            <div className="row-main">
              <strong>{grant.permission}</strong>
              <small>
                {(state.data?.members ?? []).find((item) => item.user_id === grant.user_id)?.display_name ?? grant.user_id} · {grant.reason ?? "—"} ·{" "}
                {grant.expires_at ? `到期 ${fmtTime(grant.expires_at)}` : "长期"}
              </small>
            </div>
            <div className="row-side">
              <button
                className="plain"
                disabled={!manageable}
                onClick={async () => {
                  setError("");
                  try {
                    await api.del(`/projects/${projectId}/members/${grant.user_id}/grants/${grant.permission}`);
                    setNotice(`已撤销 ${grant.permission}`);
                    state.reload();
                  } catch (error) {
                    setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                  }
                }}
              >
                撤销
              </button>
            </div>
          </li>
        ))}
      </ul>
      {manageable && (
        <div className="inline">
          <select value={userId} onChange={(event) => setUserId(event.target.value)}>
            <option value="">选择用户</option>
            {(state.data?.members ?? []).map((item) => (
              <option key={item.user_id} value={item.user_id}>
                {item.display_name}
              </option>
            ))}
          </select>
          <select className="mini" value={permission} onChange={(event) => setPermission(event.target.value)}>
            {["human_control", "sensitive_artifact_read"].map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
          <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="授权理由" />
          <button
            className="button"
            disabled={!userId}
            onClick={async () => {
              setError("");
              try {
                await api.put(`/projects/${projectId}/members/${userId}/grants/${permission}`, { permission, reason: reason || undefined });
                setNotice(`已授予 ${permission}`);
                state.reload();
              } catch (error) {
                setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
              }
            }}
          >
            <ShieldCheck size={14} />
            授予
          </button>
        </div>
      )}
    </section>
  );
}

function AuditPanel({ projectId }: { projectId: string }) {
  const [operation, setOperation] = useState("");
  const fetcher = useMemo(
    () => (cursor: string | null) =>
      api.get<{ items: AuditRow[]; next_cursor?: string | null; total?: number }>(
        `/projects/${projectId}/audit-logs`,
        listQuery({ limit: 25, cursor }, { operation: operation || undefined }),
      ),
    [projectId, operation],
  );
  const list = usePaged<AuditRow>(fetcher);

  return (
    <section className="card">
      <header>
        <strong>审计</strong>
        <div className="card-actions">
          <input className="mini" value={operation} onChange={(event) => setOperation(event.target.value)} placeholder="按操作过滤，如 human.claim" />
          <ReloadButton onClick={list.reload} disabled={list.loading} />
        </div>
      </header>
      <ErrorNote error={list.error} />
      {list.loading && <Busy />}
      <table className="table">
        <thead>
          <tr>
            <th>时间</th>
            <th>操作者</th>
            <th>操作</th>
            <th>资源</th>
            <th>请求号</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          {list.items.map((row) => (
            <tr key={row.audit_id}>
              <td>{fmtTime(row.created_at)}</td>
              <td>{row.actor_name ?? row.actor_id.slice(0, 8)}</td>
              <td>
                <Pill>{row.operation}</Pill>
              </td>
              <td className="clip">
                {row.resource_type} · {shortDigest(row.resource_id)}
              </td>
              <td className="clip">{row.request_id ?? "—"}</td>
              <td className="clip">
                <details className="raw inline-details">
                  <summary>查看</summary>
                  <pre>{JSON.stringify(row.detail, null, 2)}</pre>
                </details>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
  );
}

function UsersPanel() {
  const state = useAsync<{ items: UserRow[] }>(() => api.get("/admin/users", { limit: 100 }), []);
  const [issuer, setIssuer] = useState("local-dev");
  const [subject, setSubject] = useState("");
  const [role, setRole] = useState("engineer");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  return (
    <section className="card">
      <header>
        <strong>用户</strong>
        <div className="card-actions">
          <ReloadButton onClick={state.reload} disabled={state.loading} />
        </div>
      </header>
      <ErrorNote error={state.error || error} />
      {notice && <div className="callout ok"><UserPlus size={15} /><span>{notice}</span></div>}
      <table className="table">
        <thead>
          <tr>
            <th>显示名</th>
            <th>主体</th>
            <th>签发方</th>
            <th>租户角色</th>
            <th>状态</th>
            <th>创建</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(state.data?.items ?? []).map((item) => (
            <tr key={item.id}>
              <td>{item.display_name}</td>
              <td className="clip">{item.subject}</td>
              <td>{item.issuer}</td>
              <td>{item.tenant_role}</td>
              <td>
                <Pill tone={statusTone(item.status)}>{item.status}</Pill>
              </td>
              <td>{fmtTime(item.created_at)}</td>
              <td className="row-actions">
                <button
                  className="plain"
                  disabled={item.status === "DISABLED"}
                  onClick={async () => {
                    setError("");
                    try {
                      await api.patch(`/admin/users/${item.id}`, { status: "DISABLED" });
                      setNotice(`${item.display_name} 已停用`);
                      state.reload();
                    } catch (error) {
                      setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
                    }
                  }}
                >
                  停用
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <header className="sub">
        <strong>开通用户</strong>
        <span className="muted">dev 模式下主体即令牌；OIDC 部署中这里只是缓存，身份仍来自令牌</span>
      </header>
      <div className="inline">
        <input className="mini" value={issuer} onChange={(event) => setIssuer(event.target.value)} placeholder="issuer" />
        <input value={subject} onChange={(event) => setSubject(event.target.value)} placeholder="subject，例如 qa-01" />
        <select className="mini" value={role} onChange={(event) => setRole(event.target.value)}>
          {["admin", "engineer", "viewer"].map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
        <button
          className="button primary"
          disabled={!subject.trim()}
          onClick={async () => {
            setError("");
            try {
              await api.post("/admin/users", { issuer, subject: subject.trim(), role });
              setNotice(`已开通 ${subject.trim()}`);
              setSubject("");
              state.reload();
            } catch (error) {
              setError(error instanceof ApiFailure ? `${error.code}: ${error.message}` : String(error));
            }
          }}
        >
          <UserPlus size={14} />
          开通
        </button>
      </div>
    </section>
  );
}
