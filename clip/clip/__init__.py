"""clip —— 数据驱动型内容二次创作（独立项目 Agent/clip/）。

B 站市场热度信号驱动 VTuber 直播/广播素材的二次剪辑：下载/录制 → 自动总结入库 →
爆火/曲目分析 → 切片 → 二次精听字幕 → 推 Telegram 点击即出片。

**桥接复用**：clip 深度复用 radio_kg（向量检索/入库图/LLM/嵌入/规范实体）与 Radio
（转写摘要 run_pipeline、Telegram bot）。`radio` 与 `clip` 已作为 editable 包装进
工作区唯一 venv（Agent/.venv，见 Agent/pyproject.toml），直接 import 即可；
radio_kg 不是安装包（顶层模块名为 src/config），故仍需把其根目录加入 sys.path。
本文件不导入任何重依赖，确保在独立 ASR venv 里导入 clip 子模块也安全。
"""
from __future__ import annotations

import sys
from pathlib import Path

_AGENT = Path(__file__).resolve().parents[2]        # Agent/
_KG = _AGENT / "radio_kg"                            # config.settings / src.*

if _KG.exists() and str(_KG) not in sys.path:
    sys.path.insert(0, str(_KG))
