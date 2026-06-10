# ADR 0006：精细翻译作为可选 opt-in（Claude Haiku 4.5）

**日期**：2026-05-16  
**状态**：采纳

## 背景

ADR 0003 已确定日常翻译走 DeepSeek（性价比优先），并允许在质量不达标时"改一行环境变量切回 Claude"。
实际推进时发现需求更细：

- **日常 90% 的节目**：DeepSeek V4 Flash 配合术语库（ADR 0004）已经"足够好"——人名/角色名/歌曲名准确率高，
  口语化翻译够用，月成本可控。
- **少数高价值节目**：例如 Live 当天的特别企划、声优重要发表、生日会等场合，
  愿意为更细腻的翻译多花一点钱。
- **不希望每次都靠改配置切换**：那样会让"想要好翻译"成为一个仪式感强的事，反而少用了。

## 决策

引入 **`--fine-translation` CLI 标志**作为 opt-in 通道：

- 默认（不带 flag）：用 `translation.provider` + `translation.model`（DeepSeek V4 Flash）
- 带 `--fine-translation`：用 `translation.fine_provider` + `translation.fine_model`（Anthropic Claude Haiku 4.5）

两条路径在 `src/radio/translate.py` 里通过 `provider` 变量分支，复用相同的 batch + 段数校验 +
单段降级逻辑。Prompt 模板（包含术语库注入）也完全共享。

`scripts/main_oneshot.py` 和 `scripts/main_video.py` 都支持该 flag，并透传给 `run_pipeline(..., fine_translation=...)`。

## 理由

1. **零运维切换**：CLI flag 一次性、明确、零负担。不需要改配置文件、不需要重启服务。
2. **Haiku 而非 Sonnet**：Haiku 4.5 中日翻译质量明显优于 DeepSeek Flash，但比 Sonnet 便宜，
   单期翻译成本约 $0.10（vs DeepSeek $0.06，vs Sonnet $0.54）。在"日常 vs 偶尔精修"区间命中点最优。
3. **配置即文档**：`translation.fine_provider/fine_model` 写在 `config/config.yaml` 里，
   方便未来切到 Sonnet 或其他模型，而不需要改代码。
4. **复用 prompt + 术语库**：保证两条路径的术语遵循度一致，避免 "Haiku 翻法跟 DeepSeek 风格漂移"。

## 后果

- ✅ 用户决策颗粒度从"项目级"细化到"单次运行级"。
- ✅ DeepSeek 仍是默认，月度成本不增加。
- ⚠️ `translate.py` 多了一对镜像函数（`_translate_batch_anthropic` / `_translate_single_anthropic`），
  代码量增加约 30 行。两套调用语义类似但 SDK 不同（DeepSeek 走 HTTP，Anthropic 走 SDK），
  暂时未抽象通用接口——保留两条独立路径反而易读。
- ⚠️ 后续若加入第 3 种 provider（如 Gemini Pro 翻译），需要重构为 strategy/registry。当前规模未到。

## 关联文件

- `src/radio/translate.py` — `translate_segments(fine=True/False)` + `_translate_batch_anthropic`
- `src/radio/pipeline.py` — `run_pipeline(fine_translation=...)`
- `scripts/main_oneshot.py` — `--fine-translation` flag
- `scripts/main_video.py` — `--fine-translation` flag
- `config/config.yaml` — `translation.fine_provider/fine_model`
