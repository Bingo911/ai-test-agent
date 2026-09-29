# AI Test Agent

**用 Markdown 编写 Web 测试：每个步骤先编译成可校验的 Test IR，再由 Playwright 执行器运行，结论、证据与失败分析分开呈现。**

平台由四层组成，任何一层都能单独停用：Markdown DSL 与确定性编译器、Test IR 与执行合同、Playwright Worker 与证据采集、控制台与报告。AI 是可选项——关闭后结构化编译、常规定位与执行完全不受影响，只是没有模型解释。

## 快速启动（单进程）

开发形态不需要 Postgres、Redis 或对象存储：SQLite + 进程内队列 + 本地对象存储即可跑通全部路径。

```bash
cp .env.example .env                   # 默认值即可启动；秘密与模型凭据请按环境注入
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m playwright install --with-deps chromium   # --with-deps 要 root；本机已有系统依赖时去掉它，或用 PLAYWRIGHT_BROWSERS_PATH 指到仓库内目录

python -m backend.app.main             # API + 控制回路，默认 127.0.0.1:8000
cd frontend && npm ci && npm run dev   # 控制台 http://localhost:5173
```

首次启动会建表并生成一个开发用工作区；控制台登录框填 `DEV_ADMIN_TOKEN`（默认 `dev-admin-token`）。API 文档 `/api/v1/docs`，健康检查 `/api/v1/health`，能力矩阵 `/api/v1/capabilities`。

## 用 Compose 起完整拓扑

`compose.yaml` 按 §15.1 拆出 PostgreSQL、Redis、API、执行 Worker、后台 Worker 与静态托管的控制台：

```bash
docker compose up --build
```

镜像是执行器镜像：`pip install .` 之后紧接 `playwright install --with-deps chromium`，API 与两类 Worker 共用同一个镜像、只换启动命令；安装版本按 `pyproject.toml` 的范围解析，需要精确复现就照 `uv.lock` 装（`uv sync --frozen`）。拆进程时把 `APP_ENV` 设为 `production`（此时 `auth_mode` 必须是 `oidc`，开发令牌会被启动校验拒绝），控制回路交给单实例的 `python -m app.orchestrator.runtime`，API 只做服务。

## 一份用例长什么样

````markdown
---
dsl_version: "1.0"
tags: [smoke]
variables:
  username:
    type: string
    required: true
---
# 登录冒烟测试

## Step 1
```yaml
action: open
url: "${env.base_url}/index.html"
```

## Step 2
```yaml
action: input
target:
  description: 用户名输入框
  type: input
  css: 'input[name="username"]'
value: "${vars.username}"
```

## Step 3
```yaml
action: input
target:
  description: 密码输入框
  type: input
  css: 'input[name="password"]'
value: "${secrets.login_password}"
```
````

动作集合：`open`、`click`、`input`、`clear`、`upload`、`wait`、`assert`、`screenshot`。定位可用 `css`、`role + name`、`text`、`xpath`，运行时按 `css → role → text → xpath` 逐层降级，同一策略内先试用例里写明的候选、再试元素记忆里已验证的候选；只有在确定性候选全部落空且步骤剩余时间够用时才走视觉兜底。命中的策略与每一次失败尝试都写进报告的 `locator_attempts`。`${secrets.*}` 只在执行瞬间解引用，IR、日志与证据里只留密钥名。自然语言步骤或含糊目标不会被猜测执行：保存只排队一次确定性编译，编译把该产物判为 `FAILED` 并留下带行号的诊断（`GET /compilations/{id}` 与控制台用例页都能读到），不可运行的制品也无法发起执行；需要人工确认的产物必须先 `confirm`。AI 补充是显式选项，`use_ai` 默认关闭，保存动作本身从不把用例文本发给模型。

## 运行链路

```
保存 Markdown → case_revision（摘要定版）→ 编译（确定性，可选 AI 补充）→ compile_artifact
  → POST /executions → scheduler 预留槽位 → outbox 派发 → execution worker 持有浏览器逐步执行
  → 证据上传（截图/DOM/Console/Network/Trace/录像）→ finalize 定论 → 报告
                                                  ↘ 失败或超时异步进入 failure analysis
```

- 结论只有一个：`PASSED / FAILED / CANCELLED / TIMED_OUT / ERROR`。基础设施故障是 `ERROR`，业务断言失败是 `FAILED`，通过率固定按 `PASSED / (PASSED + FAILED)`，其余终态单独列出。
- 状态靠租约与心跳维持，Worker 掉线由 reconciler 判为会话丢失，不会静默重启已结束的运行。
- 报告分两次就绪：执行结束即可看基础报告，AI 分析完成后追加模型部分；分析失败会保留规则分类结论并显式标记 `AI_UNAVAILABLE`。只有 `FAILED`、`ERROR`、`TIMED_OUT` 的运行有失败可解释，通过与取消的运行不接受分析请求。
- 证据下载走一次性 ticket（一分钟、单次、绑定用途），Trace、录像与标记为不可公开的制品要 `sensitive_artifact_read` 才能领票；`SENSITIVE` 模式自动关闭原始 Trace 与录像，并且不把证据内容发给模型。

## 控制台

`#/overview` 质量口径与人工待办、`#/cases` Markdown 编辑器与编译诊断、`#/runs` 运行列表与 SSE 实时日志、`#/report` 步骤证据与失败分析、`#/assist` 人工接管（含 OTP 输入，界面不回显）、`#/settings` 环境、秘密、成员与审计。会改变资源的请求都接受可选的 `Idempotency-Key`，带上同一个键重试会重放第一次的响应而不是重复执行；权限不足返回 403 并说明缺哪一项。

## 测试与检查

```bash
python -m pytest -q                     # 仓储、执行器、端到端与失败分析，真实 Chromium
python -m ruff check backend tests scripts && python -m ruff format --check backend tests scripts
cd frontend && npm run build            # tsc -b + vite build
```

端到端用例用 `tests/site/` 的本地站点跑完整链路（保存 → 编译 → 执行 → 证据 → 报告 → 分析）。对着真实网站验收用同一个 HTTP 客户端走公网路径：

```bash
AITA_API_URL=http://127.0.0.1:8000 python scripts/live_check.py
```

## 配置

`.env.example` 按 §15.2 的分组列出键名与含义；启动时做交叉校验（`visibility_timeout > task_hard_limit`、`lease_ttl > heartbeat × 3` 等），不通过就拒绝启动。秘密提供 `local_fernet` 与 `kms` 两种，模型凭据只从环境变量注入。

## 边界

- 不要暴露到公网：`AUTH_MODE=dev` 与默认开发令牌只在 `APP_ENV=development` 下可用。
- 目标站点由环境的 `allowed_domains` 与出网策略限定，`BROWSER_SANDBOX=false` 只适用于容器这类没有 setuid 沙箱助手的隔离环境。
- 本仓库不含任何站点凭据或真实账号；`data/`、`.env` 已 gitignore。

查看[详细设计](./AI-Test-Agent-Detailed-Design.md)、[审查记录](./AI-Test-Agent-Design-Review.md)与[架构图索引](./architecture/README.md)。安全问题请私下联系维护者；不要在公开 issue 中发布密码、OTP、API key 或真实测试账号。

## License

本项目采用 [MIT License](./LICENSE)。
