"""Distill 'behavioral knowledge': representative routed Q&A exemplars.

For each of the three answering routes — 统计聚合(stats) / 实体档案(dossier) /
深度检索(retrieval) — generate ~20 fan-voice questions WITH the routing
rationale and the answering *method* (not just an answer). The point is to teach
the connected API **how** this system decides a route and shapes a grounded,
cited answer — the method, not memorized answers.

Output: `persona/route_exemplars.json`. A curated few-shot subset is injected
into the router prompt at runtime (see StatsAgent.load_route_fewshot()).

Re-runnable. Run:  .venv/bin/python -m src.build_route_exemplars
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.canonical import HOST  # noqa: E402
from src.llm.client import LLMClient, LLMError  # noqa: E402

OUT_PATH = settings.abspath("persona") / "route_exemplars.json"
PROGRAM = settings.program_name

# capability description the generator must respect (mirrors the live router/tools)
ROUTE_SPECS = {
    "stats": {
        "name_zh": "统计聚合",
        "desc": "对全库做计数/排序/分布：来信最多的听众、来信主题 top3、各类型实体数量、"
                "某关系逐期分布、嘉宾次数等。答案的【数字来自图谱/全语料统计，不由模型编造】。",
        "method": "路由到 StatsAgent / MailAnalytics 工具：count_type / top_subjects / "
                  "per_episode / mail 主题分布 / mail 发信人排行(带环节+出处)。答案给出排名/数量"
                  "+示例出处。",
    },
    "dossier": {
        "name_zh": "实体档案（完整追溯）",
        "desc": f"针对【某一个具体人名（听众或人物，但不是主持人{HOST}本人）】，要求其全部/历来记录："
                "某来信者写过的所有信、某人参与的所有企划/出演。",
        "method": "路由到 StatsAgent.dossier(name)：取该实体双向全部边(无截断)+对应广播原话窗口，"
                  "按期数完整罗列、逐条带【出处】，结尾给总量。主持人本人不可做 dossier(=整张图)。",
    },
    "retrieval": {
        "name_zh": "深度检索",
        "desc": "询问具体事实/原因/经过、某关键词在哪些期出现、对比/趋势/心路、内輪ネタ的诞生等，"
                "不针对单一人物做完整罗列。",
        "method": "路由到两阶段检索(摘要路由→精确窗口+图谱分支→RRF 融合)，再以"
                  "『事实句→source_id→citation』结构化生成并做后置校验，未被来源支撑的句子丢弃。",
    },
}

GEN_SYSTEM_TMPL = """你是顶级声优广播领域的知识工程专家，正在为电台节目《{program}》（主持人{host}）的
问答系统建立「行为知识库」。该系统有三条回答路由：统计聚合 / 实体档案 / 深度检索。

现在只针对【{route_zh}】这一条路由，生成 {n} 个**极具代表性**的提问范本。要求：
- 用**真实粉丝的口吻**（自然、口语、带点亲昵或好奇），像听众真的会问的那样；问题要多样、覆盖该路由的不同子情形。
- 该路由的定位：{desc}
- 该路由的处理方式：{method}

每条范本输出以下字段：
- question：粉丝口吻的问题。
- route：固定为 "{route}"。
- routing_rationale：一句话说明【为什么该问题应走这条路由】（路由依据，要点出可辨识的信号词/意图）。
- method：一句话说明系统应【如何作答】（用什么工具/检索 + 答案结构），强调事实要带【出处】。
- standard_answer：一个**标准答案范例**，重点展示答案的**结构与方式**（事实句＋【出处:《{program}》第N期 时间戳】占位），
  而不是要求记住具体数值；可用合理占位（如「にらら（约X次）」「第N期」）。

只输出 JSON：{{"exemplars":[{{"question":"","route":"{route}","routing_rationale":"","method":"","standard_answer":""}}]}}"""


def generate_route(llm: LLMClient, route: str, n: int = 20) -> list[dict]:
    spec = ROUTE_SPECS[route]
    system = GEN_SYSTEM_TMPL.format(
        program=PROGRAM, host=HOST, route=route, route_zh=spec["name_zh"],
        desc=spec["desc"], method=spec["method"], n=n)
    out: list[dict] = []
    # generate in two halves so neither JSON reply gets truncated
    for half in (n // 2, n - n // 2):
        for _ in range(2):
            try:
                d = llm.complete_json(system, f"请生成 {half} 条。", max_tokens=4096)
                ex = d.get("exemplars") or []
                if ex:
                    out.extend(ex)
                    break
            except LLMError:
                pass
    # normalize + dedupe by question
    seen, clean = set(), []
    for e in out:
        q = str(e.get("question", "")).strip()
        if not q or q in seen:
            continue
        seen.add(q)
        clean.append({
            "question": q,
            "route": route,
            "routing_rationale": str(e.get("routing_rationale", "")).strip(),
            "method": str(e.get("method", "")).strip(),
            "standard_answer": str(e.get("standard_answer", "")).strip(),
        })
    return clean[:n]


def build():
    llm = LLMClient()
    all_ex = {}
    for route in ("stats", "dossier", "retrieval"):
        ex = generate_route(llm, route, n=20)
        all_ex[route] = ex
        print(f"{route}: {len(ex)} exemplars", flush=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(all_ex, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(len(v) for v in all_ex.values())
    print(f"wrote {total} exemplars -> {OUT_PATH}")


if __name__ == "__main__":
    build()
