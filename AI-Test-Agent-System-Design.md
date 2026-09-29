# AI智能自动化测试平台

# 系统概要设计说明书

版本：V1.0

------------------------------------------------------------------------

# 1. 系统总体架构

                     Web Console

                          |

                     API Gateway

                          |

                  Test Management

                          |

                  AI Test Compiler

                          |

                    Test IR Store

                          |

                Execution Orchestrator

                          |

            +-------------+-------------+

            |                           |

     Playwright Executor        Human Task Service

            |

     Chromium Browser

            |

     Target Website

            |

     Result Collector

            |

     Failure Analyzer

------------------------------------------------------------------------

# 2. 技术架构

## Frontend

技术：

-   React
-   TypeScript

职责：

-   测试管理
-   执行监控
-   报告展示

------------------------------------------------------------------------

## Backend

语言：

Python

框架：

-   FastAPI
-   Celery
-   Redis
-   PostgreSQL

------------------------------------------------------------------------

## Browser Engine

技术：

Playwright

浏览器：

Chromium

------------------------------------------------------------------------

# 3. 核心模块设计

## 3.1 Markdown Parser

职责：

解析测试文件。

输入：

Markdown

输出：

Test Case Object

------------------------------------------------------------------------

## 3.2 AI Test Compiler

职责：

将测试描述转换为 Test IR。

示例：

输入：

    点击登录按钮

输出：

``` json
{
 "action":"click",
 "target":{
   "description":"登录按钮"
 }
}
```

------------------------------------------------------------------------

## 3.3 Test IR Engine

系统核心。

作用：

隔离测试描述与执行实现。

------------------------------------------------------------------------

## 3.4 Execution Orchestrator

职责：

-   调度执行
-   管理状态
-   重试

------------------------------------------------------------------------

## 3.5 Playwright Executor

职责：

执行浏览器动作。

接口：

    navigate()

    click()

    input()

    assert()

    screenshot()

------------------------------------------------------------------------

## 3.6 Locator Engine

负责寻找页面元素。

策略：

    Selector

    ↓

    Role

    ↓

    Text

    ↓

    XPath

    ↓

    Vision AI

------------------------------------------------------------------------

## 3.7 Human Task Service

人工介入服务。

数据：

    task_id

    execution_id

    reason

    status

    created_time

------------------------------------------------------------------------

## 3.8 Failure Analyzer

输入：

-   Screenshot
-   DOM
-   Logs
-   Trace

输出：

-   failure_type
-   reason
-   suggestion

------------------------------------------------------------------------

# 4. 数据库设计

## tenant

    id
    name
    created_time

## project

    id
    tenant_id
    name

## test_case

    id
    project_id
    name
    markdown
    version

## test_execution

    id
    case_id
    status
    start_time
    end_time

## step_execution

    id
    execution_id
    step_no
    action
    result
    screenshot

## element_memory

    id
    page
    description
    selector
    success_rate

## human_task

    id
    execution_id
    reason
    status

------------------------------------------------------------------------

# 5. 执行流程

    用户提交测试

    ↓

    Markdown Parser

    ↓

    AI Compiler

    ↓

    生成 Test IR

    ↓

    Execution Engine

    ↓

    Playwright

    ↓

    页面操作

    ↓

    结果分析

------------------------------------------------------------------------

# 6. 部署架构

                  Load Balancer

                        |

                  API Server

                        |

                  Task Queue

                        |

            +-----------+-----------+

            |                       |

     Browser Worker          Browser Worker

            |

     Chromium

------------------------------------------------------------------------

# 7. 扩展设计

未来支持：

## Selenium

增加：

Selenium Adapter

## App自动化

增加：

Appium Adapter

## API测试

增加：

API Executor

------------------------------------------------------------------------

# 8. 开发计划

## Phase 1

完成：

-   Markdown Parser
-   Test IR
-   Playwright执行

## Phase 2

完成：

-   AI Compiler
-   Locator Engine

## Phase 3

完成：

-   Failure Analyzer
-   Human Task
