"""爆款特征提取（共用）：动量筛选 + LLM 特征蒸馏。

动量指标（不只看绝对播放量）：
- momentum      = view / 发布小时数        （增长速度）
- coin_like     = coin / like              （投币点赞比，硬核认可度）
- danmaku_dens  = danmaku / duration       （弹幕密度，互动热度）
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from clip.bilibili_source import TrendItem
from clip.config import clip_config
from src.llm.client import LLMClient

_FEATURE_SYSTEM = """你是 ACGN/虚拟主播领域的内容选题分析师。给你一批 B 站近期爆款视频的标题/简介/标签/热评，
请提炼出可用于「在广播/直播素材库里检索匹配片段」的检索特征。

输出严格 JSON：
{
  "trends": [
    {
      "topic": "一句话概括这个热点话题",
      "keywords_zh": ["中文关键词", ...],
      "keywords_ja": ["对应的日文关键词（人名/作品/梗用日文原文）", ...],
      "hot_songs": ["若涉及歌曲/翻唱，列出歌名（日文原名优先），否则空数组"],
      "hook": "为什么这个点可能爆（一句话）"
    }
  ]
}
要求：合并同质热点；keywords_ja 尽量给日文原名（声优名、作品名、歌名）以便跨语言检索；不要编造不存在的歌曲。"""


@dataclass
class TrendFeature:
    topic: str
    keywords_zh: list[str] = field(default_factory=list)
    keywords_ja: list[str] = field(default_factory=list)
    hot_songs: list[str] = field(default_factory=list)
    hook: str = ""
    momentum: float = 0.0          # 代表性来源稿件的动量（供排序/展示）
    source_titles: list[str] = field(default_factory=list)

    def queries(self) -> list[str]:
        """跨模态检索 query：日文优先 + 中文 + 话题 + 歌名。"""
        qs = list(self.keywords_ja) + list(self.hot_songs) + list(self.keywords_zh)
        if self.topic:
            qs.append(self.topic)
        seen, out = set(), []
        for q in qs:
            q = (q or "").strip()
            if q and q not in seen:
                seen.add(q)
                out.append(q)
        return out


def _now_ref(items: list[TrendItem]) -> float:
    """返回计算视频年龄的参考时间。

    正常情况下用真实当前时间，才能保证「发布已满 6 小时」是字面意义上的
    6 小时；若本机时钟明显偏离 B 站稿件时间，则退回本批最新发布时间。
    """
    latest = max((it.pubdate for it in items if it.pubdate), default=0.0)
    if not latest:
        return 0.0
    now = time.time()
    if abs(now - latest) <= 7 * 86400:
        return now
    return latest


def score_item(item: TrendItem, now_ts: float) -> float:
    """综合动量分（归一前的加权和，仅用于排序）。"""
    momentum = item.view / item.hours_since(now_ts)
    coin_like = item.coin / max(item.like, 1)
    danmaku_dens = item.danmaku / max(item.duration, 1)
    return momentum * (1.0 + coin_like) * (1.0 + min(danmaku_dens, 5.0))


def rank_items(items: list[TrendItem]) -> list[TrendItem]:
    """按时间窗过滤 + 综合动量排序（时间相对本批最新稿件）。"""
    if not items:
        return []
    now_ts = _now_ref(items)
    window = clip_config.clip_hours_window
    min_age = max(0.0, clip_config.trends_min_age_hours)
    fresh = [it for it in items if min_age <= it.hours_since(now_ts) <= window]
    fresh.sort(key=lambda it: score_item(it, now_ts), reverse=True)
    return fresh


def distill_features(items: list[TrendItem], llm: LLMClient, top_n: int) -> list[TrendFeature]:
    """对动量 topN 稿件做 LLM 特征蒸馏。"""
    ranked = rank_items(items)
    top = ranked[:top_n]
    if not top:
        return []
    now_ts = _now_ref(items)
    payload = "\n\n".join(
        f"[{i+1}] 分区:{it.partition} 标题:{it.title}\n简介:{it.desc[:200]}\n"
        f"标签:{', '.join(it.tags)}\n热评:{' | '.join(it.top_comments[:5])}\n"
        f"播放:{it.view} 点赞:{it.like} 投币:{it.coin} 弹幕:{it.danmaku}"
        for i, it in enumerate(top)
    )
    data = llm.complete_json(_FEATURE_SYSTEM, payload, max_tokens=4096)
    feats: list[TrendFeature] = []
    for t in data.get("trends", []):
        feats.append(TrendFeature(
            topic=t.get("topic", ""),
            keywords_zh=t.get("keywords_zh", []) or [],
            keywords_ja=t.get("keywords_ja", []) or [],
            hot_songs=t.get("hot_songs", []) or [],
            hook=t.get("hook", ""),
            momentum=score_item(top[0], now_ts),
            source_titles=[it.title for it in top[:3]],
        ))
    return feats
