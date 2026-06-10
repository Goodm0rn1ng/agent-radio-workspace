# 峰月律（Minetsuki Ritsu）/ 夢限大みゅーたいぷ — 节目处理方案 & 归档方案

> 机器可读配置见同目录 `minetsuki_ritsu.yaml`；本文是人读说明。
> 该方案独立于现有「羊宮妃那」节目，互不影响。

## 0. 角色调研（用于处理口径与总结侧重）

- **角色**：峰月律（みねつき りつ / **Minetsuki Ritsu**，以本人 YouTube 频道罗马字为准）。
- **所属**：BanG Dream! 虚拟乐队 **夢限大みゅーたいぷ（Mugendai Mewtype / "Yumemita"）** 的**节奏吉他手**。VTuber（Live2D），2023-11 出道，2025-09 起以真人露脸演出，2026-07 起有 TV 动画《BanG Dream! Yume∞Mita》。
- **人设**：成员色 **PONKOTSU BLUE** → 「ポンコツ（笨笨呆萌）」属性；自称**吉他初学者**；同时是多面手创作者（担任过 "TRASH LIFE" / "どんがらがっしゃん" 的 MV 拍摄监督）。生日 2/7，身高 157cm。
- **直播特征**：招牌问候「やほ」；直播 tag `#見守律`、绘图 tag `#りつみて`；直播以 **歌枠（アニソン 为主）/ 雑談 / 作業用BGM / ゲーム** 为主。本次测试视频即 86 分钟「アニソンおんりー歌枠」。
- **乐队成员**（KG 实体对齐 / 总结上下文）：

  | 名前 | 読み | 担当 | 成员色 |
  |---|---|---|---|
  | 仲町あられ | なかまち あられ | Vocal | NAKAYOSHI YELLOW |
  | 宮永ののか | みやなが ののか | Lead Guitar | NONOCHAN PINK |
  | **峰月律** | **みねつき りつ** | **Rhythm Guitar** | **PONKOTSU BLUE** |
  | 藤都子 | ふじ みやこ | Keyboard | ANGEL PURPLE |
  | 千石ユノ | せんごく ゆの | DJ & Manipulator | TSUNDERE PINK |

来源：[BanG Dream! 官方](https://bang-dream.com/yumemita/)、[ja.wikipedia 夢限大みゅーたいぷ](https://ja.wikipedia.org/wiki/夢限大みゅーたいぷ)、[bandori fandom](https://bandori.fandom.com/wiki/Mugendai_Mewtype)、频道 [@ritsu_yumemita](https://www.youtube.com/@ritsu_yumemita)。

## 1. 节目处理方案（processing）

| 项 | 口径 |
|---|---|
| 源语言 / 译文 | 日文 → 简体中文 |
| **KG 主持人身份** | `host.canonical=峰月律`（别名 **りつ / 律 / 峰月 / みねつき / Minetsuki Ritsu** 等）。入库时由本方案把 radio_kg 的主持人身份从默认「羊宮妃那」切到峰月律，使第一人称(私/僕)与昵称归一到正确的人，入库后自动还原。**否则会把本人发言错挂到羊宮妃那并污染共享图谱。**（注：自称「りっちゃん」译为**律酱**，非凛酱；名字统一为 **峰月律**，早前误记的 立石凛/峰乐律 均已纠正。） |
| 专名纠正词典 | 把 ASR/翻译可能听/译错的乐队名、成员名纠正为规范形（日中双语 `str.replace`），运行时**并入** Radio `name_corrections`，不改其配置文件 |
| 中文术语表 | 统一译名（夢限大Mewtype / BanG Dream! / 成员中文名 / アニソン=动画歌曲 / 歌枠=歌回） |
| 总结侧重 | ポンコツ可爱 + 多才创作者口吻；抓 **歌枠 setlist（曲名/原作）**、雑談话题与 ポンコツ名场面、乐队提及、招牌「やほ」与情绪节点 |
| 爆火侧重（二次创作） | 歌枠翻唱的 アニソン 是否命中 B 站当前热门歌曲/热番；ポンコツ名场面；高能演唱段 |

处理引擎复用 Radio `run_pipeline`（STT→翻译→摘要），clipper 在调用前把方案的专名词典并入设置、并以方案的节目名作 `display_name`。

## 2. 归档方案（archiving）

| 项 | 口径 |
|---|---|
| 集合 collection | `minetsuki_ritsu`，落在 `RADIO_DATA_DIR/minetsuki_ritsu/`（复用现有数据根，radio_kg 检索/问答天然消费） |
| 期目录命名 | `<YYYY-MM-DD>_<直播标题>`（日期取 YouTube `upload_date`） |
| episode_label | 无 #N 期号 → 用「日期_标题」整名作 label。**注意**：归档目录名会去掉 `【】` 括号——否则 radio_kg 的 `parse_folder_metadata` 会把首个 `【歌枠】` 当成 label，坍缩成非唯一的「歌枠」 |
| KG 节目名 | `夢限大みゅーたいぷ 峰月律` |
| 入库口径 | `auto_policy=confirm` —— **自动总结入库、无人工审查**（冲突/纠偏 in-graph 解决，不进审批看板） |
| 源视频 | 保留（`keep_source_video`），供后续按时间戳切片二次创作 |
| 每期产物 | `03_ja_segments.json` / `04_bilingual_segments.json` / `05_summary.json` / `source/<video>.mp4` /（可选）`clips/` |

## 3. 用法

```bash
# 走一遍「处理 + 归档」（下载→STT/翻译/摘要→自动入库→可选切片）
python -m clip.cli new --program minetsuki_ritsu --url <youtube-url>
# 仅选材预览（下载 + 爆火分析，不入库不渲染）
python -m clip.cli new --program minetsuki_ritsu --url <youtube-url> --dry-run
```

> 全量处理依赖运行环境：Radio 的 STT/摘要 provider key（`Radio/.env`）+ Neo4j 在线（入库）。
> 缺失时各步会清晰报错并跳过，不影响已完成的下载/归档与切片分支。
