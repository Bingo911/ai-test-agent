# HTTPS 代理与转发头信任：部署合同

对应 §13.3 与 MCP-AC-22 / MCP-AC-29。样例配置：`deploy/nginx-site.conf`。

## 0. 一句话规则

**公开入口决定转发什么，进程决定信谁。** 代理只转发四类路径，其余一概不转；进程只相信
`FORWARDED_ALLOW_IPS` 里点名的地址写下的 `X-Forwarded-*`，而对外展示的每一个 URL 都来自配置，
不来自请求的 Host。

## 1. 四类路径

| 路径 | 去向 | 说明 |
| --- | --- | --- |
| `= /mcp` | API | MCP 端点本身，**路径不改写**（`proxy_pass` 不带 URI 段） |
| `/.well-known/` | API | RFC 9728 资源发现，根写法与带资源路径的写法都要能命中 |
| `/api/v1/` | API | 控制台需要的 REST；readiness 探针也在其中，但它是给内网负载均衡用的 |
| `/` | 控制台 | 兜底只能是控制台，新增 `location` 等于新增一条公网路由 |

Header 保留靠 nginx 的默认透传：`Authorization`、`MCP-Protocol-Version`、`Mcp-Method`、`Mcp-Name`、
`Accept`、`Content-Type` 都会原样到达 API。**不要**为了“显式”而写
`proxy_set_header Mcp-Method $http_mcp_method;`：客户端没发这个头时，那会变成发一个空值头。

## 2. 谁读什么

| 东西 | 来源 | 绝不来自 |
| --- | --- | --- |
| metadata 的 `resource`、`WWW-Authenticate` 里的地址 | `MCP_PUBLIC_URL` | 请求 Host、`X-Forwarded-Host` |
| 工具输出里的控制台链接 | `MCP_CONSOLE_URL` | 请求 Host |
| token 校验用的 audience | `MCP_OIDC_AUDIENCE` | 请求 Host |
| Host 白名单判断 | `MCP_ALLOWED_HOSTS`（代理转发来的合法 Host） | — |

代理不设置 `X-Forwarded-Host`：应用侧没有任何一处读它，多写一个头只是多给后来人一个可以误用的入口。

## 3. 三个开关

```
PROXY_HEADERS=false          # 默认：完全不读转发头
FORWARDED_ALLOW_IPS=127.0.0.1 # 只有真的存在代理时才改成代理自己的地址
MCP_ALLOWED_HOSTS=test.example.com
```

- `PROXY_HEADERS=true` 且 `FORWARDED_ALLOW_IPS` 只剩通配符 → **启动即失败**。通配符等价于“相信任何
  能连上我的进程”，而任何能发这个头的进程就是攻击者本身。
- 传给 uvicorn 的是清洗后的显式列表，不走 uvicorn 自己的环境变量兜底，避免运行环境悄悄放宽信任。
- `MCP_ALLOWED_HOSTS` 必须包含 `MCP_PUBLIC_URL` 的域名（端口形式按实际拓扑决定，检查只看主机名）。
  漏掉的结果是这台部署拒绝自己客户端的流量：SDK 会返回 421。
- 开发 Compose 的端口发布绑 `127.0.0.1`；容器内部仍监听 `0.0.0.0`，因为桥接网络要能路由到它，
  而容器内回环 healthcheck 看不出监听范围错了（§13.2）。

## 4. 上限、超时与流式

| 项 | 代理 | 进程 | 为什么两边都要 |
| --- | --- | --- | --- |
| MCP 请求体 | `client_max_body_size 1m`（`/mcp`） | `MCP_MAX_REQUEST_BYTES=1048576` | 代理先拒省带宽，应用仍然自己算一遍 |
| REST 请求体 | `client_max_body_size 32m`（server 级） | `MAX_REQUEST_BODY_BYTES=32MiB` | 同上（§12.2 启动期校验 `mcp ≤ rest`） |
| `/mcp` 读超时 | 3600s | 工具自身 15s 预算 | 长连接不能被代理掐掉，但一次调用也不能没有上限 |
| 响应缓冲 | `proxy_buffering off`、`proxy_cache off` | — | SSE/流式响应必须逐块出去 |
| 日志 | 自定义 `log_format`：只有地址、方法、路径、状态、耗时 | — | 不记查询串、不记任何 header 值（`Authorization` 就在里面） |

80 端口只做一件事：`return 301 https://test.example.com$request_uri;`。目标域名写死，不用 `$host`，
否则重定向本身就成一个可以被投毒的响应。

## 5. 从宿主机验收（AC-29）

容器内的 healthcheck 不能代替这三条：

```bash
curl -fsS http://127.0.0.1:8000/api/v1/health                    # REST 直连
curl -fsS http://127.0.0.1:5173/api/v1/health                    # 控制台 → API
curl -fsS http://127.0.0.1:8000/api/v1/mcp/readiness             # MCP 开关与依赖状态
curl -fsS https://test.example.com/.well-known/oauth-protected-resource/mcp
curl -fsS -X POST https://test.example.com/mcp \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" -H 'MCP-Protocol-Version: 2026-07-28' \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

最后一条要拿到 `serverInfo`；拿不到就先按顺序查：`MCP_ALLOWED_HOSTS` 是否含该域名（421）、
token 的 audience 与 `MCP_OIDC_AUDIENCE` 是否一致（401）、readiness 是否 503。

## 6. 已验证的合同

`tests/test_mcp_proxy.py` 直接读上面这两个文件：路径集合、`/mcp` 不改写路径、两处体积上限等于
应用侧的两个设置值、没有 `X-Forwarded-Host`、访问日志不含头值、80 只重定向且目标写死、
Compose 的 `API_HOST` 与 loopback 发布；另有两条启动期校验（通配符信任被拒、Host 白名单必须含公网
域名）和一条 metadata 在恶意 Host 下不改变输出的用例。
