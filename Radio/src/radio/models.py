"""Pipeline 各模块间共享的数据结构。"""

from __future__ import annotations

from pydantic import BaseModel


class Segment(BaseModel):
    """一段带时间戳的转写文本。Whisper 输出的最小单位。"""

    i: int  # 全局段索引（拼接所有切片后的顺序）
    start: float  # 起始秒
    end: float  # 结束秒
    ja: str  # 日文原文
    zh: str = ""  # 中文翻译（翻译完成后填入）


class Highlight(BaseModel):
    """LLM 标注的一个高光时刻。"""

    timestamp: str  # HH:MM:SS
    reason: str
    quote: str


class ProgramSection(BaseModel):
    """节目中的一个环节/部分总结。"""

    title: str = ""  # 环节中文展示名（保留向后兼容，Telegram v0.2.1 起不渲染）
    title_ja: str = ""  # 环节名日语原文（如 "僕、私、迷子中"）
    intro: str = ""  # 环节介绍；来自 segments_library 或 LLM 现编
    is_recurring: bool = False  # 是否匹配到 segments_library 中的已登记环节
    time_range: str
    content: str
    listener_mail_from: str = ""  # 来信署名/人名日语原文（如「松剣さんばさん」「電気羊さん」）
    listener_mail: str = ""  # 来信中文翻译（保留向后兼容，Telegram v0.2.1 起不渲染）
    listener_mail_ja: str = ""  # 来信日语原文（保留向后兼容，Telegram v0.2.1 起不渲染）
    member_reactions: list[str] = []
    music: list[str] = []
    notes: list[str] = []


class Summary(BaseModel):
    """summarize 模块的输出。"""

    summary: str
    sections: list[ProgramSection] = []
    key_topics: list[str] = []
    highlights: list[Highlight] = []  # 仍生成（落盘 JSON 保留），但 v0.2.1 起不再推 Telegram
