"""Clipper 共用数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MatchedClip:
    """一个待剪辑的候选片段（Branch A 匹配 / Branch B 爆火潜力 共用）。"""
    source: str                 # "past" | "new"
    title: str                  # 建议成片标题
    copy: str                   # 建议文案/简介
    start: float                # 秒
    end: float                  # 秒
    score: float                # 相关性 / 爆火潜力 0-1
    reason: str                 # 入选理由
    trend_topic: str = ""       # 触发它的热点
    matched_signal: str = ""    # 命中的具体信号（歌名/关键词）
    episode: int | None = None
    episode_label: str = ""
    citation: str = ""
    text: str = ""              # 该片段对应的转写文本（用于字幕/参考）
    media_path: str = ""        # 源媒体绝对路径（slicer 填充/校验）
    media_missing: bool = False

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "copy": self.copy,
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "duration": round(self.end - self.start, 2),
            "score": round(self.score, 3),
            "reason": self.reason,
            "trend_topic": self.trend_topic,
            "matched_signal": self.matched_signal,
            "episode": self.episode,
            "episode_label": self.episode_label,
            "citation": self.citation,
            "media_path": self.media_path,
            "media_missing": self.media_missing,
        }
