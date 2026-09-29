/** Console shell: sign-in gate, project switcher and hash routing across the pages (§13.5). */

import { useEffect, useState } from "react";
import {
  Activity, Bot, ChevronDown, FileText, LayoutDashboard, LifeBuoy, LogIn, LogOut, Plus, Settings2,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { ApiFailure, api, token } from "./api";
import { AssistPage } from "./assist";
import { CasesPage } from "./cases";
import { OverviewPage } from "./overview";
import { ReportView } from "./report";
import { RunsPage } from "./runs";
import { SessionProvider, currentTenantHint, useSession } from "./session";
import { SettingsPage } from "./settings";
import { Busy, ErrorNote } from "./ui";

type View = "overview" | "cases" | "runs" | "report" | "assist" | "settings";

const NAV: { view: View; label: string; icon: LucideIcon }[] = [
  { view: "overview", label: "总览", icon: LayoutDashboard },
  { view: "cases", label: "测试用例", icon: FileText },
  { view: "runs", label: "运行", icon: Activity },
  { view: "assist", label: "人工接管", icon: LifeBuoy },
  { view: "settings", label: "设置", icon: Settings2 },
];

const TITLES: Record<View, string> = {
  overview: "总览",
  cases: "测试用例",
  runs: "运行",
  report: "执行报告",
  assist: "人工接管",
  settings: "项目设置",
};

function parseHash(): { view: View; id: string | null } {
  const parts = window.location.hash.replace(/^#\/?/, "").split("/");
  const raw = parts[0] || "overview";
  const known = NAV.some((item) => item.view === raw) || raw === "report";
  return { view: (known ? raw : "overview") as View, id: parts[1] ? decodeURIComponent(parts[1]) : null };
}

export default function App() {
  return (
    <SessionProvider>
      <Shell />
    </SessionProvider>
  );
}

function Shell() {
  const session = useSession();
  const [route, setRoute] = useState(parseHash);

  useEffect(() => {
    const sync = () => setRoute(parseHash());
    window.addEventListener("hashchange", sync);
    if (!window.location.hash) window.location.replace("#/overview");
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  function navigate(view: View, id?: string | null) {
    window.location.hash = `#/${view}${id ? `/${encodeURIComponent(id)}` : ""}`;
  }

  /** `report:<id>` is how the run detail asks for the report tab; everything else is a run id. */
  function openRun(raw: string) {
    if (raw.startsWith("report:")) navigate("report", raw.slice("report:".length));
    else navigate("runs", raw);
  }

  if (!session.signedIn) {
    return token() && !session.authError ? (
      <div className="auth-shell">
        <Busy label="正在校验身份…" />
      </div>
    ) : (
      <SignIn />
    );
  }

  const { project, projects, projectId, selectProject, whoami, capabilities, reloadProjects } = session;

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <Bot size={20} strokeWidth={1.7} />
          </span>
          <span className="brand-copy">
            <strong>ai-test-agent</strong>
            <small>{capabilities ? `${capabilities.api_version} · IR ${capabilities.ir_version}` : "TEST CONSOLE"}</small>
          </span>
        </div>

        <label className="workspace-switcher">
          <span className="workspace-copy">
            <strong>{project ? project.display_name : "未选择项目"}</strong>
            <small>{project ? project.name : `${projects.length} 个可见项目`}</small>
          </span>
          <select className="project-select" value={projectId} onChange={(event) => selectProject(event.target.value)}>
            {projects.map((item) => (
              <option key={item.id} value={item.id}>
                {item.display_name}
              </option>
            ))}
          </select>
          <ChevronDown size={14} />
        </label>

        <div className="nav-label">导航</div>
        <nav className="primary-nav" aria-label="主导航">
          {NAV.map((item) => (
            <button
              key={item.view}
              className={route.view === item.view || (route.view === "report" && item.view === "runs") ? "nav-item selected" : "nav-item"}
              onClick={() => navigate(item.view)}
            >
              <item.icon size={17} />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        <div className="nav-label">项目</div>
        <div className="project-list">
          {projects.map((item) => (
            <button
              key={item.id}
              className={item.id === projectId ? "project-item chosen" : "project-item"}
              onClick={() => {
                selectProject(item.id);
                navigate("overview");
              }}
            >
              <span className="project-copy">{item.display_name}</span>
              <small>{item.role}</small>
            </button>
          ))}
        </div>
        {whoami?.is_admin && <NewProjectButton onCreated={reloadProjects} />}
        {!projects.length && <p className="muted side-note">当前身份看不到任何项目，请先新建一个项目。</p>}

        <div className="sidebar-spacer" />
        <div className="profile-row">
          <span className="avatar">{(whoami?.display_name ?? "?").slice(0, 2).toUpperCase()}</span>
          <span className="profile-copy">
            <strong>{whoami?.display_name}</strong>
            <small>
              {whoami ? `${whoami.roles["*"] ?? whoami.roles[projectId] ?? "member"} · ${whoami.is_admin ? "平台管理员" : "成员"}` : ""}
            </small>
          </span>
          <button className="icon-button" aria-label="退出登录" onClick={session.signOut}>
            <LogOut size={16} />
          </button>
        </div>
      </aside>

      <section className="main-panel">
        <header className="topbar">
          <div className="breadcrumbs">
            <span>{project ? project.display_name : "—"}</span>
            <i>/</i>
            <strong>{TITLES[route.view]}</strong>
            {route.id && (
              <>
                <i>/</i>
                <code>{route.id.slice(0, 8)}</code>
              </>
            )}
          </div>
          <div className="top-actions">
            {capabilities && <span className="tag-pill">{capabilities.auth_mode === "dev" ? "dev 令牌" : "OIDC"}</span>}
            {capabilities && <span className="tag-pill">{capabilities.browsers.join(" / ")}</span>}
          </div>
        </header>

        <div className="work-area">
          {!projectId ? (
            <div className="card">
              <header>
                <strong>还没有可选项目</strong>
              </header>
              <p className="muted">项目是用例、环境与运行的边界。管理员可以在左侧新建项目，或联系管理员加入现有项目。</p>
            </div>
          ) : (
            <>
              {route.view === "overview" && <OverviewPage onOpenRun={openRun} onOpenAssist={() => navigate("assist")} />}
              {route.view === "cases" && <CasesPage onRun={openRun} />}
              {route.view === "runs" && <RunsPage focusId={route.id} onOpen={openRun} />}
              {route.view === "report" &&
                (route.id ? (
                  <ReportView key={route.id} executionId={route.id} onOpenRun={openRun} />
                ) : (
                  <div className="placeholder">从运行详情页点击「查看报告」进入。</div>
                ))}
              {route.view === "assist" && <AssistPage onOpenRun={openRun} />}
              {route.view === "settings" && <SettingsPage key={projectId} />}
            </>
          )}
        </div>
      </section>
    </main>
  );
}

function SignIn() {
  const { signIn, signingIn, authError, capabilities } = useSession();
  const [bearer, setBearer] = useState("");
  const [tenant, setTenant] = useState(currentTenantHint());

  return (
    <div className="auth-shell">
      <form
        className="card auth-card"
        onSubmit={async (event) => {
          event.preventDefault();
          await signIn(bearer.trim(), tenant);
        }}
      >
        <div className="brand">
          <span className="brand-mark">
            <Bot size={20} strokeWidth={1.7} />
          </span>
          <span className="brand-copy">
            <strong>ai-test-agent</strong>
            <small>Markdown DSL · Test IR · Playwright</small>
          </span>
        </div>
        <p className="muted">
          {capabilities
            ? `API ${capabilities.api_version} · 认证模式 ${capabilities.auth_mode}`
            : "正在读取服务器能力…"}
        </p>
        <ErrorNote error={authError} />
        <label className="field">
          <span>访问令牌</span>
          <input value={bearer} onChange={(event) => setBearer(event.target.value)} placeholder="dev 模式下的令牌，例如 dev-admin-token" autoFocus />
          <small>dev 模式下主体即令牌；OIDC 部署请改由身份提供商签发。</small>
        </label>
        <label className="field">
          <span>租户（可选）</span>
          <input value={tenant} onChange={(event) => setTenant(event.target.value)} placeholder="留空使用默认租户" />
        </label>
        <button className="button primary" type="submit" disabled={!bearer.trim() || signingIn}>
          <LogIn size={15} />
          {signingIn ? "正在校验…" : "进入控制台"}
        </button>
      </form>
    </div>
  );
}

function NewProjectButton({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState("");
  const { selectProject } = useSession();

  async function create() {
    setError("");
    try {
      const created = await api.post<{ id?: string; project_id?: string }>("/projects", {
        name: name.trim(),
        display_name: displayName.trim() || name.trim(),
      });
      const id = created.project_id ?? created.id ?? "";
      onCreated();
      if (id) selectProject(id);
      setOpen(false);
    } catch (failure) {
      setError(failure instanceof ApiFailure ? `${failure.code}: ${failure.message}` : String(failure));
    }
  }

  return (
    <>
      <button className="project-add" onClick={() => setOpen(true)}>
        <Plus size={15} />
        新建项目
      </button>
      {open && (
        <div className="modal-backdrop" onClick={() => setOpen(false)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <header>
              <strong>新建项目</strong>
              <button className="plain" onClick={() => setOpen(false)}>
                关闭
              </button>
            </header>
            <ErrorNote error={error} />
            <label className="field">
              <span>标识（slug）</span>
              <input value={name} onChange={(event) => setName(event.target.value)} placeholder="小写字母、数字、- 或 _" autoFocus />
              <small>创建后不可更改，用于 API 引用。</small>
            </label>
            <label className="field">
              <span>显示名</span>
              <input value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="可选，默认与标识相同" />
            </label>
            <div className="modal-actions">
              <button className="button" onClick={() => setOpen(false)}>
                取消
              </button>
              <button className="button primary" disabled={!name.trim()} onClick={create}>
                创建
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
