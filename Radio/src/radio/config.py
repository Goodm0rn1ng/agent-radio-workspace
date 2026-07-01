"""配置加载：合并 .env（敏感凭证）和 config.yaml（节目/模型参数）成一个强类型 Settings。

用法：
    from radio.config import load_settings
    settings = load_settings()
    print(settings.program.name, settings.secrets.groq_api_key)
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- .env 部分 --------------------------------------------------------------


class Secrets(BaseSettings):
    """从 .env 加载的敏感凭证。Pydantic 会按字段名读取环境变量。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    groq_api_key: SecretStr
    deepseek_api_key: SecretStr
    anthropic_api_key: SecretStr
    gemini_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr
    telegram_chat_id: str


# --- config.yaml 部分 -------------------------------------------------------


class ScheduleConfig(BaseModel):
    timezone: str = "Asia/Tokyo"
    day_of_week: str = "wed"
    hour: int = 21
    minute: int = 0


class ProgramConfig(BaseModel):
    name: str
    channel_live_url: str
    schedule: ScheduleConfig
    detection_timeout_minutes: int = 30
    detection_interval_seconds: int = 60


class ScheduledProgramConfig(BaseModel):
    """调度器管理的单个节目（v0.5.0 起）。"""

    name: str                    # 节目展示名，进 Telegram + work_dir
    source_type: str             # "radiko_live" / "radiko_timefree" / "youtube_live"
    enabled: bool = True
    schedule: ScheduleConfig

    # source_type=radiko_*
    radiko_station_id: str = ""
    radiko_timefree_url: str = ""
    interval_days: int = 7

    # source_type=youtube_live
    channel_live_url: str = ""
    detection_timeout_minutes: int = 30
    detection_interval_seconds: int = 60
    cookies_path: Path | None = None

    # 通用
    duration_minutes: int = 30
    profile_id: str | None = None
    health_check: bool = True
    health_pre_record_minutes: int = 0  # 0 = 触发时立即体检
    fail_on_health_fail: bool = False
    fine_translation: bool = False


class SchedulerConfig(BaseModel):
    """daemon / APScheduler 配置。"""

    jobstore_path: Path = Path("data/scheduler.sqlite")
    # 错过窗口的 grace period（秒）；为 0 表示错过就跳过
    misfire_grace_seconds: int = 600


class STTConfig(BaseModel):
    model: str = "whisper-large-v3"
    prompt: str = ""
    language: str = "ja"
    segment_seconds: int = 300
    # 切点对齐静音，避免固定时长硬切截断句子（「听不全」的主要来源）
    silence_align: bool = True
    # —— 幻听过滤（用 Whisper verbose_json 的置信信号）——
    # 经典静音/音乐幻听判据：no_speech_prob 高 且 avg_logprob 低（OpenAI 同款规则）
    filter_no_speech_max: float = 0.6
    filter_logprob_min: float = -1.0
    # 段内复读循环：压缩比超限即为重复轰炸
    filter_compression_max: float = 2.4
    # 空洞重听：首轮转写留下 ≥N 秒空白时，单独切出该段重转一次（防 Whisper
    # 解码窗被幻听吃掉后整窗跳过造成的漏听）。音乐段被过滤产生的空洞会再次被过滤，无害。
    gap_relisten: bool = True
    gap_relisten_min_seconds: float = 25.0
    # 已知幻听口癖（YouTube 结尾语等）：整段去掉标点后只剩这些短语的重复时，
    # 在置信不高（no_speech_prob>0.2 或 avg_logprob<-0.45）的前提下丢弃
    hallucination_phrases: list[str] = [
        "ご視聴ありがとうございました",
        "ご清聴ありがとうございました",
        "チャンネル登録お願いします",
        "チャンネル登録よろしくお願いします",
        "字幕視聴ありがとうございました",
    ]


class TranslationConfig(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    fine_provider: str = "anthropic"
    fine_model: str = "claude-haiku-4-5"
    batch_size: int = 30
    terminology_path: Path = Path("config/terminology.yaml")
    prompt_path: Path = Path("src/radio/prompts/translate.txt")


class SummaryConfig(BaseModel):
    provider: str = "gemini"
    model: str = "gemini-2.5-flash"
    max_summary_chars: int = 600
    max_output_tokens: int = 32768
    target_highlight_count: int = 5
    # 节目专属总结侧重（注入 prompt 的 {summary_style} 槽）。空＝无特别侧重（现状）。
    summary_style: str = ""
    # few-shot 范例总结（注入 {style_exemplar} 槽）：学其语气/结构，不照抄内容。空＝不注入。
    style_exemplar: str = ""
    prompt_path: Path = Path("src/radio/prompts/summarize.txt")
    segments_library_path: Path = Path("config/segments_library.yaml")
    # 跑节目时，LLM 发现的 is_recurring=False 新环节是否自动追加到 library
    auto_append_new_segments: bool = True
    # 往期回忆：summarize 时注入同节目最近 N 期的 key_topics + highlights
    history_recent_n: int = 5


class RuntimeConfig(BaseModel):
    recordings_dir: Path = Path("data/recordings")
    logs_dir: Path = Path("data/logs")
    stt_concurrency: int = 2


class XiaohongshuConfig(BaseModel):
    """xiaohongshu-cli 集成（Telegram 审核后保存为小红书私密笔记）。

    封面图来源：用户点保存按钮后，向 Telegram bot 直接发送一张图片，下载后用作封面。
    """

    enabled: bool = False
    cli_command: list[str] = ["xhs"]
    private: bool = True
    title_max_chars: int = 30
    body_max_chars: int = 1000
    # 按 series_name 配置的话题标签，例：
    #   {"羊宮妃那のこもれびじかん": ["羊宮妃那", "声優ラジオ"]}
    topics_by_series: dict[str, list[str]] = {}


class KnowledgeBaseConfig(BaseModel):
    """Optional handoff to the downstream radio_kg service."""

    enabled: bool = False
    ingest_url: str = ""
    timeout_seconds: float = 600.0
    fail_pipeline_on_error: bool = False


class YAMLConfig(BaseModel):
    program: ProgramConfig
    stt: STTConfig
    translation: TranslationConfig
    summary: SummaryConfig
    name_corrections: dict[str, str] | None = None
    runtime: RuntimeConfig
    # v0.5.0+：调度器模式（main_daemon.py 入口）。CLI 单跑模式不依赖这两个。
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    scheduled_programs: list[ScheduledProgramConfig] = Field(default_factory=list)
    xiaohongshu: XiaohongshuConfig = Field(default_factory=XiaohongshuConfig)
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)


# --- 合并出口 ---------------------------------------------------------------


class Settings(BaseModel):
    """整个应用唯一的配置出入口。"""

    secrets: Secrets
    program: ProgramConfig
    stt: STTConfig
    translation: TranslationConfig
    summary: SummaryConfig
    name_corrections: dict[str, str]
    runtime: RuntimeConfig
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    scheduled_programs: list[ScheduledProgramConfig] = Field(default_factory=list)
    xiaohongshu: XiaohongshuConfig = Field(default_factory=XiaohongshuConfig)
    knowledge_base: KnowledgeBaseConfig = Field(default_factory=KnowledgeBaseConfig)


def load_settings(yaml_path: str | Path = "config/config.yaml") -> Settings:
    """加载并校验全部配置。任一字段缺失或类型不对都会抛出清晰的错误。"""
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"配置文件不存在：{yaml_path.resolve()}\n"
            "复制 config/config.yaml 模板并按需修改。"
        )
    project_root = yaml_path.resolve().parent.parent

    with yaml_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    yaml_cfg = YAMLConfig(**raw)
    _resolve_paths(yaml_cfg, project_root)
    secrets = Secrets(_env_file=project_root / ".env")  # type: ignore[call-arg]

    return Settings(
        secrets=secrets,
        program=yaml_cfg.program,
        stt=yaml_cfg.stt,
        translation=yaml_cfg.translation,
        summary=yaml_cfg.summary,
        name_corrections=yaml_cfg.name_corrections or {},
        runtime=yaml_cfg.runtime,
        scheduler=yaml_cfg.scheduler,
        scheduled_programs=yaml_cfg.scheduled_programs,
        xiaohongshu=yaml_cfg.xiaohongshu,
        knowledge_base=yaml_cfg.knowledge_base,
    )


def _resolve_path(path: Path | None, root: Path) -> Path | None:
    if path is None or path.is_absolute():
        return path
    return root / path


def _resolve_paths(cfg: YAMLConfig, root: Path) -> None:
    cfg.translation.terminology_path = _resolve_path(cfg.translation.terminology_path, root)
    cfg.translation.prompt_path = _resolve_path(cfg.translation.prompt_path, root)
    cfg.summary.prompt_path = _resolve_path(cfg.summary.prompt_path, root)
    cfg.summary.segments_library_path = _resolve_path(cfg.summary.segments_library_path, root)
    cfg.runtime.recordings_dir = _resolve_path(cfg.runtime.recordings_dir, root)
    cfg.runtime.logs_dir = _resolve_path(cfg.runtime.logs_dir, root)
    cfg.scheduler.jobstore_path = _resolve_path(cfg.scheduler.jobstore_path, root)
    for program in cfg.scheduled_programs:
        program.cookies_path = _resolve_path(program.cookies_path, root)
