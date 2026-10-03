# 项目 MCP 数据策略：操作说明与调用示例

对应设计：[详细设计 v1.3 §5.5](../AI-Test-Agent-MCP-Detailed-Design.md)（策略语义、REST-only 决定）及验收 AC-33、AC-35、AC-36。
本文只讲运行期怎么改策略，不改任何设计文档；设计文本以 v1.3 为准。

## 0. 为什么只有 REST

四个开关决定“平台可以把哪些数据交给调用它的模型”，是管理员做出的同意，不是模型可以申请的权限。因此第 1 版不提供任何 MCP 工具修改策略，只有 `PATCH /api/v1/projects/{project_id}/mcp-policy`，且要求 `PROJECT_MANAGE`。工程师角色会直接收到 `FORBIDDEN`（`tests/test_mcp_policy.py::test_turning_mcp_on_is_not_a_permission_a_model_can_ask_for`）。

## 1. 四个字段

| 字段 | 含义 | 关掉会怎样 |
|---|---|---|
| `enabled` | 该项目是否允许 MCP 业务访问 | 除 `aita_get_context`、`aita_list_projects` 外的业务调用被拒 |
| `allow_case_content` | 是否把用例 Markdown、IR、含原文的诊断发给客户端 | 内容类读只返回元数据 |
| `allow_report_details` | 是否发送已脱敏的步骤描述、断言期望/实际与定位摘要 | 报告只给聚合计数与控制台入口 |
| `allow_server_ai` | 是否允许 MCP 显式请求平台内部 AI 编译、视觉兜底或失败分析 | 编译与运行只走确定性路径 |

`allow_case_content` 与 `allow_server_ai` 是两个方向的决策，任何一方都不能由另一方推导，代码里也各自检查。`allow_server_ai=true` 只是必要而非充分条件：运行期还要求模型可用（`AI_ENABLED` 及其连接配置），否则调用被显式拒绝而不是静默降级。

## 1.1 收紧策略对已在队列里的任务同样生效

编译与执行任务是异步的：请求入队时校验过一次策略，真正调用模型的却是 Worker。所以 Worker 在调用前会用「任务快照里的意图 ∩ 此刻的项目策略」重新判定一次（§6.4）。把 `allow_server_ai` 关掉，不需要等队列排空——它撤销的是还没发生的模型外发，也不会撤销已经返回给调用方的受理凭证。

降级的原因写在数据里，而不是只写日志：

- 编译产物：一条 `severity=WARNING`、`code=AI_POLICY_STOPPED` 的诊断，加上 `usage.ai_stopped`。编译本身是成功的，所以绝不占用 `error_code`（§13.2 只把 ERROR 诊断当作错误码来源）。
- 运行：事件日志 `execution.ai_stopped`，`payload` 为 `{"purpose": "vision"|"analysis", "reason": "AI_POLICY_STOPPED"}`。执行快照本身不再改写。
- 失败分析：规则结论照常落库，同行的 `usage.ai_stopped` 记录原因；模型解释这一段直接跳过。

只有 MCP 来源的任务受这条规则约束。网页控制台的编译/运行/AI 行为不由 `mcp_policy` 决定（AC-40）：REST 请求不会写入 AI 意图，收紧四个开关不会让控制台上的用例突然失去模型。若以后 REST 新增可选的 `use_server_ai` 字段，"省略"表示沿用既有行为、`false` 表示显式关闭，两者不能归一为同一个幂等意图。

缺失策略 = 四项全 false。存储的策略若无法按严格模型解析，同样整项关闭，绝不部分开启。

## 2. 先读当前状态

```bash
curl -sS -i "http://127.0.0.1:8000/api/v1/projects/${PROJECT_ID}" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}"
```

响应头里的 `ETag` 形如 `"project-{id}-{row_version}"`，响应体同时给出两份信息，不要混淆：

- `settings.mcp_policy`：存储原样，可能是非法历史值；
- `mcp_policy`：**生效视图**，即 MCP 适配器实际使用的四项布尔值，读不懂的策略在这里就是四项 false。

改策略前必须用这个 `ETag`，不能凭记忆填版本号。

PowerShell：

```powershell
$p = Invoke-RestMethod -Method Get -Uri "http://127.0.0.1:8000/api/v1/projects/$ProjectId" `
      -Headers @{ Authorization = "Bearer $AdminToken" } -ResponseHeadersVariable h
$p.mcp_policy            # 生效策略
$h.ETag                  # 下一步的 If-Match
```

## 3. 局部修改（推荐做法）

```bash
curl -sS -X PATCH "http://127.0.0.1:8000/api/v1/projects/${PROJECT_ID}/mcp-policy" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  -H "If-Match: ${ETAG}" \
  -H "Content-Type: application/json" \
  -d '{"allow_case_content": true}'
```

规则：

- `If-Match` 必填，缺失或不是读到的 ETag 都返回 400 `VALIDATION_ERROR`；
- 请求体至少给四个字段中的一个，空 patch 返回 400；未给的字段保持当前值；
- 值必须是 JSON 布尔。`"true"`、`1`、未知字段、多余字段全部 400（模型是 `extra="forbid", strict=True`）；
- 成功返回 `project_id`、`mcp_policy`（合并后的完整四项）、`row_version`，并在响应头给出新的 `ETag`——下一次修改要用它。

同一份 settings 的其他键（例如 `allow_vision`、`human_slot_ratio`）不受影响：这条路径只替换 `mcp_policy` 子对象。

## 4. 整块 settings 替换的后果（慎用）

既有 `PATCH /api/v1/projects/{project_id}` 仍是**整块替换**合同，不会做深合并：

- 你没写进 `settings` 的其他键会被删除。要用这条路径，必须先 GET 拿到完整 settings，改完再整体提交；
- 省略 `mcp_policy` 等于把它重置为四项关闭，也就是关掉 MCP；这次关闭会被登记为策略关闭变更，进审计；
- 带着非法 `mcp_policy` 提交会被拒绝（400），因为接受它等于写下一条下一次读取必须整项关闭的策略。

只想改一个开关时用第 3 节的端点。控制台目前没有项目 settings 编辑面板，本版也不提供管理 UI，改策略只有上述两条 REST 路径。

## 5. 并发修改

`If-Match` 的版本与库内 `row_version` 不符时返回 409 `VERSION_CONFLICT`；版本比较发生在事务内锁定项目之后，不是在未锁定的 ORM 对象上比一次。

处理顺序固定：重新 GET → 比较你要的字段是否已被别人改成别的值 → 由管理员决定是否以新 ETag 形成**新的**修改意图。不要自动重发旧 patch，那会把别人的修改覆盖成自己已过时的意图。

## 6. 响应丢失、提交确认不确定

本端点不提供 `Idempotency-Key` 业务重放，也不适用 R02 的“原键恢复”规则。响应丢失时：

1. 先 GET `/api/v1/projects/{id}`，比较原 patch 指定的**每一个**字段；
2. 全部相同 → 目标已达成，不重发；
3. 存在不同 → 当前状态未满足目标，基于新 ETag 由管理员重新决定意图；
4. GET 只能证明当前状态，不能证明是谁执行了原请求。要判明原请求是否提交，查审计：审计行与策略更新在同一事务提交，所以“有行即已提交”。

当前没有审计读取的 REST 接口，用数据库查询（表 `audit_log`，每行都带 `request_id`；错误响应体同样返回 `request_id`，成功响应不带，需按服务端日志关联）：

```sql
SELECT created_at, actor_id, request_id, detail
FROM audit_log
WHERE operation = 'project.mcp_policy.update' AND resource_id = '<project_id>'
ORDER BY created_at DESC;
```

`detail` 只存 `fields`（本次改动的字段名）、`before` 与 `after` 的规范化策略摘要，不存请求原文。

若收到 409，走第 5 节流程；此时原请求确定没有提交成功，但仍应确认一次再决定。

## 7. 修复读不懂的历史策略

存储值非法时生效视图是四项 false，且**只能整条替换**：一次请求必须同时给全 `enabled`、`allow_case_content`、`allow_report_details`、`allow_server_ai`，否则 400，`details.required_fields` 列出四个字段名。理由是把一个字段补丁合并进 fail-closed 的默认值，会悄悄关掉三个开关，而管理员以为自己只动了一个。

## 8. 开发工作区

`ensure_development_workspace` 只在开发/test seed 且 `MCP_ENABLED=true` 时，为**首次创建**的 demo 项目写入 `enabled`、`allow_case_content`、`allow_report_details` = true、`allow_server_ai` = false。MCP 关闭时保留原 seed 默认。

已经存在的 demo 项目、被管理员显式关闭的项目、非法策略，启动时一律不覆盖——否则重启会撤销管理员的关闭意图。重复初始化也不改动已有策略、版本或审计（AC-35）。

已有的开发数据库要打开 MCP：按第 3 节调用一次本端点，而不是删库重建。生产 seed 禁止写 demo 策略。

## 9. 已验证的合同

对应测试在 `tests/test_mcp_policy.py`（严格模型与 fail-closed 读取、局部更新保留其他键、审计同事务、必填 If-Match、非法策略整条替换、整块替换省略策略即关闭、seed 边界）与 `tests/test_mcp_close_matrix.py`（14 个工具在项目关闭时的拒绝与重放矩阵）。1.1 节的 Worker 交集由 `tests/test_worker_ai_intent.py` 覆盖：三个 Worker 各自的降级与原因记录、MCP 默认关闭、REST 不受 `mcp_policy` 影响、以及策略读不到的 fail-closed。
