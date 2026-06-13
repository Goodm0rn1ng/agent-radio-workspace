"""全链路成本台账：每次 LLM 调用的 token + 墙钟耗时 + 估算成本，按「产出」聚合。

一次问答 / 一个切片任务会扇出多次 LLM 调用（分析、路由、生成、校验…）。这里用一个
contextvar 给「当前产出」打唯一标签，`track()` 上下文管理器在退出时把这段时间内所有 LLM
调用汇总成一条产出记录（n 次调用、总 token、估算 USD、墙钟/LLM 耗时）。

进程内环形缓冲（线程安全）+ 追加到 jsonl 持久化。纯只读统计，不影响主流程；记账失败一律吞掉。
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

# 每 100 万 token 的估算单价 (输入, 输出) USD。best-effort，未知模型记 0 并标注 estimated=False。
# DeepSeek 官方价近似；其余为公开参考价。仅供「相对量级」参考，不是账单。
PRICING: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.27, 1.10),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o": (2.50, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "mimo-v2.5-pro": (0.0, 0.0),
}

_LEDGER_PATH = Path(__file__).resolve().parents[2] / "data" / "cost_ledger.jsonl"
_current: contextvars.ContextVar[str] = contextvars.ContextVar("cost_label", default="")
_lock = threading.Lock()
_calls: deque[Call] = deque(maxlen=4000)      # 单次 LLM 调用
_outputs: deque[dict] = deque(maxlen=800)     # 聚合后的「产出」


@dataclass
class Call:
    ts: float
    label: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_s: float
    cost_usd: float


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
    """返回 (估算USD, 是否有该模型单价)。"""
    if model not in PRICING:
        return 0.0, False
    pin, pout = PRICING[model]
    return prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout, True


def record(provider: str, model: str, prompt_tokens: int,
           completion_tokens: int, latency_s: float) -> None:
    """记一次 LLM 调用（由 LLMClient 调用）。"""
    try:
        cost, _known = estimate_cost(model, prompt_tokens, completion_tokens)
        c = Call(time.time(), _current.get(), provider, model,
                 int(prompt_tokens or 0), int(completion_tokens or 0),
                 round(latency_s, 3), round(cost, 6))
        with _lock:
            _calls.append(c)
    except Exception:  # noqa: BLE001 — 记账绝不影响主流程
        pass


class track:
    """把内部所有 LLM 调用聚合为一条「产出」。

    with track("qa", title=question):  # 或 "clip" / "mail" …
        ...  # 期间的每次 complete_json/_complete_text 都会归到这条产出
    """

    def __init__(self, kind: str, title: str = "", **meta):
        self.kind = kind
        self.title = (title or "")[:120]
        self.meta = meta

    def __enter__(self):
        self.id = f"{self.kind}:{uuid.uuid4().hex[:8]}"
        self.t0 = time.time()
        self._tok = _current.set(self.id)
        return self

    def __exit__(self, *exc):
        _current.reset(self._tok)
        try:
            with _lock:
                calls = [c for c in _calls if c.label == self.id]
            row = {
                "ts": self.t0,
                "kind": self.kind,
                "title": self.title,
                "n_calls": len(calls),
                "models": sorted({c.model for c in calls}),
                "prompt_tokens": sum(c.prompt_tokens for c in calls),
                "completion_tokens": sum(c.completion_tokens for c in calls),
                "total_tokens": sum(c.prompt_tokens + c.completion_tokens for c in calls),
                "cost_usd": round(sum(c.cost_usd for c in calls), 6),
                "llm_s": round(sum(c.latency_s for c in calls), 2),
                "wall_s": round(time.time() - self.t0, 2),
                **self.meta,
            }
            with _lock:
                _outputs.append(row)
            _persist(row)
        except Exception:  # noqa: BLE001
            pass
        return False


def _persist(row: dict) -> None:
    try:
        _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def recent_outputs(n: int = 50) -> list[dict]:
    with _lock:
        return list(_outputs)[-n:][::-1]


def recent_calls(n: int = 100) -> list[dict]:
    with _lock:
        return [asdict(c) for c in list(_calls)[-n:][::-1]]


def totals() -> dict:
    # computed from the durable per-output rows (survive restart via jsonl load),
    # not the transient _calls buffer.
    with _lock:
        outs = list(_outputs)
    return {
        "n_outputs": len(outs),
        "n_calls": sum(o.get("n_calls", 0) for o in outs),
        "total_tokens": sum(o.get("total_tokens", 0) for o in outs),
        "total_cost_usd": round(sum(o.get("cost_usd", 0.0) for o in outs), 6),
        "by_kind": _by_kind(outs),
    }


def _by_kind(outs: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for o in outs:
        a = agg.setdefault(o["kind"], {"kind": o["kind"], "n": 0, "tokens": 0,
                                       "cost_usd": 0.0, "wall_s": 0.0})
        a["n"] += 1
        a["tokens"] += o.get("total_tokens", 0)
        a["cost_usd"] += o.get("cost_usd", 0.0)
        a["wall_s"] += o.get("wall_s", 0.0)
    for a in agg.values():
        a["avg_cost_usd"] = round(a["cost_usd"] / max(a["n"], 1), 6)
        a["avg_wall_s"] = round(a["wall_s"] / max(a["n"], 1), 2)
        a["cost_usd"] = round(a["cost_usd"], 6)
    return sorted(agg.values(), key=lambda x: x["cost_usd"], reverse=True)


def _load_persisted() -> None:
    """启动时把 jsonl 末尾若干条产出载入内存，让看板跨重启延续。"""
    try:
        if not _LEDGER_PATH.exists():
            return
        lines = _LEDGER_PATH.read_text(encoding="utf-8").splitlines()[-_outputs.maxlen:]
        for line in lines:
            try:
                _outputs.append(json.loads(line))
            except (ValueError, TypeError):
                continue
    except Exception:  # noqa: BLE001
        pass


_load_persisted()
