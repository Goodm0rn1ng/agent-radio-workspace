"""Branch B：分析新直播「有什么内容」，结合 B 站当前热点判断各片段的爆火潜力。

候选片段来源：直播章节（歌枠常按歌分章，最可靠）；无章节时退化为整场单候选。
评分：LLM 交叉比对候选 与 B 站热点（大火歌曲 / 热门话题 / 关键词），给爆火分。
"""
from __future__ import annotations

from clip.config import clip_config
from clip.models import MatchedClip
from clip.trend_features import TrendFeature
from clip.youtube_source import Chapter, LiveMeta
from src.llm.client import LLMClient

_VIRAL_SYSTEM = """你是 VTuber 切片二次创作的选题分析师。给你一场直播的若干片段（章节标题+时长），
以及当前 B 站正在火的「热门歌曲 / 话题 / 关键词」。判断每个片段被切出来后的爆火潜力：
重点看片段是否命中了当前大火的歌曲（如歌枠翻唱了热门曲）、热门话题或梗。

输出严格 JSON：
{
  "picks": [
    {
      "index": 片段序号(从1开始),
      "viral_score": 0~1,
      "matched_signal": "命中的具体热点（歌名/话题/关键词），没有则空串",
      "reason": "为什么有潜力（一句话）",
      "title": "切片成片标题",
      "copy": "一句话文案"
    }
  ]
}
只保留 viral_score>=0.4 的片段；都不行则 picks 为空。不要编造片段里没有的内容。"""


def _candidates(live: LiveMeta) -> list[Chapter]:
    if live.chapters:
        return live.chapters
    # 无章节：整场作为单一候选，至少能产出一个切片（标题用直播标题）。
    print("  [warn] 直播无章节，退化为整场单候选")
    return [Chapter(start=0.0, end=live.duration or 0.0, title=live.title)]


def _hot_signals(features: list[TrendFeature]) -> str:
    songs, topics, kws = [], [], []
    for f in features:
        songs += f.hot_songs
        topics.append(f.topic)
        kws += f.keywords_zh + f.keywords_ja
    return (
        f"热门歌曲：{', '.join(dict.fromkeys(songs)) or '无'}\n"
        f"热门话题：{'; '.join(t for t in topics if t)}\n"
        f"关键词：{', '.join(dict.fromkeys(kws))}"
    )


def analyze_live(live: LiveMeta, features: list[TrendFeature], llm: LLMClient,
                 profile=None) -> list[MatchedClip]:
    cands = _candidates(live)
    listing = "\n".join(
        f"[{i+1}] {c.title}  ({c.start:.0f}-{c.end:.0f}s, 时长{c.end-c.start:.0f}s)"
        for i, c in enumerate(cands)
    )
    ctx = ""
    if profile is not None:
        ctx = (
            f"\n节目背景：{profile.performer}（{profile.band}）。成员：{profile.member_glossary}\n"
            f"爆火侧重：{profile.viral_focus}\n"
        )
    user = (
        f"直播标题：{live.title}\n直播简介：{live.description[:300]}\n{ctx}\n"
        f"当前 B 站热点：\n{_hot_signals(features)}\n\n"
        f"直播片段：\n{listing}"
    )
    data = llm.complete_json(_VIRAL_SYSTEM, user, max_tokens=1800)

    clips: list[MatchedClip] = []
    for pick in data.get("picks", []):
        idx = int(pick.get("index", 0)) - 1
        if not (0 <= idx < len(cands)):
            continue
        score = float(pick.get("viral_score", 0))
        if score < clip_config.clip_min_score:
            continue
        c = cands[idx]
        clips.append(MatchedClip(
            source="new",
            title=pick.get("title", "") or c.title,
            copy=pick.get("copy", ""),
            start=c.start,
            end=c.end,
            score=score,
            reason=pick.get("reason", ""),
            matched_signal=pick.get("matched_signal", ""),
            episode_label=live.title,
            citation=f"{live.title} {c.start:.0f}-{c.end:.0f}s",
            text=c.title,
            media_path=str(live.video_path) if live.video_path else "",
            media_missing=not live.video_path,
        ))
    clips.sort(key=lambda x: x.score, reverse=True)
    return clips[: clip_config.clip_topk]
