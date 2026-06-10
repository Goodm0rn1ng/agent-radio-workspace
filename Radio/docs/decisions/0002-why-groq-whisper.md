# ADR 0002：STT 选 Groq Whisper-large-v3

**日期**：2026-05-15  
**状态**：采纳

## 背景

候选方案：
- **Groq Whisper-large-v3 API**：约 $0.04/小时音频，速度约 10x 实时
- **Deepgram Nova-2**：日语识别质量略高，约 $0.20-0.40/小时
- **OpenAI Whisper API**：$0.006/分钟 = $0.36/小时
- **本地 whisper.cpp**：免费但 Mac M 系列以下慢；服务器要 GPU

## 决策

v1 用 Groq Whisper-large-v3。

## 理由

1. **成本最低**：单期 60 分钟节目 STT 成本约 $0.04，比 Deepgram 便宜 5-10 倍。
2. **速度最快**：60 分钟音频约 4-6 分钟出结果，满足"节目结束 5 分钟内推送"。
3. **质量够用**：日语识别 WER 在 10% 左右，对追星向内容（梗、人名）配合 prompt 引导可控。
4. **本地方案不划算**：v1 在 Mac 上跑，whisper.cpp 跑 large-v3 实时倍率 < 1，反而拖慢整体。

## 后果

- 正面：成本可忽略，速度满足 SLA。
- 负面：依赖外部 API，可用性不在我们手里。Plan B：若 Groq 长时间不可用，可临时切到 OpenAI Whisper（代码改动 < 20 行）。
