"""Clip 配置 — 读取共享的 radio_kg/.env，给出全部默认值。

独立项目 Agent/clip/；运行时桥接复用 radio_kg + Radio（见 clip/__init__.py）。
"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]          # Agent/clip/（本项目根）
_ENV = ROOT.parent / "radio_kg" / ".env"            # 复用 radio_kg 同一份 .env

# B 站分区名 → ranking v2 的 rid（tid）。允许在 .env 里直接写数字 rid。
PARTITION_RID = {
    "all": 0,
    "douga": 1,      # 动画
    "anime": 1,
    "music": 3,      # 音乐（“大火歌曲”信号主要来源）
    "dance": 129,    # 舞蹈
    "game": 4,       # 游戏
    "vtuber": 240,   # 虚拟UP主（站点偶有调整，可用数字 rid 覆盖）
    "virtual": 240,
    "ent": 5,        # 娱乐
    "life": 160,     # 生活
}


class ClipperConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV, env_file_encoding="utf-8", extra="ignore"
    )

    # B 站
    bilibili_partitions: str = "music,game,douga"   # 逗号分隔的分区名或数字 rid
    # 前端「近期热点」聚焦：只取与 歌曲/虚拟主播/bangdream 相关的分区排行（而非全站），
    # 再用关键词把榜单过滤到「翻唱/歌枠/VTuber/bangdream/声優…」相关项优先。
    # 注：B 站「虚拟主播」无排行榜接口、搜索接口已被风控(v_voucher)，故以「相关分区排行 + 关键词过滤」近似。
    trends_partitions: str = "music,dance,douga"
    trends_keywords: str = ("VTuber,Vtuber,虚拟,バンドリ,BanG Dream,邦,夢限大,歌枠,アニソン,翻唱,"
                            "カバー,cover,歌ってみた,声優,声优,Vsinger,初音,ホロ,hololive,にじ")
    trends_min_age_hours: float = 6.0                 # 热点刷新只看发布已满 N 小时的视频
    trends_search_pages: int = 3                      # 关键词搜索多翻几页，避开刚发布的新稿
    bilibili_sessdata: str = ""                      # 可选 cookie，匿名也能跑大多数榜单
    bilibili_top_per_partition: int = 30             # 每个分区取榜单前 N 条做增强
    bilibili_request_sleep: float = 0.6              # 礼貌限速（秒）

    # 选材
    clip_hours_window: int = 48                      # 只看最近 N 小时发布的稿件
    clip_topk: int = 5                               # 最终保留的热点/片段数
    clip_pad_sec: float = 1.5                        # 切片首尾 padding
    clip_min_score: float = 0.45                     # 相关性/爆火分阈值
    clip_video_res: int = 720                        # Branch B 下载分辨率上限

    # 输出
    clip_output_dir: str = "./data/clips"

    # 歌词（metadata=默认只显示曲名/原唱；netease=授权网易云歌词；file=用户自备文件；placeholder=曲名占位）
    lyrics_mode: str = "metadata"
    lyrics_netease_base_url: str = "http://127.0.0.1:3000"
    lyrics_netease_cookie: str = ""
    lyrics_netease_timeout_sec: float = 8.0
    lyrics_search_limit: int = 5
    lyrics_netease_match_score: float = 0.62
    lyrics_match_min_score: float = 0.45
    lyrics_official_min_score: float = 0.90

    # WhisperX（隔离在独立 venv，避免降级主 venv 的 torch/transformers）
    whisperx_python: str = "./.venv_whisperx/bin/python"  # 独立 venv 的解释器
    whisperx_device: str = "cpu"                     # cpu | cuda
    whisperx_compute_type: str = "int8"              # int8(cpu) | float16(cuda)
    # 切片二次精听 ASR：Kotoba-Whisper（JA 专用，faster-whisper 兼容）；首选 Parakeet-mlx 见下
    whisperx_model: str = "kotoba-tech/kotoba-whisper-v2.0-faster"
    whisperx_language: str = "ja"
    whisperx_timeout_sec: int = 1200                 # 超时即回退到逐句转写
    # 首选 Parakeet-mlx（Apple Silicon 原生，快；CTC/TDT 在静音处不出 token → 天然少幻觉）。
    # 用社区已转好的 MLX 模型（nvidia 原版是 .nemo，parakeet-mlx 不能直接加载）。失败自动回退 Kotoba。
    parakeet_model: str = "mlx-community/parakeet-tdt_ctc-0.6b-ja"
    # 切片后「二次精听」：用短片重识别词级结果校正谈话字幕的日文，并丢弃 VAD 判为静音处的幻觉句
    whisperx_relisten_text: bool = True
    whisperx_no_speech_max: float = 0.6              # no_speech_prob 超此判为幻觉，丢弃
    whisperx_logprob_min: float = -1.1              # avg_logprob 低于此判为低置信幻觉，丢弃

    def abspath(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else (ROOT / path).resolve()

    def venv_python(self) -> Path:
        """WhisperX 解释器路径——**不解析符号链接**。venv 的 bin/python 是指向基础
        解释器的 symlink，resolve() 会丢掉 venv 关联导致 site-packages 找不到。"""
        path = Path(self.whisperx_python)
        return path if path.is_absolute() else (ROOT / path)

    def partition_rids(self) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for raw in self.bilibili_partitions.split(","):
            name = raw.strip().lower()
            if not name:
                continue
            if name.isdigit():
                out.append((f"rid{name}", int(name)))
            elif name in PARTITION_RID:
                out.append((name, PARTITION_RID[name]))
        return out or [("music", 3)]


clip_config = ClipperConfig()
