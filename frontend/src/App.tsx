import { useEffect, useMemo, useState } from "react";
import {
  Activity, ArrowRight, Bell, BookOpen, Bot, Check, ChevronDown, CircleHelp,
  Clock3, Code2, Command, FileCode2, FileText, FolderKanban, GitBranch, History,
  Layers3, LockKeyhole, MoreHorizontal, PanelLeftClose, Play, Plus, Search,
  Settings2, ShieldCheck, Sparkles, TerminalSquare, Upload, X, Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

type IRStep = {
  id: string;
  action: string;
  target?: { description?: string };
  source: { start_line: number; end_line: number };
};
type ParseResult = {
  status: string;
  ir: { steps: IRStep[]; [key: string]: unknown };
  diagnostics: { code: string; message: string; source_range?: { start_line: number } }[];
};

const SAMPLE = [
  '---',
  'dsl_version: "1.0"',
  'tags: [smoke, login]',
  'variables:',
  '  username:',
  '    type: string',
  '    required: true',
  '---',
  '# 登录验证',
  '',
  '## Step 1',
  '```yaml',
  'action: open',
  'url: "https://example.com/login"',
  '```',
  '',
  '## Step 2',
  '```yaml',
  'action: input',
  'target:',
  '  description: 用户名输入框',
  '  type: input',
  '  role: textbox',
  '  name: 用户名',
  'value: "${vars.username}"',
  '```',
  '',
  '## Step 3',
  '```yaml',
  'action: click',
  'target:',
  '  description: 登录按钮',
  '  type: button',
  '  role: button',
  '  name: 登录',
  '```',
  '',
  '## Step 4',
  '```yaml',
  'action: assert',
  'condition:',
  '  kind: page_contains',
  '  expected: 欢迎回来',
  '```',
].join('\n');

const NAV: { label: string; icon: LucideIcon }[] = [
  { label: "总览", icon: Layers3 },
  { label: "测试用例", icon: FileText },
  { label: "测试运行", icon: Activity },
  { label: "执行历史", icon: History },
];

function IconButton({ icon: Icon, label }: { icon: LucideIcon; label: string }) {
  return <button aria-label={label} className="icon-button"><Icon size={17} strokeWidth={1.8} /></button>;
}

export default function App() {
  const [markdown, setMarkdown] = useState(() => localStorage.getItem("ai-agent-case") ?? SAMPLE);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [compileError, setCompileError] = useState("");
  const [compiling, setCompiling] = useState(false);
  const [activeNav, setActiveNav] = useState("测试用例");
  const [toast, setToast] = useState("");
  const title = useMemo(() => markdown.match(/^#\s+(.+)$/m)?.[1]?.trim() || "未命名测试", [markdown]);
  const stepCount = useMemo(() => (markdown.match(/^##\s+Step\s+\d+\s*$/gim) ?? []).length, [markdown]);

  useEffect(() => { localStorage.setItem("ai-agent-case", markdown); }, [markdown]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  async function compile() {
    setCompiling(true);
    setCompileError("");
    try {
      const response = await fetch("/api/v1/cases/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ markdown }),
      });
      const body = await response.json();
      if (!response.ok) {
        const detail = body.detail;
        setResult(null);
        setCompileError(typeof detail === "string" ? detail : detail?.message ?? "解析失败，请检查 Markdown DSL。");
      } else {
        setResult(body as ParseResult);
        setToast("Test IR 已生成");
      }
    } catch {
      setCompileError("无法连接 API。请先启动后端，再重试解析。");
    } finally {
      setCompiling(false);
    }
  }

  function uploadMarkdown(file?: File) {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".md")) { setToast("请选择 Markdown 文件（.md）"); return; }
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") {
        setMarkdown(reader.result);
        setResult(null);
        setToast(`已载入 ${file.name}`);
      }
    };
    reader.readAsText(file, "UTF-8");
  }

  function downloadMarkdown() {
    const file = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(file);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${title}.md`;
    link.click();
    URL.revokeObjectURL(url);
    setToast("用例草稿已下载");
  }

  function chooseUpload() {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".md,text/markdown";
    input.onchange = () => uploadMarkdown(input.files?.[0]);
    input.click();
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <a className="brand" href="#home" aria-label="AI Test Agent 首页">
          <span className="brand-mark"><Bot size={21} strokeWidth={1.7} /></span>
          <span className="brand-copy"><strong>testpilot</strong><small>AI TEST AGENT</small></span>
          <PanelLeftClose className="sidebar-collapse" size={17} />
        </a>
        <button className="workspace-switcher">
          <span className="workspace-icon">N</span>
          <span className="workspace-copy"><strong>Nebula Studio</strong><small>个人工作空间</small></span>
          <ChevronDown size={15} />
        </button>
        <div className="nav-label">工作区</div>
        <nav className="primary-nav" aria-label="主导航">
          {NAV.map(({ label, icon: Icon }) => (
            <button onClick={() => setActiveNav(label)} className={`nav-item ${activeNav === label ? "selected" : ""}`} key={label}>
              <Icon size={17} /><span>{label}</span>{label === "执行历史" && <span className="nav-count">12</span>}
            </button>
          ))}
        </nav>
        <div className="side-section-title"><span>项目</span><button aria-label="添加项目"><Plus size={15} /></button></div>
        <button className="project-item chosen"><span className="project-dot mint" /><span>Web 商城</span><MoreHorizontal size={17} /></button>
        <button className="project-item"><span className="project-dot violet" /><span>会员中心</span></button>
        <button className="project-item"><span className="project-dot amber" /><span>内容管理平台</span></button>
        <button className="project-add"><Plus size={15} />新建项目</button>
        <div className="sidebar-spacer" />
        <div className="usage-card"><div className="usage-title"><span>本月 AI 用量</span><Sparkles size={14} /></div><div className="usage-meter"><span /></div><div className="usage-caption"><span>12,480 tokens</span><span>额度 40%</span></div></div>
        <button className="nav-item bottom-link"><Settings2 size={17} /><span>设置</span></button>
        <button className="nav-item bottom-link"><CircleHelp size={17} /><span>帮助中心</span><ArrowRight size={14} className="link-arrow" /></button>
        <button className="profile-row"><span className="avatar">LW</span><span className="profile-copy"><strong>Lin Wei</strong><small>测试工程师</small></span><MoreHorizontal size={17} /></button>
      </aside>

      <section className="main-panel">
        <header className="topbar">
          <div className="breadcrumbs"><button>Web 商城</button><span>/</span><button>测试用例</button><span>/</span><strong>{title}</strong><span className="draft-pill">草稿</span></div>
          <div className="top-actions"><div className="shortcut"><Command size={13} /> K</div><IconButton icon={Search} label="搜索" /><span className="top-divider" /><button className="help-link"><CircleHelp size={16} />帮助文档</button><button className="notification" aria-label="通知"><Bell size={17} /><i /></button><span className="avatar top-avatar">LW</span></div>
        </header>

        <div className="work-area">
          <div className="page-heading">
            <div><div className="eyebrow"><FolderKanban size={13} /> WEB 商城 <span>·</span> 登录流程</div><h1>{title}</h1><p>使用 Markdown DSL 定义页面步骤，并预览校验后的 Test IR。</p></div>
            <div className="heading-actions"><button className="button secondary" onClick={() => { setMarkdown(SAMPLE); setResult(null); setToast("已恢复示例用例"); }}><History size={15} />重置示例</button><button className="button secondary" onClick={downloadMarkdown}><Check size={15} />下载草稿</button><button className="button primary" disabled={compiling} onClick={compile}><Sparkles size={15} />{compiling ? "正在解析…" : "生成 Test IR"}<ArrowRight size={15} /></button></div>
          </div>
          <div className="metadata-row"><span className="status-live"><i /> 草稿</span><span className="metadata-separator">·</span><span><GitBranch size={14} /> v1.0 草稿</span><span className="metadata-separator">·</span><span><Clock3 size={14} />刚刚保存</span><span className="metadata-separator">·</span>{["Web", "Chromium", "Markdown DSL"].map((item) => <span className="tag-pill" key={item}>{item}</span>)}</div>

          <section className="editor-card">
            <div className="editor-toolbar"><div className="file-tab"><FileCode2 size={15} /><strong>login-flow.md</strong><i /></div><div className="editor-tools"><span className="markdown-label"><Code2 size={13} /> Markdown</span><span className="toolbar-divider" /><button onClick={chooseUpload}><Upload size={14} />导入 .md</button><IconButton icon={MoreHorizontal} label="更多编辑器选项" /></div></div>
            <div className="editor-workspace">
              <div className="editor-code"><div className="code-lines" aria-hidden="true">{markdown.split("\n").map((_, index) => <span key={index}>{String(index + 1).padStart(2, "0")}</span>)}</div><textarea aria-label="Markdown 测试用例编辑器" spellCheck={false} value={markdown} onChange={(event) => { setMarkdown(event.target.value); setResult(null); }} /></div>
              <aside className="editor-helper">
                <div className="helper-topline"><span className="helper-orbit"><Sparkles size={15} /></span><span>测试场景助手</span><span className="ai-ready"><i />解析器就绪</span></div>
                <h3>把想测的场景写具体</h3><p>当前 MVP 每一步使用一个 YAML 动作块；AI 自然语言编译待接入。</p>
                <div className="helper-suggestion"><span className="suggestion-icon"><Zap size={14} /></span><span>识别页面上的关键交互元素</span><ArrowRight size={14} /></div>
                <div className="helper-suggestion"><span className="suggestion-icon violet-bg"><ShieldCheck size={14} /></span><span>生成登录成功后的页面断言</span><ArrowRight size={14} /></div>
                <div className="helper-tips"><div><Check size={14} /><span>尽量用具体的按钮或字段名称</span></div><div><LockKeyhole size={14} /><span>密码请用 <code>${"${secrets.password}"}</code> 引用</span></div></div>
                <button className="ai-provider"><span className="provider-led" /><span>AI 提供商</span><span>待配置</span><ChevronDown size={13} /></button>
                <div className="upload-hint"><Upload size={14} /><span>也可以拖入 .md 文件</span><input aria-label="上传 Markdown 文件" type="file" accept=".md,text/markdown" onChange={(event) => uploadMarkdown(event.target.files?.[0])} /></div>
              </aside>
            </div>
            <footer className="editor-footer"><span><span className="saved-dot" />已保存到本地草稿</span><span>{stepCount} 个步骤 <i>·</i> {markdown.length} 字符 <i>·</i> UTF-8</span></footer>
          </section>

          <section className="ir-panel">
            <div className="ir-heading"><div className="ir-heading-icon"><Code2 size={17} /></div><div className="ir-title"><h2>Test IR 预览</h2><p>结构化执行合同 · IR Schema v1.0</p></div>{result && <span className={`compile-status ${result.status === "SUCCEEDED" ? "success" : "review"}`}><i />{result.status === "SUCCEEDED" ? "结构校验通过" : "等待审核"}</span>}<button className="expand-button" aria-label="Test IR 状态"><ArrowRight size={15} /></button></div>
            {compileError && <div className="compile-error"><X size={16} /><div><strong>需要检查用例</strong><p>{compileError}</p></div><span>解析诊断</span></div>}
            {result ? <div className="ir-result"><div className="steps-strip">{result.ir.steps.map((step, index) => <div className="step-chip" key={step.id}><span className="step-index">{String(index + 1).padStart(2, "0")}</span><span><strong>{step.action}</strong><small>{step.target?.description ?? `第 ${index + 1} 步`}</small></span>{index < result.ir.steps.length - 1 && <ArrowRight className="step-arrow" size={14} />}</div>)}</div><pre className="json-preview"><code>{JSON.stringify(result.ir, null, 2)}</code></pre></div> : <div className="ir-empty"><div className="ir-empty-art"><span className="art-square one"><Code2 size={16} /></span><span className="art-square two"><TerminalSquare size={15} /></span><span className="art-square three"><Sparkles size={16} /></span><span className="art-orbit orbit-one" /><span className="art-orbit orbit-two" /></div><div><strong>你的执行结构将在这里呈现</strong><p>检查用例步骤，看看它们如何变成可验证的浏览器动作。</p><button onClick={compile}><Play size={13} />解析并预览 IR</button></div><span className="step-counter"><span>{String(stepCount).padStart(2, "0")}</span><small>STEPS</small></span></div>}
          </section>
          <footer className="bottom-note"><span><ShieldCheck size={14} />测试内容仅保存在当前浏览器</span><span><BookOpen size={14} />查看 DSL 文档 <ArrowRight size={13} /></span></footer>
        </div>
      </section>
      {toast && <div className="toast"><Check size={16} />{toast}</div>}
    </main>
  );
}
