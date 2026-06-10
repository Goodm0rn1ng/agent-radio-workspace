"""Pipeline 运行指标收集与落盘。

设计目标：
- 每次 pipeline 跑完写一行 JSON 到 `data/logs/metrics.jsonl`（append-only）
- 各步骤耗时用 context manager 自动测量
- 超过阈值自动 logger.warning（聚合到 errors 字段）
- 不引入新依赖，pydantic 已用

字段约定（向后兼容时只加不删）：
- run_id          : "20260516-022054" 时间戳
- started_at      : ISO 8601
- duration_s      : 总耗时秒
- source          : "oneshot" / "video" / "live_recording" / "resummarize"
- program_name    : 用户传入的节目标题
- air_date        : YYYY-MM-DD
- audio_duration_s: 节目音频时长（由 segmenter 测得）
- segments_count  : transcript 总段数
- batches_count   : 翻译批次数
- sections_count  : Summary.sections 数
- library_hits    : 命中常驻环节库数
- library_added   : 自动入库新增数
- input_tokens    : 本次模型调用输入 token 总数
- output_tokens   : 本次模型调用输出 token 总数
- total_tokens    : 本次模型调用 token 总数
- token_usage     : {stage.provider: token totals}
- step_durations  : {step_name: seconds}
- errors          : list of error strings
- warnings        : list of warning strings (e.g. 超时)
- success         : bool
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

# 各阶段建议耗时上限（超过会写一条 warning）
DEFAULT_THRESHOLDS_S: dict[str, float] = {
    "segment_audio": 60.0,
    "transcribe_segments": 600.0,
    "translate_segments": 900.0,
    "summarize": 120.0,
    "send_to_telegram": 30.0,
    "video_source": 180.0,
}


class TokenUsage(BaseModel):
    """模型调用的 token 统计。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class PipelineMetrics(BaseModel):
    """一次 pipeline 跑的指标快照。"""

    run_id: str
    started_at: str
    duration_s: float = 0.0
    source: str = "unknown"
    program_name: str = ""
    air_date: str = ""
    audio_duration_s: float = 0.0
    segments_count: int = 0
    batches_count: int = 0
    sections_count: int = 0
    library_hits: int = 0
    library_added: int = 0
    telegram_messages_sent: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_usage: dict[str, TokenUsage] = Field(default_factory=dict)
    step_durations: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    success: bool = False


class MetricsCollector:
    """pipeline 运行期间累积指标，运行结束 flush 到 jsonl。"""

    def __init__(
        self,
        *,
        source: str,
        program_name: str = "",
        air_date: str = "",
        run_id: str | None = None,
        thresholds: dict[str, float] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self._start = time.monotonic()
        self._thresholds = {**DEFAULT_THRESHOLDS_S, **(thresholds or {})}
        self.metrics = PipelineMetrics(
            run_id=run_id or now.strftime("%Y%m%d-%H%M%S"),
            started_at=now.isoformat(timespec="seconds"),
            source=source,
            program_name=program_name,
            air_date=air_date,
        )

    @contextmanager
    def step(self, name: str):
        """测一个步骤的耗时；超阈值记 warning。"""
        t0 = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - t0
            # 多次同名调用累加（如 STT 切片有多次）
            self.metrics.step_durations[name] = (
                self.metrics.step_durations.get(name, 0.0) + elapsed
            )
            threshold = self._thresholds.get(name)
            if threshold and elapsed > threshold:
                msg = (
                    f"{name} 耗时 {elapsed:.1f}s 超过阈值 {threshold:.0f}s"
                )
                self.metrics.warnings.append(msg)
                logger.warning(f"⏱️ {msg}")

    def add_warning(self, msg: str) -> None:
        self.metrics.warnings.append(msg)
        logger.warning(msg)

    def add_error(self, msg: str) -> None:
        self.metrics.errors.append(msg)

    def add_token_usage(self, label: str, usage: TokenUsage) -> None:
        """累加一次模型调用 token；空 usage 忽略。"""
        if (
            usage.input_tokens <= 0
            and usage.output_tokens <= 0
            and usage.total_tokens <= 0
        ):
            return

        total = usage.total_tokens or usage.input_tokens + usage.output_tokens
        current = self.metrics.token_usage.get(label, TokenUsage())
        self.metrics.token_usage[label] = TokenUsage(
            input_tokens=current.input_tokens + usage.input_tokens,
            output_tokens=current.output_tokens + usage.output_tokens,
            total_tokens=current.total_tokens + total,
        )
        self.metrics.input_tokens += usage.input_tokens
        self.metrics.output_tokens += usage.output_tokens
        self.metrics.total_tokens += total

    def finalize(self, *, success: bool) -> PipelineMetrics:
        self.metrics.duration_s = time.monotonic() - self._start
        self.metrics.success = success
        return self.metrics

    def flush(self, jsonl_path: Path) -> Path:
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.metrics.model_dump()
        with jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        logger.info(
            f"metrics 已写入 {jsonl_path.name}: "
            f"duration={self.metrics.duration_s:.1f}s, "
            f"sections={self.metrics.sections_count}, "
            f"library_hits={self.metrics.library_hits}, "
            f"library_added={self.metrics.library_added}"
        )
        return jsonl_path
