# DataOps Troubleshooter 项目指引

- 编码实现优先使用机器可读的产品基线 `docs/product-design.md`；`docs/DataOps_Troubleshooter_产品设计文档_v2.0.docx` 是内容一致的正式阅读版。涉及需求、架构、数据模型、接口、里程碑或验收口径时，先检索 Markdown 的相关章节。
- 规划、实现、重构、测试或评测本项目时，必须使用仓库级 Skill：`.agents/skills/build-dataops-troubleshooter/SKILL.md`。
- ReAct、GraphRAG、Planner/Auditor 双 Agent、经审计的长期记忆和真实 MCP 协议边界是不可删减能力；轻量化应通过收敛场景、Agent 数量和基础设施实现。
- Planner 必须使用有界 ReAct 循环：隐藏式 Reason、结构化 Action、标准化 Observation；只记录决策摘要、工具事件和证据，不保存或展示模型原始思维链。
- 修改 Planner ReAct Prompt、GraphRAG 实体/关系抽取或相关输出 Schema 时，先读取 `docs/prompt-contracts.md`，并同步更新对应测试和 Prompt 版本。
- 运行时领域能力放在 `app/capabilities/`；它们是 Prompt 片段、工具优先级和校验规则，不是新的 Agent，也不要与仓库级 `.agents/skills/` 混淆。
- 历史案例匹配是第五个必选 capability；默认只召回已确认案例，并始终让本次实时 Observation 优先。
- MCP 工具清单以 `docs/product-design.md` 第 6.1 节的 9 个名称为准；不得静默改名、合并或删减。
- 调整依赖、目录、工具数量、检索流程、记忆写入或开发顺序时，先查阅 `docs/reference-adoption.md`，不要重新引入其中已明确收敛或暂缓的设计。
- 仅使用脱敏、合成或 Mock 数据，不接入原单位生产系统、真实日志、内部域名、凭据或未公开接口。
- 采用可验证的垂直切片推进开发；每次变更都给出验收条件，并运行与风险相称的测试。
- 本项目以学习和求职展示为核心目的。所有人工编写的代码、配置、测试和脚本必须包含详细说明，至少解释文件职责、核心技术原理、数据流、关键设计取舍、失败路径和验证方式；新增技术或架构决策时必须同步更新 `docs/implementation-guide.md`。每个类、函数、异步函数、方法和测试函数都必须有函数级 docstring，说明输入输出、实现原理、边界与失败语义；复杂函数还必须在函数体的关键步骤旁写内联注释，解释校验顺序、数据转换、重试、事务、资源释放和安全取舍。文件开头的统一说明不能替代 callable 级说明。注释重点解释“为什么这样设计”和“边界如何保证”，不得只逐行复述代码。JSON、锁文件、图片、DOCX 等不支持注释或机器生成的文件，通过相邻 README、Schema 测试和 `docs/implementation-guide.md` 提供对应说明，不手工篡改生成内容。
- 学习型说明是完成定义的一部分：缺少模块级 docstring、任一 callable 的解释性 docstring、关键步骤内联注释、技术原理文档或相应测试时，不得声称切片完成。
- 冲突优先级：当前用户明确要求 > `docs/product-design.md` > 已有测试和对外接口契约 > 当前代码 > Skill 默认约束。若 Markdown 与 DOCX 出现内容差异，先停止扩展并同步两份文档。
