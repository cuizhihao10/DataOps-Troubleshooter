# 单页诊断 Demo 前端实现设计

## 1. 交付地位与当前状态

前端是产品设计 M4 和最终验收的**必需交付项**，不是可选美化。本文件先固定信息架构、API 依赖、
安全边界和验收口径；当前仓库尚未包含前端实现，因此不能声称单页 Demo 已完成。实现安排在可靠
后台 Worker 与轮询状态契约稳定之后，避免先按当前同步 POST 绑定 UI，再为 queued/running/cancelled
状态重写请求和错误处理。

该顺序不改变前端范围：Worker 切片完成后必须立即实现本文件定义的单页 Demo，并在 Docker 中由同一
FastAPI 服务托管。若异步接口契约发生变化，本文件与前端测试必须在同一切片同步更新。

## 2. 技术选择

首版采用 FastAPI 静态托管的原生 HTML、CSS 和模块化 JavaScript，不引入独立 Node 服务、组件平台
或后台管理框架。理由如下：

- 项目核心是 Agent 控制流与证据可信度，单页 Demo 只需清楚展示现有资源，不需要复杂前端状态库。
- 同一 Docker 容器提供 API 和静态资源，可避免 CORS、双进程部署和额外 lockfile，面试时一条命令
  即可启动。
- 浏览器原生 `fetch`、AbortController 和语义化 HTML 足以实现提交、轮询、取消请求及错误恢复。
- 人工编写 JavaScript 的每个函数必须有详细 JSDoc，复杂状态转换旁必须解释原因与失败边界，继续
  遵守项目“学习与求职”注释要求；HTML/CSS 也要分区说明信息架构与可访问性取舍。

如果后续交互复杂度确实超过原生模块承载能力，才评估 Vite/React，并先记录迁移收益、构建产物责任
和依赖成本；不能为了技术栈数量提前引入重型前端。

## 3. 页面信息架构

单页按从输入到证据、再到结论的阅读顺序分为六区：

1. **服务状态栏**：显示 `/health` 的数据库、Planner、Auditor、MCP、知识节点/边和契约版本；密钥、
   数据库 URL 和内部异常永不显示。
2. **会话与输入区**：创建 session，填写自然语言问题，显式选择 intent、组件和历史触发；在自然语言
   路由器实现前不能隐藏这些必填结构字段。
3. **运行状态区**：显示 queued/running/completed/failed/cancelled、run ID、开始时间、耗时和安全错误；
   页面刷新后可用 run ID 恢复，而不是只依赖内存 spinner。
4. **Action/Observation 时间线**：按 `RunPublicEvent.sequence` 分组展示 retrieval、react、report、memory、
   system；只展示 decision summary、工具名、状态、Evidence ID 和停止原因，不展示 Thought。
5. **证据与故障链区**：Evidence 卡片显示 source、observed_at、reliability 和 content；GraphRAG 路径
   卡片按 node/edge 顺序显示故障链，并标记最终报告是否引用该 `path_id`。
6. **已审计报告与记忆区**：展示 summary、Top-1/候选根因、引用、处置步骤、风险、不确定性、相似
   confirmed 案例；pending memory 只能由用户显式 confirm/reject。

窄屏时六区按上述顺序纵向排列；宽屏可把输入/状态与证据/报告分栏，但 DOM 阅读顺序保持不变，保证
键盘和屏幕阅读器不会因视觉布局改变证据因果顺序。

## 4. 前端状态机与 API 依赖

目标异步资源契约应支持：

```text
idle
  -> creating_session
  -> submitting_message
  -> queued | running
  -> completed | failed | cancelled
```

前端只使用结构化 API：创建 session、提交 message、读取 run、读取 events、确认/拒绝 memory 和健康
检查。`POST message` 返回 run 资源后，页面以有上限的退避轮询 `GET run/events`；切换 session 或重新
提交时使用 AbortController 取消旧浏览器请求，但不能把浏览器取消误写成服务端 run 已取消。服务端
取消只有在未来取消 API 返回终态后才展示 cancelled。

轮询必须遵守以下边界：

- running 使用短间隔，长时间无变化逐步退避并保留“仍在运行”提示；completed/failed/cancelled 立即停止。
- 事件按 sequence 去重和排序；发现缺号时保留已有事件并提示刷新，不能自行重排或填造 Observation。
- 404 区分 session/run 不存在；422 展示字段校验；500 只展示服务端公开 error code/message。
- 浏览器刷新可从 URL 中的 session/run ID 恢复；本地存储不得保存 API key、Prompt 或完整模型响应。

## 5. 安全与可解释性边界

- 页面和网络调试面只消费公开 API，不读取服务端日志、checkpoint 内部对象或 Provider SDK 响应。
- 不提供 Thought 展开按钮，也不把 `decision_summary` 标成“思维链”；它只是公开决策摘要。
- 根因、处置和故障链中的每个引用都应可点击定位 Evidence/path，悬空引用显示为契约错误而非隐藏。
- 高风险处置使用明显警示并展示 prerequisites、rollback、verification；前端不增加任何自动执行按钮。
- Fixture 标明合成数据；禁止输入或示例包含真实内部域名、日志、租约、凭据或生产资源身份。
- HTML 渲染使用 `textContent` 或受控 DOM 构造，不用不可信数据赋值 `innerHTML`，避免报告内容触发 XSS。

## 6. 实现切片与验收条件

前端交付分两个可验证小步，但必须连续完成：

### 6.1 静态壳与运行时间线

- FastAPI `/demo` 返回单页，Docker 健康后可直接打开。
- 能创建 session、提交消息、轮询 run/events，并恢复刷新前的 run。
- 五类公开 event 可按 sequence 展示，loading、空、失败、终态均有明确视觉状态。
- 浏览器自动化使用合成 API 数据验证无 Thought/Prompt 字段、错误恢复和响应式布局。

### 6.2 证据、GraphRAG、报告与记忆操作

- Evidence、路径、根因和处置引用可以双向定位，冲突证据不会被折叠成单一“正确”卡片。
- 报告完整展示 risks、uncertainties、相似案例解释和 Auditor outcome。
- pending memory 可 confirm/reject，操作后重新读取并显示 confirmed/rejected；失败不做乐观伪成功。
- README、实现指南、演示脚本和 Docker 验证同步更新，并保存至少一个三组件合成场景的演示截图。

完成定义：上述两步、详细 JSDoc/注释、前端单元/浏览器测试、FastAPI 静态托管集成测试和 Docker 实机
验证全部通过，才可以把“单页 Demo”从尚未完成改为已完成。
### Worker 契约已稳定（前端实现前置条件）

后端现在提供 `diagnosis-resources:v3`：提交 message 返回 HTTP 202 与 `queued` run，前端必须用 run_id 轮询 GET run/events，展示 `queued -> running -> completed|failed`，并把 HTTP 409 的 `active_run_id` 显示为“当前 session 已有任务”。本切片尚未加入 `cancelled`，浏览器 AbortController 只取消客户端请求。前端实现仍待下一切片，必须保留本文件定义的 Evidence、Action/Observation、Auditor、uncertainty、memory confirm/reject 和 Thought 禁止边界。
## 7. Demo 前端已经落地（diagnosis-resources:v3）

`/demo` 已由 FastAPI 同源托管，使用原生 HTML/CSS/ES Module，不引入额外 Node 服务。页面当前完整覆盖：

- `/health` 依赖、Worker 配置、契约版本与数据规模摘要。
- 创建 session、选择单个或多个组件、提交 message，并轮询 `queued -> running -> completed|failed`。
- 按 `RunPublicEvent.sequence` 展示公开 Action/Observation 时间线；所有文本写入 DOM 使用 `textContent`，避免合成 Evidence 被当成 HTML 执行。
- 报告 summary、root cause、risk 和 uncertainty 摘要；不展示 Prompt、Thought、embedding 或 Provider 原始响应。
- `memory_stage.memory` 的 pending 候选显示 confirm/reject；按钮调用 `POST /api/v1/memories/{memory_id}/confirm`，以服务端返回状态为准，confirmed/rejected 后自动隐藏操作按钮。

这段实现对应学习型验收闭环：浏览器只保存 `sessionId/runId/memoryId` 等可恢复标识，后端 PostgreSQL Worker 才是队列和记忆状态真相；网络失败只显示错误，不伪造 completed 或自动确认案例记忆。`tests/integration/test_demo_frontend.py` 覆盖静态资源、路径穿越防护、409 错误提示和记忆决策入口。
