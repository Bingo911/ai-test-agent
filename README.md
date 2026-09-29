# AI Test Agent

**用 Markdown 编写 Web 测试，先将每个步骤编译成可校验的 Test IR，再由浏览器执行器运行。**

这是根据产品需求与系统设计文档搭建的开源项目雏形。当前版本提供可以启动的 React/TypeScript 控制台、FastAPI 服务，以及安全解析 YAML DSL 并生成 Test IR 的 API。Playwright Worker、队列、数据库持久化、AI 提供商接入、身份鉴权与人工接管仍在设计/排期中，当前项目不会向真实网站提交测试动作。

> 当前 API 是无数据库的原型，不提供多用户安全、测试用例长期存储、环境秘密管理或浏览器沙箱。不要将它直接暴露到公网上，也不要用于生产环境。

## 快速启动

需要 Docker Compose。仓库不含密钥和机器相关配置。

```powershell
docker compose up --build
```

打开 [http://localhost:5173](http://localhost:5173) 使用 Markdown 测试用例编辑器，点击“生成 Test IR”查看实际解析结果。API 文档见 [http://localhost:8000/api/docs](http://localhost:8000/api/docs)，健康检查见 [http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health)。第一次启动需要拉取容器镜像并安装前端依赖。

## 本地开发

API：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m uvicorn backend.app.main:app --reload --port 8000
```

Web 控制台需要 Node.js 20.19+ 或 22.12+。

```powershell
cd frontend
npm install
npm run dev
```

Vite 会将 `/api` 请求转发至 `http://localhost:8000`。修改 API 地址时设置 `API_PROXY_TARGET`。

## 编译一份用例

```http
POST /api/v1/cases/parse
Content-Type: application/json
```

````json
{
  "markdown": "# 登录测试\n\n## Step 1\n```yaml\naction: open\nurl: https://example.com/login\n```"
}
````

每一步使用一个 YAML 代码块。当前编译器校验用例标题、连续步骤编号、动作合同、定位字段和变量引用，并返回含 SHA-256 原文摘要、源行范围及脱敏秘密引用的 Test IR。遇到自然语言步骤、含糊的目标或不支持的 YAML 会返回 422 诊断；当前版本不会调用 AI 补全。

支持的动作：`open`、`click`、`input`、`clear`、`upload`、`wait`、`assert`、`screenshot`。目标定位字段可使用 `css`、`role + name`、`text` 或 `xpath`。密码应通过 `value: "${secrets.login_password}"` 引用，Test IR 只保留密钥名。

## 项目结构

```text
backend/app/                 FastAPI、DSL Parser、Test IR Schema
frontend/src/                React/TypeScript 测试用例工作台
architecture/                逻辑架构图与部署边界图（SVG/PNG/Mermaid）
compose.yaml                 API 与 Web 开发环境
AI-Test-Agent-Product-Requirement.md
AI-Test-Agent-System-Design.md
AI-Test-Agent-Detailed-Design.md
AI-Test-Agent-Design-Review.md
```

## 路线图

1. 将现有解析器扩展为 DSL 兼容层，补充 API 合同、版本历史与项目持久化。
2. 增加 Playwright 执行 Worker、状态调度、隔离浏览器及可审计的证据采集。
3. 接入可禁用的 AI 编译、定位和失败分析能力。
4. 完成租户权限、环境配置、人工协助、安全测试和部署运维。

查看[详细设计](./AI-Test-Agent-Detailed-Design.md)、[审查记录](./AI-Test-Agent-Design-Review.md)与[架构图索引](./architecture/README.md)。安全问题请私下联系维护者；不要在公开 issue 中发布密码、OTP、API key 或真实测试账号。

## License

本项目采用 [MIT License](./LICENSE)。
