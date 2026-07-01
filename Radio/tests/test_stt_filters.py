"""ASR 幻听过滤单元测试（Radio/CHANGELOG.md 2026-06-10 四项治理的回归防线）。

覆盖 src/radio/stt.py 的四个纯函数：置信度三重过滤、prompt 回声检测、
跨段复读坍缩。不触网、不需要 Groq API。
"""
from __future__ import annotations

from types import SimpleNamespace

from radio.config import STTConfig
from radio.models import Segment
from radio.stt import (
    _collapse_repeats,
    _filter_segment,
    _is_hallucination_phrase,
    _looks_like_prompt_echo,
)

_PHRASES = STTConfig().hallucination_phrases


def _settings(**stt_overrides) -> SimpleNamespace:
    return SimpleNamespace(stt=STTConfig(**stt_overrides))


class TestIsHallucinationPhrase:
    def test_exact_known_phrase_matches(self):
        assert _is_hallucination_phrase("ご視聴ありがとうございました", _PHRASES) is True

    def test_repeated_known_phrase_matches(self):
        text = "ご視聴ありがとうございました。ご視聴ありがとうございました。"
        assert _is_hallucination_phrase(text, _PHRASES) is True

    def test_unrelated_text_does_not_match(self):
        assert _is_hallucination_phrase("今日はいい天気ですね", _PHRASES) is False

    def test_no_match_at_all_is_not_length_misjudged(self):
        # 没命中任何黑名单短语时必须直接判 False，防止超短文本被长度判定误杀。
        assert _is_hallucination_phrase("もう", _PHRASES) is False

    def test_real_content_around_the_phrase_is_not_flagged(self):
        # 主播真的说了感谢语，但周围还有大段实际内容——不应被判定为纯幻听。
        text = (
            "ご視聴ありがとうございました、今日は本当に楽しかったです、"
            "また来週も同じ時間に見てくださいね、待ってます"
        )
        assert _is_hallucination_phrase(text, _PHRASES) is False


class TestFilterSegment:
    def test_high_no_speech_and_low_logprob_is_dropped(self):
        settings = _settings()
        reason = _filter_segment("……", 0.9, -2.0, 1.0, settings)
        assert reason is not None and "no_speech" in reason

    def test_high_compression_ratio_is_dropped(self):
        settings = _settings()
        reason = _filter_segment("あああああ", 0.1, -0.1, 3.0, settings)
        assert reason is not None and "compression_ratio" in reason

    def test_hallucination_phrase_with_weak_confidence_is_dropped(self):
        settings = _settings()
        reason = _filter_segment(
            "ご視聴ありがとうございました", no_speech_prob=0.3, avg_logprob=-0.1,
            compression_ratio=1.0, settings=settings,
        )
        assert reason == "hallucination_phrase"

    def test_hallucination_phrase_with_high_confidence_is_kept(self):
        # 主播真的说了感谢语且置信很高——不应丢弃（CHANGELOG「主播真说感谢时保留」）。
        settings = _settings()
        reason = _filter_segment(
            "ご視聴ありがとうございました", no_speech_prob=0.05, avg_logprob=-0.1,
            compression_ratio=1.0, settings=settings,
        )
        assert reason is None

    def test_normal_confident_segment_is_kept(self):
        settings = _settings()
        reason = _filter_segment(
            "今日はゲストを迎えてお届けします", no_speech_prob=0.05, avg_logprob=-0.1,
            compression_ratio=1.2, settings=settings,
        )
        assert reason is None

    def test_thresholds_are_configurable(self):
        settings = _settings(filter_compression_max=10.0)
        reason = _filter_segment("あああ", 0.1, -0.1, 3.0, settings)
        assert reason is None


class TestLooksLikePromptEcho:
    PROMPT = "ラジオ、ライブ、安野希世乃、悠木碧"

    def test_full_prompt_repeated_is_echo(self):
        assert _looks_like_prompt_echo(self.PROMPT, self.PROMPT) is True

    def test_single_prompt_term_is_not_echo(self):
        # 主播可能真的说出节目名——单个词条不算回声。
        assert _looks_like_prompt_echo("ラジオ", self.PROMPT) is False

    def test_unrelated_text_is_not_echo(self):
        assert _looks_like_prompt_echo("今日は天気がいいですね", self.PROMPT) is False

    def test_empty_prompt_is_never_echo(self):
        assert _looks_like_prompt_echo("ラジオ、ライブ", None) is False

    def test_many_chunk_high_overlap_is_echo(self):
        prompt = "アルファ、ベータ、ガンマ、デルタ、イプシロン、ゼータ"
        text = "アルファ、ベータ、ガンマ、デルタ、イプシロン、ゼータ"
        assert _looks_like_prompt_echo(text, prompt) is True


class TestCollapseRepeats:
    def _seg(self, i, start, end, ja):
        return Segment(i=i, start=start, end=end, ja=ja)

    def test_three_or_more_identical_segments_collapse_to_one(self):
        flat = [
            self._seg(0, 0.0, 1.0, "ありがとう"),
            self._seg(1, 1.0, 2.0, "ありがとう"),
            self._seg(2, 2.0, 3.0, "ありがとう"),
        ]
        out = _collapse_repeats(flat)
        assert len(out) == 1
        assert out[0].start == 0.0
        assert out[0].end == 3.0

    def test_two_identical_segments_are_kept_separate(self):
        flat = [
            self._seg(0, 0.0, 1.0, "ありがとう"),
            self._seg(1, 1.0, 2.0, "ありがとう"),
        ]
        out = _collapse_repeats(flat)
        assert len(out) == 2

    def test_non_adjacent_duplicates_are_not_merged(self):
        flat = [
            self._seg(0, 0.0, 1.0, "ありがとう"),
            self._seg(1, 1.0, 2.0, "今日はいい天気"),
            self._seg(2, 2.0, 3.0, "ありがとう"),
        ]
        out = _collapse_repeats(flat)
        assert len(out) == 3

    def test_segment_ids_are_reindexed_sequentially(self):
        flat = [
            self._seg(5, 0.0, 1.0, "ありがとう"),
            self._seg(9, 1.0, 2.0, "ありがとう"),
            self._seg(12, 2.0, 3.0, "ありがとう"),
            self._seg(20, 3.0, 4.0, "次の話題です"),
        ]
        out = _collapse_repeats(flat)
        assert [s.i for s in out] == [0, 1]
