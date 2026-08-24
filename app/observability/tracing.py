"""按 run 组织的持久化调用链遥测：span 契约、上下文绑定采集器与安全属性约束。

生产级多 Agent 系统必须能在事后解释"这一次 run 到底把 30 秒花在哪"，而进程级内存指标在 Worker
重启或多进程部署后立刻丢失，因此本模块把 span 建模为可落库、可通过 API 回放的一等数据。设计上刻意
只保留结构化事实：span 名称与属性值被正则限制为 ASCII 标识符字符，空格与 CJK 一律拒绝，因此一句
Prompt、Thought 或 Provider 原文在结构上就无法被塞进遥测，而不是依赖调用方自觉。

采集器通过 ``ContextVar`` 绑定，父子关系同样存放在 ``ContextVar`` 而不是共享栈：``asyncio.gather``
派生的并发任务各自复制上下文，因此并行工具调用会正确挂在同一个父 span 下，而不会互相覆盖栈顶。
未绑定采集器时 ``trace_span`` 是零成本 no-op，所以检索、MCP 与 Agent 代码可以无条件插桩，离线评测
与单元测试不需要额外准备遥测环境。
"""

from __future__ import annotations

import re
from asyncio import CancelledError
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import wraps
from hashlib import sha256
from time import perf_counter
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RUN_TRACE_CONTRACT_ID = "run-trace:v1"

#: 单次 run 的 span 上限。有界循环加并行工具调用不会接近该值，超限只会发生在插桩失控时；
#: 此时丢弃并公开计数，而不是让一次诊断把无界数据写进数据库。
MAX_SPANS_PER_RUN = 512
MAX_ATTRIBUTES_PER_SPAN = 12
MAX_ATTRIBUTE_VALUE_CHARS = 120

_SPAN_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{2,63}$")
_ATTRIBUTE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{1,39}$")
#: 属性值只允许 ASCII 标识符、路径与版本号常用字符。空格与 CJK 被排除，因此自然语言正文
#: （Prompt、Thought、日志原文）无法通过校验，敏感面由类型系统保证而不是靠代码评审。
_ATTRIBUTE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.:\-/+]+$")

AttributeValue = str | int | float | bool | None

#: LangGraph 节点签名各不相同（state、runtime、可选 writer），装饰器只需要"返回 awaitable 的可调用
#: 对象"这一最小约束；用 TypeVar 绑定而不是重写签名，包装后的类型与原节点保持一致。
NodeCallable = TypeVar("NodeCallable", bound=Callable[..., Awaitable[object]])


class TraceSpanKind(StrEnum):
    """把 span 归类到固定的架构边界，禁止调用方发明新的层级名称。

    枚举与三层嵌套 LangGraph、MCP 协议边界和两条检索通道一一对应，因此聚合视图天然按真实架构
    分组；自由字符串会让同一段耗时在不同 run 里落到不同类别，事后无法比较。``persistence`` 单列
    是因为数据库往返常常是首个可优化项，混进 ``node`` 会被工作流耗时掩盖。
    """

    WORKFLOW = "workflow"
    NODE = "node"
    REACT_STEP = "react_step"
    TOOL_CALL = "tool_call"
    RETRIEVAL = "retrieval"
    MODEL_CALL = "model_call"
    PERSISTENCE = "persistence"


class TraceSpanStatus(StrEnum):
    """区分正常结束、显式失败与被取消的 span，用于错误率而不是错误正文统计。

    只保留三个值：``error`` 由异常自动置位，``cancelled`` 对应 run 取消与超时这类外部中断。
    异常类型与消息刻意不进入遥测，因为它们可能携带资源名、SQL 片段或 Provider 响应正文。
    """

    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


def make_span_id(run_id: str, sequence: int) -> str:
    """按 run_id 与序号确定性派生 ``span_*`` 引用 ID，保证重放稳定且不依赖随机源。

    与 ``run_evt_*`` 事件 ID 使用同一套派生规则：同一次 run 重新导出时 span 引用不变，前端与
    评测脚本可以安全地把 ID 当作外键使用。随机 UUID 会让 Golden 回放每次产生不同引用，报告也就
    无法逐字比对。
    """

    if sequence < 1:
        raise ValueError("span sequence must start at 1")
    digest = sha256(f"{run_id}|{sequence}".encode()).hexdigest()[:16]
    return f"span_{digest}"


def normalize_span_attributes(
    attributes: Mapping[str, AttributeValue],
) -> dict[str, AttributeValue]:
    """校验并规范化 span 属性，拒绝越界键名、超长值和任何含空格或 CJK 的文本。

    这是遥测层唯一的写入口，因此把"不得泄露 Prompt/Thought/凭据"落实成可执行规则最划算：键名限定
    小写点分标识符，字符串值必须整体匹配 ASCII 标识符字符集，长度上界 120。布尔在 Python 中是 int
    的子类，必须先判断，否则 ``True`` 会被当成 1 写入并丢失语义。``None`` 保留为显式未知，避免用 0
    冒充"没有测到"。
    """

    if len(attributes) > MAX_ATTRIBUTES_PER_SPAN:
        raise ValueError(f"span accepts at most {MAX_ATTRIBUTES_PER_SPAN} attributes")
    normalized: dict[str, AttributeValue] = {}
    for key, value in attributes.items():
        if not _ATTRIBUTE_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"invalid span attribute key: {key!r}")
        if value is None or isinstance(value, bool):
            normalized[key] = value
            continue
        if isinstance(value, int | float):
            normalized[key] = value
            continue
        if not isinstance(value, str):
            raise TypeError(f"unsupported span attribute type for {key!r}")
        if len(value) > MAX_ATTRIBUTE_VALUE_CHARS:
            raise ValueError(f"span attribute {key!r} exceeds {MAX_ATTRIBUTE_VALUE_CHARS} chars")
        if not _ATTRIBUTE_VALUE_PATTERN.fullmatch(value):
            raise ValueError(f"span attribute {key!r} must be an identifier-like ASCII value")
        normalized[key] = value
    return normalized


class TraceSpan(BaseModel):
    """一次可落库、可回放的调用链片段：所属 run、父子关系、耗时、状态与安全属性。

    ``sequence`` 既是同 run 内的稳定排序键，也是 ``span_id`` 的派生输入，因此不需要额外的 trace ID
    体系：run_id 本身就是 trace 标识。``duration_ms`` 由单调时钟测得而不是两个墙钟时间戳相减，
    避免系统时间回拨产生负耗时；两个时间戳仍然保留，供前端按绝对时间轴渲染。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    span_id: str = Field(pattern=r"^span_[a-f0-9]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^span_[a-f0-9]{16}$")
    kind: TraceSpanKind
    name: str
    status: TraceSpanStatus
    started_at: datetime
    ended_at: datetime
    duration_ms: float = Field(ge=0)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """把 span 名称限制为小写点分标识符，禁止把中文说明或动态资源名写进名称。

        名称是聚合维度：一旦包含 run_id、表名或自然语言，指标基数会爆炸且不同 run 无法对齐。动态
        信息应放进属性，属性同样受 ASCII 约束，因此两条路径都不会承载自由文本。
        """

        if not _SPAN_NAME_PATTERN.fullmatch(value):
            raise ValueError("span name must be a lowercase dotted identifier")
        return value

    @field_validator("started_at", "ended_at")
    @classmethod
    def validate_timezone(cls, value: datetime) -> datetime:
        """要求两个时间戳都带时区，避免落库后无法判断是本地时间还是 UTC。

        naive datetime 在跨时区容器与本机开发之间会静默偏移，而 trace 的价值恰恰依赖时间轴可比；
        水位线时区案例已经说明这类错误只表现为"结果看起来正常但结论错误"。
        """

        if value.tzinfo is None:
            raise ValueError("span timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_consistency(self) -> TraceSpan:
        """校验 span_id 由 run_id/sequence 派生、父子不自引用且结束不早于开始。

        span_id 可推导意味着任何一条从数据库读回的记录都能被重新验证，篡改或错误关联会立刻暴露；
        自引用父指针会让前端的树形渲染陷入死循环，必须在契约层拒绝而不是在 UI 里防御。
        """

        if self.span_id != make_span_id(self.run_id, self.sequence):
            raise ValueError("span_id must be derived from run_id and sequence")
        if self.parent_span_id == self.span_id:
            raise ValueError("span must not be its own parent")
        if self.ended_at < self.started_at:
            raise ValueError("span ended_at must not precede started_at")
        return self


class RunTrace(BaseModel):
    """单次 run 的完整调用链：连续序号、唯一根 span 和可解析的父指针。

    校验刻意严格，因为 trace 一旦缺号或出现悬空父指针，前端只能画出一棵残树，而使用者很难分辨
    "系统没做这一步"和"这一步的 span 丢了"。``dropped_span_count`` 显式公开被上限截断的数量，
    保证残缺状态是可读事实而不是静默沉默。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["run-trace:v1"] = RUN_TRACE_CONTRACT_ID
    run_id: str = Field(min_length=1, max_length=128)
    spans: tuple[TraceSpan, ...] = ()
    dropped_span_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_structure(self) -> RunTrace:
        """校验序号从 1 连续递增、全部 span 同属该 run、父 span 先于子 span 且根唯一。

        父必须先出现是排序结果的自然推论（span 在开始时就分配序号），把它写成断言可以在插桩把
        ContextVar 传播搞错时立刻失败；否则错误只会表现为火焰图层级看起来"有点怪"。
        """

        seen: set[str] = set()
        roots = 0
        for index, span in enumerate(self.spans, start=1):
            if span.run_id != self.run_id:
                raise ValueError("all spans must belong to the trace run")
            if span.sequence != index:
                raise ValueError("span sequences must be consecutive starting at 1")
            if span.parent_span_id is None:
                roots += 1
            elif span.parent_span_id not in seen:
                raise ValueError("span parent must appear before the child span")
            seen.add(span.span_id)
        if self.spans and roots != 1:
            raise ValueError("a non-empty trace must contain exactly one root span")
        return self

    @property
    def total_duration_ms(self) -> float:
        """返回根 span 的耗时作为整次 run 的端到端时长，空 trace 返回 0。

        端到端时长必须取根 span 而不是所有 span 求和：子 span 与父 span 在时间上重叠，求和会把
        30 秒的 run 报成上百秒，直接破坏 P95 口径。
        """

        return self.spans[0].duration_ms if self.spans else 0.0

    def spans_by_kind(self, kind: TraceSpanKind) -> tuple[TraceSpan, ...]:
        """筛出指定层级的 span，供评测断言与 Prometheus 聚合复用同一份数据。

        提供这一层而不是让调用方各写推导式，是为了让"按架构边界聚合"成为契约的一部分；测试与
        指标导出使用完全相同的筛选语义，指标口径就不会与断言口径分叉。
        """

        return tuple(span for span in self.spans if span.kind is kind)


class SpanHandle:
    """进行中 span 的可写句柄：允许补充属性或显式改写终态，但不允许改名或改父。

    句柄故意不暴露耗时与时间戳，避免调用方"帮忙"写入自己测的数字导致口径不一致。当采集器未绑定或
    span 已超过上限时，句柄退化为惰性对象，所有方法都是 no-op，因此插桩代码不需要写任何分支判断。
    """

    __slots__ = ("_attributes", "_status", "sequence", "span_id")

    def __init__(
        self,
        sequence: int | None = None,
        span_id: str | None = None,
        attributes: Mapping[str, AttributeValue] | None = None,
    ) -> None:
        """初始化句柄；三个参数全部缺省即构造出永不产生 span 的惰性句柄。

        属性在构造时就规范化一次，让非法键值在实际业务代码位置立即失败，而不是等到 span 结束后
        才在采集器内部报错——那时异常栈已经指不回真正的插桩点。
        """

        self.sequence = sequence
        self.span_id = span_id
        self._attributes = dict(normalize_span_attributes(attributes or {}))
        self._status = TraceSpanStatus.OK

    @property
    def is_recording(self) -> bool:
        """说明该句柄是否真的会产出 span，供少数需要跳过昂贵取值的插桩点判断。

        绝大多数调用点不必检查：``annotate`` 本身已经是 no-op。只有当属性值需要额外计算（例如统计
        字节数）时才值得先看这个标志，避免为不会落库的 span 付出计算成本。
        """

        return self.span_id is not None

    def annotate(self, **attributes: AttributeValue) -> None:
        """在 span 结束前追加安全属性；惰性句柄直接忽略。

        多数有价值的属性（工具是否命中缓存、召回条数、是否降级）只有在执行完之后才知道，因此必须
        允许后补。合并后仍受同一套键名、类型和条数上限约束，不给遥测留旁路。
        """

        if self.span_id is None:
            return
        merged = {**self._attributes, **attributes}
        self._attributes = dict(normalize_span_attributes(merged))

    def mark(self, status: TraceSpanStatus) -> None:
        """显式改写终态，用于"没有抛异常但业务上已降级/取消"的情况。

        重排降级、Auditor 否决和 run 取消都不会抛异常，却是需要在 trace 上看见的失败形态；若只依赖
        异常判定状态，这些路径会全部记成 ``ok``，错误率指标随之失去意义。
        """

        if self.span_id is None:
            return
        self._status = status

    def _snapshot(self) -> tuple[TraceSpanStatus, dict[str, AttributeValue]]:
        """把当前状态与属性交给采集器构造不可变 span，仅供本模块内部使用。

        采集器不直接读私有字段，是为了让"句柄可写、span 不可变"的边界在代码里显式存在，后续把
        span 导出到 OTLP 时也只需要替换采集器实现。
        """

        return self._status, dict(self._attributes)


_INERT_SPAN = SpanHandle()


class RunTraceCollector:
    """按 run 收集 span：开始时分配序号，结束时冻结为不可变记录，超限则丢弃并计数。

    序号在 span 开始时分配、记录在结束时写入，因此内部用字典按序号存放而不是直接 append——并发工具
    调用的完成顺序与开始顺序不同，直接 append 会让父 span 出现在子 span 之后，破坏 ``RunTrace``
    的父先于子约束。采集器不做任何 I/O，落库由持久化层在 run 终态的同一事务里完成。
    """

    def __init__(self, run_id: str, *, max_spans: int = MAX_SPANS_PER_RUN) -> None:
        """绑定 run 标识与 span 上限，并初始化序号计数与丢弃计数。

        上限可覆盖主要是为了让测试用很小的值验证截断行为；生产使用默认值，因为有界 ReAct 循环的
        span 数量本身就有上界，触达上限只可能意味着插桩递归失控。
        """

        if not run_id:
            raise ValueError("run_id must not be blank")
        if max_spans < 1:
            raise ValueError("max_spans must be positive")
        self._run_id = run_id
        self._max_spans = max_spans
        self._next_sequence = 1
        self._completed: dict[int, TraceSpan] = {}
        self._dropped_span_count = 0

    @property
    def run_id(self) -> str:
        """返回该采集器绑定的 run 标识，供落库与断言核对归属。

        暴露为只读属性而不是公开字段，避免调用方在 run 执行中途改写归属，导致一份 trace 里混入
        两次 run 的 span 而校验又恰好通过。
        """

        return self._run_id

    @property
    def dropped_span_count(self) -> int:
        """返回因超过上限而被丢弃的 span 数量，用于让残缺 trace 自我暴露。

        丢弃是有意的保护动作，但必须可见：使用者看到非零值时会知道火焰图不完整，而不是误判系统
        真的只执行了这些步骤。
        """

        return self._dropped_span_count

    def _reserve_sequence(self) -> int | None:
        """分配下一个序号；已达上限时返回 ``None`` 并累加丢弃计数。

        计数器只增不减，因此即使某个 span 因异常提前结束，序号也不会被复用——复用会让两个 span
        派生出同一个 ``span_id``，前端的父子关联随之错乱。
        """

        if self._next_sequence > self._max_spans:
            self._dropped_span_count += 1
            return None
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    @contextmanager
    def open_span(
        self,
        kind: TraceSpanKind,
        name: str,
        *,
        parent_span_id: str | None = None,
        **attributes: AttributeValue,
    ) -> Iterator[SpanHandle]:
        """打开一个 span，正常退出记为 ok，异常记为 error，取消记为 cancelled 并原样重抛。

        用同步上下文管理器而不是 async 版本，是因为本体不做任何 await：同步实现同样可以在 async 函数
        里使用，却避免为每个 span 额外创建一个协程对象。异常必须重抛，遥测绝不能吞掉控制流。
        """

        sequence = self._reserve_sequence()
        if sequence is None:
            yield _INERT_SPAN
            return
        handle = SpanHandle(sequence, make_span_id(self._run_id, sequence), attributes)
        started_at = datetime.now(UTC)
        started_perf = perf_counter()
        try:
            yield handle
        except CancelledError:
            # 取消是外部中断而不是缺陷，单独归类才能让错误率指标只反映真实故障。
            self._finish(
                handle, kind, name, parent_span_id, started_at, started_perf, cancelled=True
            )
            raise
        except Exception:
            self._finish(handle, kind, name, parent_span_id, started_at, started_perf, failed=True)
            raise
        else:
            self._finish(handle, kind, name, parent_span_id, started_at, started_perf)

    def _finish(
        self,
        handle: SpanHandle,
        kind: TraceSpanKind,
        name: str,
        parent_span_id: str | None,
        started_at: datetime,
        started_perf: float,
        *,
        failed: bool = False,
        cancelled: bool = False,
    ) -> None:
        """把句柄冻结为不可变 span 并按序号存放，同时把孤立 span 挂到根 span 之下。

        耗时取单调时钟差值，墙钟只用于时间轴展示，因此系统时间调整不会产生负耗时。无父指针且序号
        不为 1 的 span 一律挂到根，避免任何插桩疏漏把一次 run 的 trace 变成互不相连的森林——那会让
        导出结果直接违反 ``RunTrace`` 的唯一根约束，一次插桩小错误就会连带毁掉整个 trace 接口。
        """

        if handle.span_id is None or handle.sequence is None:
            return
        duration_ms = max((perf_counter() - started_perf) * 1000, 0.0)
        status, attributes = handle._snapshot()
        if failed:
            status = TraceSpanStatus.ERROR
        elif cancelled:
            status = TraceSpanStatus.CANCELLED
        effective_parent = parent_span_id
        if effective_parent is None and handle.sequence != 1:
            effective_parent = make_span_id(self._run_id, 1)
        self._completed[handle.sequence] = TraceSpan(
            run_id=self._run_id,
            sequence=handle.sequence,
            span_id=handle.span_id,
            parent_span_id=effective_parent,
            kind=kind,
            name=name,
            status=status,
            started_at=started_at,
            ended_at=datetime.now(UTC),
            duration_ms=duration_ms,
            attributes=attributes,
        )

    def record_span(
        self,
        kind: TraceSpanKind,
        name: str,
        *,
        duration_ms: float,
        status: TraceSpanStatus = TraceSpanStatus.OK,
        parent_span_id: str | None = None,
        **attributes: AttributeValue,
    ) -> None:
        """记录一个耗时已由别处测量完成的 span，用于桥接非上下文管理器式的既有计时器。

        模型 Provider 的计时器跨越 ``__init__`` 与多个 ``finish`` 分支，无法改写成一个 with 块；
        与其为遥测重构可靠性关键的异常分支，不如接受外部耗时。开始时间由结束时间回推，只用于时间轴
        展示，真正参与聚合的仍是调用方测得的 ``duration_ms``。
        """

        sequence = self._reserve_sequence()
        if sequence is None:
            return
        safe_duration = max(float(duration_ms), 0.0)
        ended_at = datetime.now(UTC)
        effective_parent = parent_span_id
        if effective_parent is None and sequence != 1:
            effective_parent = make_span_id(self._run_id, 1)
        self._completed[sequence] = TraceSpan(
            run_id=self._run_id,
            sequence=sequence,
            span_id=make_span_id(self._run_id, sequence),
            parent_span_id=effective_parent,
            kind=kind,
            name=name,
            status=status,
            started_at=ended_at - timedelta(milliseconds=safe_duration),
            ended_at=ended_at,
            duration_ms=safe_duration,
            attributes=normalize_span_attributes(attributes),
        )

    def snapshot(self) -> RunTrace:
        """按开始顺序导出 trace；若存在未完成序号，压实序号并同步改写父指针。

        序号在开始时分配、记录在结束时写入，因此一个被强制中断且未走完 ``finally`` 的 span 会留下
        空洞。空洞会直接违反"序号从 1 连续"的契约，让整个 trace 接口对该 run 报错，因此这里在导出
        阶段压实：保留原有开始顺序，重新派生 ``span_id``，并用旧新映射修正每个父指针。正常路径下
        映射是恒等的，压实不产生任何差异。
        """

        ordered = [self._completed[key] for key in sorted(self._completed)]
        remapped: dict[str, str] = {}
        compacted: list[TraceSpan] = []
        for index, span in enumerate(ordered, start=1):
            new_span_id = make_span_id(self._run_id, index)
            remapped[span.span_id] = new_span_id
            parent = span.parent_span_id
            # 父 span 一定先被压实，因此映射必然已经存在；缺失说明父 span 未完成，此时挂到根。
            if parent is not None:
                parent = remapped.get(parent, make_span_id(self._run_id, 1))
            if index == 1:
                parent = None
            compacted.append(
                span.model_copy(
                    update={
                        "sequence": index,
                        "span_id": new_span_id,
                        "parent_span_id": parent,
                    }
                )
            )
        return RunTrace(
            run_id=self._run_id,
            spans=tuple(compacted),
            dropped_span_count=self._dropped_span_count,
        )


_CURRENT_COLLECTOR: ContextVar[RunTraceCollector | None] = ContextVar(
    "dataops_run_trace_collector", default=None
)
#: 当前父 span 也放在 ContextVar 里而不是采集器内部的栈：``asyncio.gather`` 会为每个协程复制上下文，
#: 因此并行工具调用天然共享同一个父 span，而共享栈会被并发的 push/pop 互相破坏。
_CURRENT_PARENT: ContextVar[str | None] = ContextVar("dataops_run_trace_parent", default=None)


def bind_run_trace_collector(collector: RunTraceCollector) -> Token[RunTraceCollector | None]:
    """把采集器绑定到当前异步上下文，返回用于恢复的 Token。

    与模型调用遥测一致采用显式绑定/恢复而不是全局单例：Worker 在同一进程里串行执行多个 run，全局
    状态会让上一次 run 的 span 泄漏到下一次；返回 Token 也让嵌套绑定（例如评测里包一层）可还原。
    """

    return _CURRENT_COLLECTOR.set(collector)


def reset_run_trace_collector(token: Token[RunTraceCollector | None]) -> None:
    """恢复绑定前的采集器，必须在 ``finally`` 中调用以避免上下文泄漏。

    单独提供函数而不是让调用方直接操作 ContextVar，是为了让"绑定"与"恢复"在代码搜索中成对出现，
    漏掉恢复会让后续 run 的 span 写进上一个 run 的 trace。
    """

    _CURRENT_COLLECTOR.reset(token)


def current_run_trace_collector() -> RunTraceCollector | None:
    """返回当前上下文绑定的采集器，未绑定时为 ``None``。

    持久化层用它判断本次 run 是否需要落库 trace；未绑定是完全合法的状态，例如离线评测脚本和单元
    测试并不需要遥测，因此这里返回 ``None`` 而不是抛错。
    """

    return _CURRENT_COLLECTOR.get()


@contextmanager
def trace_span(
    kind: TraceSpanKind,
    name: str,
    **attributes: AttributeValue,
) -> Iterator[SpanHandle]:
    """在当前上下文记录一个 span，未绑定采集器时退化为零成本 no-op。

    这是业务代码唯一需要用到的入口：父子关系由 ContextVar 自动推导，因此检索、MCP 与 Agent 模块
    不必互相传递 span 参数，也不需要判断遥测是否开启。no-op 路径保证离线评测和单元测试不受影响。
    """

    collector = _CURRENT_COLLECTOR.get()
    if collector is None:
        yield _INERT_SPAN
        return
    parent_span_id = _CURRENT_PARENT.get()
    with collector.open_span(kind, name, parent_span_id=parent_span_id, **attributes) as handle:
        if handle.span_id is None:
            yield handle
            return
        token = _CURRENT_PARENT.set(handle.span_id)
        try:
            yield handle
        finally:
            # 必须在 span 结束前恢复父指针，否则同级后继 span 会错误地挂到刚结束的兄弟节点下。
            _CURRENT_PARENT.reset(token)


def traced_node(
    name: str,
    kind: TraceSpanKind = TraceSpanKind.NODE,
) -> Callable[[NodeCallable], NodeCallable]:
    """把 LangGraph 异步节点包装成自动计时的 span，而不侵入节点函数体。

    节点的价值信息是"哪一层慢、哪一层抛错"，用装饰器在注册处包一层即可，既不用给每个节点缩进
    一个 with 块，也不会让节点逻辑与遥测耦合。需要写属性的地方仍然直接用 `trace_span`，因此本
    装饰器刻意不暴露 handle：节点内部真正想标注的往往是模型调用而不是整个节点。
    """

    def decorator(node: NodeCallable) -> NodeCallable:
        """返回同签名协程包装器，保留原函数名以便 LangGraph 拓扑与堆栈信息保持可读。

        用 ``wraps`` 而不是新建函数名，是因为 LangGraph 的错误信息与调试快照会显示被包装对象的名字；
        丢掉原名会让"哪个节点抛错"这类问题需要额外一次源码定位。
        """

        @wraps(node)
        async def wrapper(*args: object, **kwargs: object) -> object:
            """在 span 内执行原节点，并把返回值原样透传给 LangGraph 状态归并。

            异常沿用 ``open_span`` 的 error/cancelled 归类后继续传播：遥测不改变控制流，否则一个
            观测层缺陷会伪装成业务节点成功，比缺少 trace 危险得多。
            """

            with trace_span(kind, name):
                return await node(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def record_completed_span(
    kind: TraceSpanKind,
    name: str,
    *,
    duration_ms: float,
    status: TraceSpanStatus = TraceSpanStatus.OK,
    **attributes: AttributeValue,
) -> None:
    """把外部计时器测得的一次调用写入当前 trace，未绑定采集器时静默跳过。

    与 `trace_span` 的分工是：能用 with 包住的调用一律用 `trace_span`，只有像模型 Provider 那样
    "开始与结束分散在不同方法、结束分支多达六个"的既有计时器才走这里，从而避免为遥测改写可靠性
    关键路径。父指针同样取自 ContextVar，因此桥接进来的 span 仍会挂在正确的 Agent 节点下。
    """

    collector = _CURRENT_COLLECTOR.get()
    if collector is None:
        return
    collector.record_span(
        kind,
        name,
        duration_ms=duration_ms,
        status=status,
        parent_span_id=_CURRENT_PARENT.get(),
        **attributes,
    )
