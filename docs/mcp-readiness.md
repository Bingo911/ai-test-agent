# MCP 就绪探针：读法与处置

`GET /api/v1/mcp/readiness`。只在 `MCP_ENABLED=true` 时注册，关闭时这个路径和其他未知路径一样 404。
`/api/v1/health` 的语义不变，它讲平台；本探针只回答一个问题：**这个进程现在能不能接 MCP 请求**（§13.5、AC-24、AC-34）。

## 0. 一句话规则

状态码由五个组件决定，且只有这五个：

```
transport=running 且 auth=initialised 且 database=ok 且 schema=ok 且 limiter∈{ok, not_required}
→ 200；否则 503
```

三个能力信号（submission / execution / background）永远不改变状态码。Worker 停了、broker 挂了，API 仍然能把
提交落库，所以它仍然是 200；能力位与 reason 会把这个坏消息单独说清楚。

## 1. 响应体

```json
{
  "status": "ready",
  "components": {"transport": "running", "auth": "initialised", "database": "ok", "schema": "ok", "limiter": "ok"},
  "capabilities": {"submission_available": true, "execution_available": false, "background_available": false},
  "capability_reasons": {"submission": "available", "execution": "no_live_worker", "background": "no_live_worker"}
}
```

组件取值是固定词表，探针不会把依赖自身的报错文本带回来（那里面可能有 URL、key id、凭据）：

| 组件 | 可能值 | 含义 |
| --- | --- | --- |
| `transport` | `running` / `stopped` / `disabled` | session manager 是否已进入 |
| `auth` | `initialised` / `uninitialised` / `disabled` | 认证组件是否按模式配齐；`oidc` 需要 key source 与 `MCP_OIDC_AUDIENCE` |
| `database` | `ok` / `unavailable` / `stale` / `disabled` | 采样里**能不能读到**，读不到就是 `unavailable` |
| `schema` | `ok` / `drift` / `stale` / `disabled` | 读到了但**形状版本不对**才是 `drift`（§13.6） |
| `limiter` | `ok` / `unavailable` / `stale` / `not_required` / `disabled` | 生产必须有 Redis 限流器；`not_required` 只可能出现在开发环境（§12.2） |

`stale` 不是“坏了”，是“这个进程现在不知道”。超过 3 个采样周期没有新观测，正向答案就作废。

## 2. 能力位与 reason

reason 也是固定词表：`available`、`no_live_worker`、`dispatch_failing`、`dispatch_stalled`、`no_evidence`、
`database_unavailable`、`schema_unavailable`、`limiter_unavailable`。

| 信号 | 证据来自 | 为什么单独算 |
| --- | --- | --- |
| `submission` | database + schema + limiter，**不含 Worker** | 提交落库与有没有人干活是两件事 |
| `execution` | Outbox 派发证据 + 心跳里声明了 `execution` 队列的活 Worker | 出问题时先说确定的那一半 |
| `background` | 同上，队列换成 `compile` / `analysis` | 两类 Worker 可以只挂一个 |

派发证据只看三种真信号：未发布的行带 `last_error` → `dispatch_failing`；未发布的行过了自己的
`next_attempt_at` 60 秒以上 → `dispatch_stalled`；最近 300 秒内确实发布过 → 可用。**空 Outbox 不算证据**：
闲系统和一小时前就停掉的派发器，从表里剩下的行长得一模一样（§13.5）。

`no_live_worker` 优先于派发质量：没人消费的队列，运维能做的是起 Worker，而不是去查 broker。

## 3. 探针自己花多少

采样由 app lifespan 持有的一个后台任务完成，规则写死在 `backend/app/mcp/readiness.py`：

- 正常 5 秒一轮，失败退避、上限 30 秒；
- 每轮最多 1 秒网络预算，数据库那一趟跑在探针自己的单线程池里，不占执行槽位；
- limiter 一次 `PING`，一条命令；
- **不抓 JWKS**（探针绕过刷新冷却去压 IdP 是被明确禁止的）、**不 ping broker**、不跑 admission 脚本；
- 请求路径不查库：它读的是缓存的观测，所以探针不会变成它正在测的那份负载；
- 关闭时先停采样、清空缓存，再收客户端。

探针说 200 也永远不会让一次请求跳过准入（§11）。

## 4. 常见读法

| 看到 | 处置 |
| --- | --- |
| `503` + `database=unavailable` | 先查连接与网络；不要跑迁移，那不是这个问题的形状 |
| `503` + `database=ok`、`schema=drift` | 跑结构 Job（`docs/deployment-schema.md`），本进程只校验不改结构 |
| `503` + `limiter=unavailable` | 生产 fail-closed：限流 Redis 不可用就不接新会话，不回退进程内计数（§11） |
| `503` + 任一 `stale` | 采样器没跟上：看进程是不是刚起来、DB/Redis 是不是卡在超时 |
| `200` + `execution=false`、`no_live_worker` | 起 Worker 或补 `WORKER_ROLES`；`-Q` 对探针不可见 |
| `200` + `dispatch_failing` / `dispatch_stalled` | broker 或派发任务出问题，提交仍然安全 |

## 5. Worker 侧前提

探针读的是心跳里 `capabilities.queues`，也就是这个进程自己声明的队列。Celery Worker 必须显式带上
`WORKER_ROLES`，且和命令行 `-Q` 一致；留空表示**不声明任何队列**，探针就按“没有这份容量”上报，绝不猜测
（`compose.yaml` 里两个 Worker 都写了，`.env.example` 里是注释掉的样例）。心跳超过 `LEASE_TTL_SECONDS`
（默认 30 秒）就不再算活。

## 6. 已验证的合同

`tests/test_mcp_readiness.py`（31 例）逐条对着 §13.5 写，其中几条专门钉住容易写错的地方：空 Outbox 不构成
证据、缺 Worker 先于派发质量、过期观测不算可用、预算超时那一轮记为 `unavailable`、一次采样只花一条 Redis
命令、采样绝不改动 Outbox 行、探针不碰 `orchestrator.queue`、生产 limiter 不可用只 503 而不让进程退出。
每条都做过变异验证：把实现改回旧行为，对应的用例必须变红。
