# 出网实跑：`tests/test_mcp_public_site.py`

这条链路默认**不跑**。它真的离开机器：经 `/mcp` 写一个用例、让真实 Worker 起真实 Chromium 访问公网站点、
并（配置了模型时）让平台自己的编译器真的调用一次模型。CI 里没有它，也不该有它——公网抖动、站点改版、
模型回复变化都不是代码回归。

```bash
AITA_LIVE_MCP=1 pytest tests/test_mcp_public_site.py
```

`AITA_LIVE_MCP=1` 之外的开关都走环境变量，凭据**只**从环境读，不写进任何被跟踪文件：

| 变量 | 含义 | 默认 |
| --- | --- | --- |
| `AITA_LIVE_BASE_URL` | 要访问的站点 origin，同时决定环境白名单 | `https://www.baidu.com` |
| `AITA_LIVE_KEYWORD` | 运行变量值，例如 `人工智能`；断言比的是它解析后的值 | `人工智能` |
| `AITA_LIVE_AI_BASE_URL` / `_MODEL` / `_API_KEY` | 平台的模型配置；三者齐备才跑 prose 那一条 | 空（跳过） |
| `AITA_LIVE_COMPILE_TIMEOUT` / `_RUN_TIMEOUT` | 轮询上限（秒） | `300` |

## 它证明什么

第一条：MCP 写进去的结构化步骤，编译成 `SUCCEEDED`、`executable=true`、`review_items=[]`，然后在真浏览器
里跑成 `PASSED`，七步顺序与证据（截图 ref 的 `upload_status=READY`、`size>0`）都在。回执里的
`compiler_mode=deterministic` 是编译器自己说的，不是测试假设的。

第二条：一个自然语言步骤确实会走到模型。模型写了什么，产物就是什么模式，且**不会**因为 MCP 而变得可跑
（§6.3、MCP-AC-07）：`aita_run_test` 必须被拒。"确实走到了"这句不看回执，看库里那一行：编译产物的
`usage.calls>=1`、`usage.latency_ms>0`、`model` 与 `CompileArtifact.model` 一致——凭据被拒、域名不可达时
`calls` 停在 0，这条就红，不会让"模型没连上"冒充"模型答得不好"。MCP 投影里没有 usage（§6.3 只发判定，
不发账目），所以这一眼只能看数据库。同一条测试还会扫已收到的每个回答：API key、`sk-` 前缀、本机路径、
`sqlite:///`、`Traceback`、`object_key` 都不许出现在诊断的自由文本里（§11）。

## 怎么确认它没在假装绿

`skip` 的绿等于什么都没验，反过来"跑过的绿"也要能证伪。2026-10-03 在本机做过两次反证：

* 把 `AITA_LIVE_BASE_URL` 指到一个解析不了的域名，第一条必须红，红在 `outcome=FAILED` /
  `error_code=TARGET_HTTP_ERROR`，七步里 `s1` 失败、其余 `SKIPPED`——说明浏览器真的出网了，断言真的绑在结果上。
* 把 `AITA_LIVE_AI_API_KEY` 换成废凭据，第二条必须红在 `usage` 那行（打印 `{'calls': 0, ...}`）。
* 泄漏那三条 needle 另做一次注入：把带 `sk-…` 的文本塞进一条诊断再从 `/mcp` 读回来，断言确实报警。

## 它对模型的不确定性持什么态度

真模型会给出不一样但都合理的答复。实测同一条 prose 步骤给出过三种：合法的 `page_contains`、需要定位器的
`element_visible`、以及把契约分支名当字段的 `{"action":"assert","assert":{...}}`。后两种是**正确的失败**。

所以这条测试钉的是不变量，不是运气：编译要么 `NEEDS_REVIEW`（带 `ai_generated_step` 审阅项与控制台入口），
要么 `FAILED`（ERROR 诊断点名那一步，且没有可审阅项）；两条路都拒绝运行，且拒绝的话必须与产物状态相符
（"需要人工确认" vs "没有产出可执行 IR"）。模型答复固定下来的那两条路径，在
`tests/test_review_regressions.py` 里用假适配器逐条断言，不依赖网络。

## 公网站点的已知事实

* 百度首页可断言；提交搜索后会先进图形验证码（`wappass.baidu.com/static/captcha/…`），结果行断言不稳。
  用例因此只断言首页文本、填入的值、`url_contains "/s?"` 与截图存在。
* 首页可见的搜索控件是 `#chat-textarea` / `#chat-submit-button`；`#kw` / `#su` 存在但被隐藏。
* 浏览器装在仓库内 `.pw-browsers`（`PLAYWRIGHT_BROWSERS_PATH`），缺它时这条测试会以执行错误失败，
  而不是静默跳过——`skip` 的绿等于什么都没验。
