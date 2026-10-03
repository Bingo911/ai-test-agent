# 数据库结构：部署 Job、启动校验与回滚

对应设计：[详细设计 v1.3 §13.6](../AI-Test-Agent-MCP-Detailed-Design.md)（结构由部署 Job 管理、生产三步初始化、禁止自动创建默认管理员/项目）与验收 AC-25。
本文只讲怎么部署结构，不改任何设计文本；设计以 v1.3 为准。

## 0. 一句话规则

`create_all` 只会“补缺失的表”，永远不会改动已存在的表的列、约束或索引。因此**“表建起来了”不等于“这就是本版本运行的结构”**。能改结构的只有一个进程：一次性 Job。API、orchestrator、Worker 一律只读那份版本记录，读不到本版本的答案就拒绝启动。

## 1. 三个角色

| 角色 | 入口 | 允许做什么 | 不允许做什么 |
|---|---|---|---|
| 部署 Job | `python -m app.db.schema_job` | 建表、写 `schema_migration` 版本行 | 猜测列差异；把“建好了”当成成功而不检查残留漂移 |
| 开发引导 | `bootstrap_runtime(settings, database=..., seed=...)`，`APP_ENV=development/test` | 同上，外加写入本地开发租户/项目/用户 seed | 在结构不完整时继续启动 |
| 服务进程 | 同一个 `bootstrap_runtime`，`APP_ENV=production` | 只 `verify_schema()` | 任何 DDL、任何 seed |

角色由 `APP_ENV` 决定，而不是新增一个开关：`is_development` 为假即“只校验”，配置文件写错环境时最坏结果是拒绝启动，不是悄悄改库。

并发安全来自 PostgreSQL 的 advisory lock（锁名 `ai-test-agent:schema`）。开发 compose 里 API 与两个 Celery 队列几乎同时启动，两个进程不能交错执行 DDL；锁键用 sha256 派生而不是 `hash()`，后者按进程加盐，两个进程会各自锁在不同的键上。SQLite 只有一个写者，直接跳过锁，不会为锁去连一条连接。

Worker 侧的入口在 `celery_app._confirm_structure`（`worker_process_init`）：它调 `bootstrap_runtime(..., seed=False)`——Worker 没有资格创建租户。

## 2. 生产初始化三步（§13.6）

```bash
# 1) 结构：一次性 Job，退出码非 0 就不要继续
python -m app.db.schema_job

# 2) 配置：管理员通过 REST 建租户、用户与项目成员（§5.2、§5.5），不走任何自动 seed
# 3) 服务：API / orchestrator / Worker 只校验
```

第 2 步是人工受控的：平台在生产环境**永远不会**自动创建默认管理员或默认项目。`APP_ENV=production` 下 `bootstrap_runtime` 返回空 dict，一个 `AppUser`/`Project`/`Tenant` 行都不会写（`tests/test_schema_versioning.py::test_a_production_bootstrap_seeds_nothing`）。

Job 的退出码：`0` 结构与记录都齐；`1` 结构建不起来（例如库里已存在旧列集的表）；`2` 连不上数据库或配置非法。`2` 只打印异常类名，不打印 traceback——驱动异常里可能带着打不开的 DSN（§11.1）。

compose 的开发拓扑同样走 Job：`schema` 服务跑 `python -m app.db.schema_job`，`api` 以 `service_completed_successfully` 等它，于是“API/Worker 不并发 create_all”在真实编排里也成立，而不只是约定。

## 3. 加一次迁移

结构变更是**追加**，不是修改历史条目：

1. 改 `backend/app/db/models.py`（加表/加列）。加列必须可空或有默认值；删列、改类型、改约束不在本流程内，需要单独写 DDL 与验证步骤。
2. 在 `backend/app/db/schema.py` 的 `MIGRATIONS` 追加一条 `Migration(version=N+1, contract=SCHEMA_CONTRACT, note=...)`，并把 `SCHEMA_VERSION` 改成 `N+1`。
3. 跑 `tests/test_schema_versioning.py`：Job 必须能在新库上把版本记到 `N+1`，旧版本库上必须因为缺列而报漂移。
4. `note` 里写清这条版本对应的设计小节，运维读 `schema_migration` 表时不需要打开代码。

`schema_migration` 不写 `tenant_id`：这是部署记账而不是租户数据，“是不是我模型期望的结构”对所有租户是同一个问题。

## 4. 备份与回滚

- 每次跑 Job 前：`pg_dump -Fc`（备份文件落在部署机的对象存储之外，不入库、不进日志）。
- 加表/加列属于**前向兼容**变更：旧版本进程能带着多余列继续跑，所以回滚 = 把应用镜像退回上一版本，结构不动。这是 `MIGRATIONS` 只追加、且新列一律可空的原因。
- 需要删列/改约束时，走两次发布：先发布“不再读写该列”的应用版本，再发布带 DDL 的版本；回滚这段 DDL 只能靠第 3 步之前那份 `pg_dump` 恢复，本文不提供自动 down migration，因为把数据删掉的逆操作不是一个能靠 SQL 保证的东西。
- 回滚到更旧的应用版本时，若 `schema_migration` 里记录的版本比该版本自己的 `SCHEMA_VERSION` 更高，进程会拒绝启动并打印 `recorded version N, this build needs M`——这是预期保护，不是故障。

## 5. 启动校验到底比什么

`current_drift()` 比三件事：缺失的表、缺失的列、记录的最高版本是否等于本构建的 `SCHEMA_VERSION`。不比约束与索引——那些是一条自己的 DDL、自己的回滚步骤的迁移，也不是一个进程能从一个数字里推断出来的东西。

连不上数据库时抛的是驱动异常，不返回“漂移”：**“不在这儿”和“不是这个形状”是两个答案**，readiness 探针要按不同的码上报（§13.5）。

## 6. 测试

- `tests/test_schema_versioning.py`：Job 建表并记账、重复执行只留一行、生产进程拒绝且不建表、版本超前/缺失都拒绝、旧列集的表被判为漂移且不记账、开发库不完整同样拒绝、Job 的两个退出码、DDL 与记账都在锁内、锁键跨进程一致（真起一个子进程比对）、SQLite 不为锁连库、PostgreSQL 分支在异常里仍解锁、生产 bootstrap 不 seed、Worker 只校验、`create_all` 只出现在一处（源码扫描）、compose 只跑一次 Job 且 API 等它。
