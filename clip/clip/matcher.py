"""Branch A：把 B 站爆款特征作为 Query，在已有素材的摘要向量库里跨模态检索匹配
高相关片段（带期数+时间戳），再用 LLM 打分并生成贴合热点的标题/文案。

复用 radio_kg 的 SummaryRetriever（摘要段已带 start_sec/end_sec/citation，是天然的可剪辑单元）。
"""
from __future__ import annotations

from config.settings import settings
from clip.config import clip_config
from clip.media_locator import find_media
from clip.models import MatchedClip
from clip.trend_features import TrendFeature
from src.llm.client import LLMClient
from src.mcp_layer.vector_store import VectorStore
from src.retrieval.two_stage import SummaryRetriever

_SCORE_SYSTEM = """你在为「广播二次创作」挑选片段。给你一个 B 站热点话题，以及从广播素材库里检索到的若干候选片段（含期数/时间/摘要文本）。
判断每个候选与热点的契合度，并为契合的片段生成适配该热点的短视频标题与一句话文案。

输出严格 JSON：
{
  "picks": [
    {
      "index": 候选序号(从1开始),
      "score": 0~1 的契合度,
      "reason": "为什么契合（结合热点与片段内容，一句话）",
      "title": "适配热点的短视频标题",
      "copy": "一句话文案/简介"
    }
  ]
}
只保留 score>=0.4 的候选；若全都不契合，picks 为空数组。不要编造片段里没有的内容。"""


def _score_and_title(feature: TrendFeature, clues: list[dict], llm: LLMClient) -> list[dict]:
    listing = "\n\n".join(
        f"[{i+1}] 期数:{c.get('episode_label')} 时间:{c.get('start_sec'):.0f}-{c.get('end_sec'):.0f}s "
        f"小节:{c.get('title')}\n摘要:{(c.get('text') or '')[:300]}"
        for i, c in enumerate(clues)
    )
    user = (
        f"热点话题：{feature.topic}\n"
        f"关键词：{', '.join(feature.keywords_zh + feature.keywords_ja)}\n"
        f"热门歌曲：{', '.join(feature.hot_songs) or '无'}\n\n"
        f"候选片段：\n{listing}"
    )
    data = llm.complete_json(_SCORE_SYSTEM, user, max_tokens=1500)
    return data.get("picks", [])


def match_trends(features: list[TrendFeature], llm: LLMClient,
                 summary_collection: str = "radio_summaries") -> list[MatchedClip]:
    clips: list[MatchedClip] = []
    with VectorStore(collection_name=summary_collection) as store:
        retriever = SummaryRetriever(store)
        for feature in features:
            clues, _best = retriever.route(feature.queries(), k=6)
            if not clues:
                continue
            picks = _score_and_title(feature, clues, llm)
            for pick in picks:
                idx = int(pick.get("index", 0)) - 1
                if not (0 <= idx < len(clues)):
                    continue
                score = float(pick.get("score", 0))
                if score < clip_config.clip_min_score:
                    continue
                c = clues[idx]
                label = c.get("episode_label", "")
                media = find_media(label)
                clips.append(MatchedClip(
                    source="past",
                    title=pick.get("title", "") or c.get("title", ""),
                    copy=pick.get("copy", ""),
                    start=float(c.get("start_sec", 0)),
                    end=float(c.get("end_sec", 0)),
                    score=score,
                    reason=pick.get("reason", ""),
                    trend_topic=feature.topic,
                    matched_signal=", ".join(feature.hot_songs or feature.keywords_ja[:2]),
                    episode=c.get("episode"),
                    episode_label=label,
                    citation=c.get("citation", ""),
                    text=c.get("text", ""),
                    media_path=str(media) if media else "",
                    media_missing=media is None,
                ))
    clips.sort(key=lambda x: x.score, reverse=True)
    return clips[: clip_config.clip_topk]
