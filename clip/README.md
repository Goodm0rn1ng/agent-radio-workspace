# clip — 数据驱动型内容二次创作（独立项目）

用 **B 站市场热度信号** 驱动 VTuber 直播 / 广播素材的二次剪辑，产出适配热点的成片，并与
Telegram 联动「点击即切片」。与 `radio`（录制/转写/摘要）、`radio_kg`（知识图谱+GraphRAG）
并列，**运行时桥接复用**这两者（`clip/__init__.py` 把它们加入 `sys.path`），不复制其代码。

## 流水线
```
B 站热榜(歌曲/虚拟主播/bangdream 分区+兴趣词) ─► 爆火因素 + 涨最快视频
新上传直播: yt-dlp 下载(视频+音频) ─► 自动总结入库(无审查) ─► 分时段话题 + 全曲清单
        └─► 推 Telegram / 前端（所有话题 + 每首歌一个按钮）
点击某条 ─► 按时间戳切片 ─► **二次精听**(Kotoba-Whisper/可选 Parakeet-mlx + 强制对齐，
        VAD 去幻觉 + 词级校正谈话字幕；歌唱段占位/已授权歌词，不复刻歌词) ─► 烧字幕 ─► 发回
```

## 目录
| 路径 | 职责 |
|---|---|
| `clip/`（Python 包）| 全部逻辑：B 站爬虫 / 特征 / 匹配 / 下载 / 入库 / 曲目 / 切片 / 二次精听 / 烧录 / Telegram / 前端路由 |
| `clip/programs/<id>.yaml` `.md` | 节目处理方案 + 归档方案（首个：峰月律 `minetsuki_ritsu`）|
| `static/clipper.html` | 「直播录制和切片」前端页面 |
| `data/` | clip 自有数据（`clip_jobs.json` / `clipper_interests.json` / `clips/` 成片）|
| `.venv_whisperx/` | 二次精听 ASR 独立 venv（whisperx + faster-whisper(kotoba) + parakeet-mlx）|

## 运行
clip 集成进 radio_kg 服务（`/clipper` 页面与 API）与 Radio 的 Telegram bot（切片回调）；
随 `./agent-up.command` 一起起来。也可直接用 CLI（clip 已 editable 装进工作区唯一 venv `Agent/.venv`）：
```bash
cd Agent/clip
../.venv/bin/python -m clip.cli new --program minetsuki_ritsu --url <youtube> --telegram
../.venv/bin/python -m clip.cli past --partition music,vtuber --dry-run
```

## 依赖
- 集成流程（server 与 bot）统一跑在工作区 venv `Agent/.venv`（依赖声明见 `Agent/pyproject.toml` 与 `clip/pyproject.toml`，含 `Pillow` 字幕渲染）。
- 二次精听在 `.venv_whisperx`（独立）：`whisperx`、`parakeet-mlx`（可选）。
- 桥接要求：运行时 `radio_kg/` 与 `Radio/` 在场（提供检索/入库/LLM/嵌入/转写/bot）。

> 版权：歌唱段默认不复刻歌词，只显示曲名/原唱。歌词走「歌曲信息 / 占位 / 用户自备 .srt·.lrc / 用户自配的授权网易云接口」四档，
> 由部署方保证授权与可展示范围（见 `clip/lyrics.py`）。
