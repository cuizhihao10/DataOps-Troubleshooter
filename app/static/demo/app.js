/**
 * DataOps Troubleshooter Demo 的状态与 API 适配层。
 *
 * 设计原则：后端是唯一事实来源，浏览器只保存 session_id/run_id 和当前渲染状态；每次
 * 轮询都重新验证 JSON 结构并用 textContent 写入 DOM，避免把合成 Evidence 当成可执行 HTML。
 * 取消是显式的服务端操作（`POST /runs/{id}/cancel`），关闭页面或中断 fetch 不会改变 run 状态。
 * 调用链面板只展示 `run-trace:v1` 的层级、状态与数字，后端契约已保证其中不含任何推理正文。
 * 实时更新优先走 `run-stream:v1` 的 SSE，但轮询始终保留：浏览器 `EventSource` 无法携带
 * Authorization 头，因此 bearer 鉴权模式下推流必然被拒，退回轮询是正常路径而不是故障处理。
 */

const state = {
  sessionId: null,
  runId: null,
  memoryId: null,
  pollTimer: null,
  pollAttempt: 0,
  stream: null,
  streamFrames: 0,
  // 已渲染的事件序号集合：推流增量追加与整表刷新可能覆盖同一批事件，用序号去重比比较正文更可靠。
  renderedSequences: new Set(),
};

/**
 * 统一执行 JSON API 请求并把 HTTP 错误转换为可展示对象。
 *
 * @param {string} path - 仅允许同源相对 API 路径，避免 Demo 把凭据发送到外部域名。
 * @param {RequestInit} [options] - fetch 的方法、body 和 headers；默认 GET。
 * @returns {Promise<{response: Response, payload: any}>} 原始响应与 JSON payload。
 * @throws {Error} 网络失败或响应不是 JSON 时抛出可诊断错误，调用方决定 UI 降级。
 */
async function requestJson(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { Accept: "application/json", ...(options.headers || {}) } });
  const payload = await response.json().catch(() => ({ detail: "服务端返回了不可解析的响应" }));
  if (!response.ok) {
    const error = new Error(payload.detail?.message || payload.detail || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return { response, payload };
}

/**
 * 用 textContent 写入单个字段，保证 Evidence/错误摘要中的尖括号不会被当作 HTML 执行。
 * @param {string} id - DOM 元素 id。
 * @param {unknown} value - 要展示的安全文本；null/undefined 显示长破折号。
 */
function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
}

/**
 * 根据后端状态更新状态徽章和进度条。
 * @param {string} status - queued、running、completed、failed、cancelled 或 idle。
 */
function setRunState(status) {
  const stateElement = document.getElementById("run-state");
  const progress = document.getElementById("run-progress");
  stateElement.textContent = status;
  stateElement.dataset.state = status;
  const widths = { idle: "3%", queued: "24%", running: "58%", completed: "100%", failed: "100%", cancelled: "100%" };
  progress.style.width = widths[status] || "3%";
}

/**
 * 渲染 /health 的公开摘要，帮助学习者把服务依赖与诊断状态区分开。
 * @param {any} health - HealthResponse JSON；只读取公开字段，不展示 URL、密钥或原始 Fixture。
 */
function renderHealth(health) {
  const chip = document.getElementById("service-status");
  chip.textContent = health.status === "ok" ? `服务正常 · ${health.diagnosis_api.execution_mode}` : "服务异常";
  chip.dataset.state = health.status === "ok" ? "ok" : "error";
  const fields = [
    ["数据库", health.database_status],
    ["Worker", health.diagnosis_api.worker_status],
    ["MCP 工具", `${health.mcp_tools_available.length} 个`],
    ["知识图谱", `${health.knowledge_nodes_loaded} 节点 / ${health.knowledge_edges_loaded} 边`],
    ["Golden Case", `${health.golden_cases_loaded} 条`],
    ["Embedding", health.retrieval.embedding_provider],
    ["诊断契约", health.diagnosis_api.contract_id],
    ["Worker 租约", `${health.diagnosis_api.worker_lease_seconds}s`],
  ];
  const grid = document.getElementById("health-grid");
  grid.replaceChildren(...fields.map(([label, value]) => {
    const card = document.createElement("div");
    card.className = "health-card";
    const title = document.createElement("dt");
    title.textContent = label;
    const content = document.createElement("dd");
    content.textContent = value;
    card.append(title, content);
    return card;
  }));
}

/**
 * 读取 health 并把失败显示为服务错误，不阻断用户查看已存在的 run。
 */
async function refreshHealth() {
  try {
    const { payload } = await requestJson("/health");
    renderHealth(payload);
  } catch (error) {
    const chip = document.getElementById("service-status");
    chip.textContent = `服务不可用 · ${error.message}`;
    chip.dataset.state = "error";
    document.getElementById("health-grid").replaceChildren();
  }
}

/**
 * 将 run 快照投影到状态卡片，并在终态展示安全错误摘要。
 * @param {any} run - AgentRunSnapshot JSON。
 */
function renderRun(run) {
  setRunState(run.status);
  setText("session-id", run.session_id);
  setText("run-id", run.run_id);
  setText("attempt-count", run.attempt_count);
  setText("run-error", run.error_code ? `${run.error_code}: ${run.error_message}` : "—");
  document.getElementById("refresh-events").disabled = false;
  document.getElementById("refresh-trace").disabled = false;
  if (run.result) renderReport(run.result);
  const cancelButton = document.getElementById("cancel-run");
  const resumeButton = document.getElementById("resume-run");
  if (cancelButton) cancelButton.hidden = !["queued", "running"].includes(run.status);
  if (resumeButton) resumeButton.hidden = run.status !== "cancelled";
}

/**
 * 把一条公开事件投影为时间线条目元素；只写 textContent，绝不使用 innerHTML。
 *
 * @param {any} event - RunPublicEvent JSON；只读取 sequence/phase/event_type/summary 四个安全字段。
 * @returns {HTMLLIElement} 可直接插入时间线的条目。
 */
function createTimelineItem(event) {
  const item = document.createElement("li");
  item.className = "timeline-item";
  const meta = document.createElement("div");
  meta.className = "timeline-meta";
  meta.textContent = `#${event.sequence} · ${event.phase} · ${event.event_type}`;
  const summary = document.createElement("p");
  summary.className = "timeline-summary";
  summary.textContent = event.summary;
  item.append(meta, summary);
  return item;
}

/**
 * 渲染公开 RunPublicEvent 列表；payload 只展示白名单摘要，避免泄漏内部原始对象。
 * @param {any} eventList - RunEventList JSON。
 */
function renderEvents(eventList) {
  const timeline = document.getElementById("timeline");
  state.renderedSequences = new Set();
  if (!eventList.events?.length) {
    timeline.replaceChildren(Object.assign(document.createElement("li"), { className: "empty-state", textContent: "当前没有公开事件。" }));
    return;
  }
  eventList.events.forEach((event) => state.renderedSequences.add(event.sequence));
  timeline.replaceChildren(...eventList.events.map(createTimelineItem));
}

/**
 * 追加一条推流事件到时间线；已渲染过的序号直接忽略。
 *
 * @param {any} event - run_event 帧携带的 RunPublicEvent JSON。
 * @returns {void} 通过 DOM 追加条目，不重新请求整条时间线。
 */
function appendTimelineEvent(event) {
  if (state.renderedSequences.has(event.sequence)) return;
  const timeline = document.getElementById("timeline");
  // 首条推流事件要先清掉占位提示，否则"当前没有公开事件"会永久留在时间线顶部。
  const placeholder = timeline.querySelector(".empty-state");
  if (placeholder) placeholder.remove();
  state.renderedSequences.add(event.sequence);
  timeline.append(createTimelineItem(event));
}

/**
 * 按父指针推导每个 span 的层级深度，供缩进渲染使用。
 *
 * @param {any[]} spans - 按 sequence 升序的 span 列表；父 span 一定先于子 span 出现。
 * @returns {Map<string, number>} span_id 到深度的映射；根为 0。
 */
function computeSpanDepths(spans) {
  const depthById = new Map();
  spans.forEach((span) => {
    // 父指针缺失或未知时退回 0：宁可把 span 画成根，也不要因为一条悬空指针整棵树都不渲染。
    const parentDepth = span.parent_span_id ? depthById.get(span.parent_span_id) : -1;
    depthById.set(span.span_id, parentDepth === undefined ? 0 : parentDepth + 1);
  });
  return depthById;
}

/**
 * 渲染 run-trace:v1 的 span 树：层级、状态、耗时占比和 ASCII 属性。
 *
 * @param {any} trace - RunTrace JSON；后端契约保证属性值不含自然语言，因此可以直接展示。
 * @returns {void} 通过 DOM 更新调用链列表与摘要行。
 */
function renderTrace(trace) {
  const list = document.getElementById("trace-list");
  const summary = document.getElementById("trace-summary");
  const spans = trace?.spans || [];
  if (!spans.length) {
    list.replaceChildren(Object.assign(document.createElement("li"), { className: "empty-state", textContent: "该 run 没有 span：可能在未开启采集的部署下执行。" }));
    summary.textContent = "调用链为空。";
    return;
  }
  const rootDuration = spans[0].duration_ms || Math.max(...spans.map((span) => span.duration_ms || 0)) || 1;
  const depths = computeSpanDepths(spans);
  list.replaceChildren(...spans.map((span) => {
    const item = document.createElement("li");
    item.className = "trace-item";
    item.dataset.status = span.status;
    item.style.marginLeft = `${Math.min(depths.get(span.span_id) || 0, 5) * 16}px`;
    const head = document.createElement("div");
    head.className = "trace-head";
    const label = document.createElement("span");
    label.className = "trace-name";
    label.textContent = span.name;
    const kind = document.createElement("span");
    kind.className = "trace-kind";
    kind.textContent = `${span.kind} · ${span.status}`;
    const duration = document.createElement("span");
    duration.className = "trace-duration";
    duration.textContent = `${(span.duration_ms || 0).toFixed(1)} ms`;
    head.append(label, kind, duration);
    const bar = document.createElement("div");
    bar.className = "trace-bar";
    const fill = document.createElement("span");
    fill.style.width = `${Math.min(100, ((span.duration_ms || 0) / rootDuration) * 100).toFixed(2)}%`;
    bar.append(fill);
    item.append(head, bar);
    const attributes = Object.entries(span.attributes || {});
    if (attributes.length) {
      const note = document.createElement("p");
      note.className = "trace-attributes";
      note.textContent = attributes.map(([key, value]) => `${key}=${value}`).join(" · ");
      item.append(note);
    }
    return item;
  }));
  const dropped = trace.dropped_span_count || 0;
  summary.textContent = `${spans.length} 个 span · 根耗时 ${rootDuration.toFixed(1)} ms · 丢弃 ${dropped} 个${dropped ? "（调用链不完整）" : ""}`;
}

/**
 * 读取当前 run 的调用链；404/503 只降级为提示文本，不影响已渲染的报告与时间线。
 *
 * @param {string} runId - 要读取调用链的持久化 run ID。
 * @returns {Promise<void>} 渲染完成或已显示降级提示时结束。
 */
async function refreshTrace(runId) {
  const summary = document.getElementById("trace-summary");
  try {
    const { payload } = await requestJson(`/api/v1/runs/${encodeURIComponent(runId)}/trace`);
    renderTrace(payload.trace);
  } catch (error) {
    summary.textContent = `调用链不可用：${error.message}`;
  }
}

/**
 * 渲染双 Agent 的审计裁决：放行/降级、Auditor 结论、已用返工次数和被否决的问题码。
 *
 * 只读取 `audited-report-workflow` 的结构化字段，不展示 AuditIssue.message 与 revision_instructions
 * 这类模型自然语言——未经放行的表述不应以"审计意见"的形式重新出现在页面上。传入 null 表示
 * 尚无报告结果，此时隐藏整张卡片而不是显示一组空占位符。
 *
 * @param {any|null} reportResult - DiagnosisRunResult.report 的公开 JSON，或 null 表示重置。
 * @returns {void} 通过 DOM 更新裁决摘要、问题清单和卡片可见性。
 */
function renderAuditVerdict(reportResult) {
  const card = document.getElementById("audit-card");
  const issues = document.getElementById("audit-issues");
  if (!reportResult) {
    card.hidden = true;
    card.dataset.outcome = "unknown";
    issues.replaceChildren();
    return;
  }
  const auditResult = reportResult.state?.audit_result || null;
  const outcome = reportResult.outcome || "unknown";
  card.hidden = false;
  card.dataset.outcome = outcome;
  document.getElementById("audit-outcome").textContent = outcome;
  document.getElementById("audit-status").textContent = auditResult?.status || "—";
  document.getElementById("audit-revisions").textContent =
    `${reportResult.state?.retry_count ?? 0} / 1`;
  const items = auditResult?.issues || [];
  document.getElementById("audit-issue-count").textContent = String(items.length);
  issues.replaceChildren(...items.map((issue) => {
    const entry = document.createElement("li");
    entry.className = "audit-issue";
    const code = document.createElement("span");
    code.className = "audit-issue-code";
    code.textContent = issue.code;
    const path = document.createElement("span");
    path.className = "audit-issue-path";
    path.textContent = issue.claim_path;
    entry.append(code, path);
    if (issue.evidence_refs?.length) {
      const refs = document.createElement("span");
      refs.className = "audit-issue-refs";
      refs.textContent = issue.evidence_refs.join(" · ");
      entry.append(refs);
    }
    return entry;
  }));
}

/**
 * 渲染结构化诊断报告的公开字段，保留不确定性和引用而不展开模型原始输出。
 * @param {any} result - DiagnosisRunResult JSON。
 */
function renderReport(result) {
  const report = result.report?.state?.draft_report;
  const grid = document.getElementById("report-grid");
  // 审计裁决先于报告正文渲染：读者必须先知道这份结论是被放行的还是被降级的，再去读它的内容。
  renderAuditVerdict(result.report);
  if (!report) return;
  const blocks = [
    ["Summary", report.summary],
    ["风险", report.risk],
    ["根因", report.root_cause?.title || report.root_cause],
    ["不确定性", report.uncertainties?.join("；")],
  ];
  grid.replaceChildren(...blocks.map(([title, value]) => {
    const card = document.createElement("article");
    card.className = "report-card";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const text = document.createElement("p");
    text.textContent = value || "暂无公开内容";
    card.append(heading, text);
    return card;
  }));
  // memory_stage 是后端持久化边界的公开投影；优先读取它，避免前端依赖内部 report state 的重复字段。
  renderMemoryCandidate(result.memory_stage?.memory || result.report?.state?.memory_candidate || null);
}

/**
 * 渲染 Auditor 通过后暂存的 CaseMemory 候选，并按状态决定是否显示用户决策按钮。
 *
 * @param {any|null} memory - DiagnosisRunResult.memory_stage.memory 的公开 JSON；不包含 embedding。
 * @returns {void} 通过 DOM 更新候选摘要和按钮可见性。
 */
function renderMemoryCandidate(memory) {
  const card = document.getElementById("memory-card");
  const actions = document.getElementById("memory-actions");
  const error = document.getElementById("memory-error");
  if (!card || !actions || !error) return;
  error.hidden = true;
  if (!memory) {
    state.memoryId = null;
    card.hidden = true;
    actions.hidden = true;
    return;
  }
  // 只保存 memory_id，后续决策仍通过同源 API 完成，避免把客户端状态当作数据库真相。
  state.memoryId = memory.memory_id;
  card.hidden = false;
  setText("memory-id", memory.memory_id);
  setText("memory-root-cause", memory.root_cause);
  setText("memory-components", (memory.components || []).join(", "));
  const status = document.getElementById("memory-status");
  status.textContent = memory.status || "pending";
  status.dataset.state = memory.status || "pending";
  // 只有 pending 可改变；confirmed/rejected 是服务端终态，按钮隐藏防止误导用户重复提交。
  actions.hidden = !["pending", "rejected"].includes(memory.status);
  [...actions.querySelectorAll("button")].forEach((button) => { button.disabled = false; });
}

/**
 * 将用户的 confirm/reject 决策提交给后端，并用服务端返回的 CaseMemory 刷新状态。
 *
 * @param {"confirm"|"reject"} decision - 有限的用户决策枚举，不能传递任意状态字符串。
 * @returns {Promise<void>} 请求成功后完成界面状态更新；失败时保留候选并显示错误。
 */
async function decideMemory(decision) {
  if (!state.memoryId) return;
  const error = document.getElementById("memory-error");
  const buttons = [...document.querySelectorAll(".memory-action")];
  buttons.forEach((button) => { button.disabled = true; });
  error.hidden = true;
  try {
    const { payload } = await requestJson(`/api/v1/memories/${encodeURIComponent(state.memoryId)}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    });
    renderMemoryCandidate(payload.memory);
  } catch (requestError) {
    error.textContent = `记忆决策失败：${requestError.message}`;
    error.hidden = false;
    buttons.forEach((button) => { button.disabled = false; });
  }
}

/**
 * 向服务端发送取消请求并刷新公开事件；取消是幂等的，失败时保留当前 run 供用户重试。
 * @returns {Promise<void>} 请求完成后的 UI 状态更新。
 */
async function cancelRun() {
  if (!state.runId) return;
  const button = document.getElementById("cancel-run");
  if (button) button.disabled = true;
  try {
    const { payload } = await requestJson(`/api/v1/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" });
    renderRun(payload.run);
    await refreshRun(state.runId);
  } catch (error) {
    document.getElementById("poll-message").textContent = `取消失败：${error.message}`;
    if (button) button.disabled = false;
  }
}

/**
 * 从 cancelled run 创建新的 queued run；新 run ID 会替换当前轮询目标。
 * @returns {Promise<void>} 新 run 已提交并开始轮询时完成。
 */
async function resumeRun() {
  if (!state.runId) return;
  const button = document.getElementById("resume-run");
  if (button) button.disabled = true;
  try {
    const { payload } = await requestJson(`/api/v1/runs/${encodeURIComponent(state.runId)}/resume`, { method: "POST" });
    state.runId = payload.run.run_id;
    state.pollAttempt = 0;
    // 恢复产生的是新 run，旧裁决对它不成立；清空后等新报告落地再渲染，不让上一轮的 degraded 悬留。
    renderAuditVerdict(null);
    renderRun(payload.run);
    startRunStream(state.runId);
  } catch (error) {
    document.getElementById("poll-message").textContent = `恢复失败：${error.message}`;
    if (button) button.disabled = false;
  }
}

/**
 * 永久删除当前案例记忆并隐藏卡片；后端事务负责证据和图节点级联清理。
 * @returns {Promise<void>} 删除完成后清空本地 memory ID。
 */
async function deleteMemory() {
  if (!state.memoryId) return;
  const error = document.getElementById("memory-error");
  try {
    await requestJson(`/api/v1/memories/${encodeURIComponent(state.memoryId)}`, { method: "DELETE" });
    renderMemoryCandidate(null);
  } catch (requestError) {
    error.textContent = `删除记忆失败：${requestError.message}`;
    error.hidden = false;
  }
}

/**
 * 读取 run 和 events；两者分开请求，保持状态快照与时间线的独立缓存边界。
 * @param {string} runId - 要读取的持久化 run ID。
 */
async function refreshRun(runId) {
  const [runResponse, eventResponse] = await Promise.all([
    requestJson(`/api/v1/runs/${encodeURIComponent(runId)}`),
    requestJson(`/api/v1/runs/${encodeURIComponent(runId)}/events`),
  ]);
  renderRun(runResponse.payload.run);
  renderEvents(eventResponse.payload);
  return runResponse.payload.run;
}

/**
 * 以递增退避轮询终态，避免在长时间模型调用期间制造请求风暴。
 * @param {string} runId - 要轮询的 run ID。
 */
async function pollRun(runId) {
  if (state.pollTimer) clearTimeout(state.pollTimer);
  try {
    const run = await refreshRun(runId);
    document.getElementById("poll-message").textContent = `已同步 · 第 ${state.pollAttempt + 1} 次`;
    if (!["completed", "failed", "cancelled"].includes(run.status)) {
      state.pollAttempt += 1;
      const delay = Math.min(4000, 600 + state.pollAttempt * 250);
      state.pollTimer = setTimeout(() => pollRun(runId), delay);
    } else {
      document.getElementById("poll-message").textContent = `run 已进入 ${run.status} 终态`;
      // 调用链只在终态读取一次：span 与 run 终态同事务写入，轮询期间读取只会拿到必然为空的结果。
      await refreshTrace(runId);
    }
  } catch (error) {
    document.getElementById("poll-message").textContent = `轮询失败：${error.message}`;
  }
}

/**
 * 关闭当前 SSE 连接并清空帧计数；重复调用是安全的。
 *
 * @returns {void} 连接释放后本地不再持有 EventSource 引用。
 */
function stopRunStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
  state.streamFrames = 0;
}

/**
 * 从推流退回退避轮询，并说明原因；轮询是等价读法，因此这不是错误状态。
 *
 * @param {string} runId - 需要继续观察的 run ID。
 * @param {string} reason - 展示给用户的中文原因，例如"连接超时"或"鉴权模式不支持推流"。
 * @returns {Promise<void>} 首次轮询完成后结束。
 */
async function fallbackToPolling(runId, reason) {
  stopRunStream();
  if (state.pollTimer) clearTimeout(state.pollTimer);
  state.pollAttempt = 0;
  document.getElementById("poll-message").textContent = `${reason}，已退回轮询`;
  await pollRun(runId);
}

/**
 * 用 `run-stream:v1` 增量订阅 run 状态与公开事件，失败时自动退回轮询。
 *
 * 事件帧直接追加渲染而不回查整条时间线（推流与 `/events` 同源，字段语义完全一致）；状态帧才
 * 重新读取 run 快照，因为报告正文只在终态出现，没必要每条事件都拉一次完整 run。首帧到达前就
 * 出错通常意味着这个部署根本不支持推流（bearer 鉴权、代理不转发 text/event-stream），此时立刻
 * 退回轮询；首帧之后的错误交给浏览器带 `Last-Event-ID` 自动重连，服务端据此续传而不重复推送。
 *
 * @param {string} runId - 要订阅的持久化 run ID。
 * @returns {void} 订阅已建立或已决定退回轮询时返回。
 */
function startRunStream(runId) {
  stopRunStream();
  if (state.pollTimer) clearTimeout(state.pollTimer);
  if (typeof EventSource === "undefined") {
    fallbackToPolling(runId, "浏览器不支持 SSE");
    return;
  }
  const message = document.getElementById("poll-message");
  const source = new EventSource(`/api/v1/runs/${encodeURIComponent(runId)}/stream`);
  state.stream = source;
  source.addEventListener("run_snapshot", (frame) => {
    state.streamFrames += 1;
    const payload = JSON.parse(frame.data);
    setRunState(payload.status);
    message.textContent = `实时推流 · 状态 ${payload.status}`;
    refreshRun(runId).catch(() => { message.textContent = "实时推流 · 快照读取失败"; });
  });
  source.addEventListener("run_event", (frame) => {
    state.streamFrames += 1;
    const payload = JSON.parse(frame.data);
    appendTimelineEvent(payload.event);
    message.textContent = `实时推流 · 已收到 ${state.renderedSequences.size} 条事件`;
  });
  source.addEventListener("stream_end", async (frame) => {
    state.streamFrames += 1;
    const payload = JSON.parse(frame.data);
    stopRunStream();
    if (["stream_timeout", "run_disappeared"].includes(payload.end_reason)) {
      // 连接级结束与 run 级结束必须区分：前者只说明这条连接到期，run 可能还在正常执行。
      await fallbackToPolling(runId, payload.end_reason === "stream_timeout" ? "推流连接到期" : "run 已不存在");
      return;
    }
    message.textContent = `run 已进入 ${payload.status} 终态（推流）`;
    await refreshRun(runId);
    await refreshTrace(runId);
  });
  source.onerror = () => {
    if (state.streamFrames === 0) {
      fallbackToPolling(runId, "推流不可用");
      return;
    }
    message.textContent = "推流中断 · 正在按最后事件序号自动重连";
  };
}

/**
 * 创建 session 并返回后端生成的资源快照。
 * @returns {Promise<any>} SessionCreateResponse JSON。
 */
async function createSession() {
  const title = document.getElementById("session-title").value.trim();
  const { payload } = await requestJson("/api/v1/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  state.sessionId = payload.session.session_id;
  setText("session-id", state.sessionId);
  return payload;
}

/**
 * 提交 message；409 不会被吞掉，而是展示 active_run_id 让用户先等待旧任务。
 * @returns {Promise<any>} MessageSubmissionResponse JSON。
 */
async function submitMessage() {
  if (!state.sessionId) await createSession();
  const question = document.getElementById("question").value.trim();
  const intent = document.getElementById("intent").value;
  const components = [...document.getElementById("component").selectedOptions].map((option) => option.value);
  const { payload } = await requestJson(`/api/v1/sessions/${encodeURIComponent(state.sessionId)}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: question, intent, components, history_trigger: "not_requested" }),
  });
  state.runId = payload.run.run_id;
  state.pollAttempt = 0;
  // 新 run 尚未产生报告时清空上一轮候选，避免用户把旧 memory 决策误认为当前 run 的结果。
  renderMemoryCandidate(null);
  renderAuditVerdict(null);
  renderEvents({ events: [] });
  renderRun(payload.run);
  startRunStream(state.runId);
}

/**
 * 绑定表单/按钮事件并启动首次 health 读取；初始化不会自动提交任何诊断。
 */
function init() {
  document.getElementById("refresh-health").addEventListener("click", refreshHealth);
  document.getElementById("refresh-events").addEventListener("click", () => state.runId && refreshRun(state.runId));
  document.getElementById("refresh-trace").addEventListener("click", () => state.runId && refreshTrace(state.runId));
  document.getElementById("confirm-memory").addEventListener("click", () => decideMemory("confirm"));
  document.getElementById("reject-memory").addEventListener("click", () => decideMemory("reject"));
  document.getElementById("cancel-run").addEventListener("click", cancelRun);
  document.getElementById("resume-run").addEventListener("click", resumeRun);
  document.getElementById("delete-memory").addEventListener("click", deleteMemory);
  document.getElementById("diagnosis-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("submit-diagnosis");
    const errorBox = document.getElementById("form-error");
    button.disabled = true;
    errorBox.hidden = true;
    try {
      await submitMessage();
    } catch (error) {
      errorBox.textContent = error.status === 409
        ? `当前 session 已有任务：${error.payload?.detail?.active_run_id || "请稍后重试"}`
        : error.message;
      errorBox.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
  refreshHealth();
}

init();
