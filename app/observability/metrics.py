"""把已落库的 run 状态与 span 耗时聚合渲染成 Prometheus 文本曝光格式。

模块刻意不依赖 `prometheus_client`：需要暴露的只是若干计数器和一个 max gauge，自己拼字符串比引入
一个带全局注册表的库更可控——全局注册表在多 Worker、多次 lifespan 的测试里会出现重复注册错误。
指标口径全部来自数据库聚合而不是进程内计数器，因为 API 与 Worker 可能是不同进程且都会重启，进程
内计数会在重启后归零并伪造出"错误率骤降"的假象。标签值只允许小写标识符字符，与 span 契约同源，
因此不需要转义逻辑，也不可能把自然语言正文写进标签。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RUNTIME_METRICS_CONTRACT_ID = "runtime-metrics:v1"

#: 标签值白名单。span 名与 kind 已由遥测契约限制为该字符集，run 状态来自枚举，因此渲染阶段
#: 不需要 Prometheus 转义规则；出现非法值说明有人绕过 ORM 手工写库，此时应显式失败而不是渲染。
_LABEL_VALUE_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{1,63}$")


class SpanMetric(BaseModel):
    """表示同一 ``kind`` + ``name`` 分组的 span 次数、总耗时、最大耗时与错误数。

    只保留计数、总和与最大值三个可加/可比较的量，而不是分位数：分位数无法跨实例合并，Prometheus
    侧应由 histogram 完成。总和加次数足以算平均耗时，最大值用于暴露长尾，错误数用于算失败率，
    这三者已能回答"哪一层慢、哪一层不稳"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=64)
    count: int = Field(ge=0)
    duration_ms_sum: float = Field(ge=0)
    duration_ms_max: float = Field(ge=0)
    error_count: int = Field(ge=0)

    @field_validator("kind", "name")
    @classmethod
    def validate_label(cls, value: str) -> str:
        """拒绝无法安全写入 Prometheus 标签的值，把校验前移到构造期。

        渲染函数因此可以无条件拼接字符串；如果非法值能进到渲染阶段，抓取端会得到一份语法损坏的
        曝光文本，整个 job 的所有指标都会被丢弃，故障范围远大于拒绝单条记录。
        """

        if not _LABEL_VALUE_PATTERN.fullmatch(value):
            raise ValueError(f"metric label value is not exposition-safe: {value}")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> SpanMetric:
        """确保错误数不超过总次数，避免聚合 SQL 写错时算出大于 1 的失败率。

        该不变量在数据库层没有对应约束（错误数由 FILTER 子句派生），因此必须在契约边界兜住；
        失败率一旦超过 1，看板与告警阈值会同时失去意义。
        """

        if self.error_count > self.count:
            raise ValueError("span error count cannot exceed total count")
        return self


class RuntimeMetricsSnapshot(BaseModel):
    """表示一次抓取时刻的 run 状态分布与各层 span 聚合。

    快照是不可变值对象，因此可以在渲染前断言、也可以直接作为测试期望；它不携带时间戳，因为
    Prometheus 以抓取时间为准，自带时间戳反而会在补抓时产生乱序样本。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Literal["runtime-metrics:v1"] = RUNTIME_METRICS_CONTRACT_ID
    run_counts: dict[str, int] = Field(default_factory=dict)
    spans: tuple[SpanMetric, ...] = ()

    @field_validator("run_counts")
    @classmethod
    def validate_run_counts(cls, value: dict[str, int]) -> dict[str, int]:
        """校验 run 状态标签合法且计数非负，使非法状态无法进入曝光文本。

        run 状态由数据库 CHECK 约束限定，这里的重复校验成本极低却能保证渲染函数无需防御性分支；
        负计数只可能来自聚合 SQL 缺陷，静默通过会让"运行中的 run 数"变成无意义的负值。
        """

        for status, count in value.items():
            if not _LABEL_VALUE_PATTERN.fullmatch(status):
                raise ValueError(f"run status label is not exposition-safe: {status}")
            if count < 0:
                raise ValueError("run status count must not be negative")
        return value


def render_prometheus_text(snapshot: RuntimeMetricsSnapshot) -> str:
    """把快照渲染为 text/plain; version=0.0.4 曝光内容，指标顺序稳定可直接断言。

    每个指标族只写一次 HELP/TYPE，样本按标签排序，因此两次抓取的文本差异只来自数值变化，便于
    在测试和演示里逐行比对。耗时单位统一用毫秒并写进指标名（``_ms``），避免与 Prometheus 生态
    默认的秒制静默混用——单位歧义是这类自制 exporter 最常见且最难察觉的错误。
    """

    lines: list[str] = [
        "# HELP dataops_runs_total Persisted diagnosis runs grouped by lifecycle status.",
        "# TYPE dataops_runs_total gauge",
    ]
    for status in sorted(snapshot.run_counts):
        lines.append(f'dataops_runs_total{{status="{status}"}} {snapshot.run_counts[status]}')

    ordered_spans = sorted(snapshot.spans, key=lambda metric: (metric.kind, metric.name))
    lines.extend(
        [
            "# HELP dataops_span_count Completed trace spans grouped by architecture layer.",
            "# TYPE dataops_span_count counter",
        ]
    )
    for metric in ordered_spans:
        labels = f'{{kind="{metric.kind}",name="{metric.name}"}}'
        lines.append(f"dataops_span_count{labels} {metric.count}")
    lines.extend(
        [
            "# HELP dataops_span_error_count Trace spans that ended with error status.",
            "# TYPE dataops_span_error_count counter",
        ]
    )
    for metric in ordered_spans:
        labels = f'{{kind="{metric.kind}",name="{metric.name}"}}'
        lines.append(f"dataops_span_error_count{labels} {metric.error_count}")
    lines.extend(
        [
            "# HELP dataops_span_duration_ms_sum Total span duration in milliseconds.",
            "# TYPE dataops_span_duration_ms_sum counter",
        ]
    )
    for metric in ordered_spans:
        labels = f'{{kind="{metric.kind}",name="{metric.name}"}}'
        lines.append(f"dataops_span_duration_ms_sum{labels} {metric.duration_ms_sum:.3f}")
    lines.extend(
        [
            "# HELP dataops_span_duration_ms_max Slowest observed span duration in milliseconds.",
            "# TYPE dataops_span_duration_ms_max gauge",
        ]
    )
    for metric in ordered_spans:
        labels = f'{{kind="{metric.kind}",name="{metric.name}"}}'
        lines.append(f"dataops_span_duration_ms_max{labels} {metric.duration_ms_max:.3f}")
    # 曝光格式要求以换行结尾；缺少末行换行会让部分抓取端丢弃最后一个样本。
    return "\n".join(lines) + "\n"
