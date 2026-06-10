"""20-question experiment harness for the QA system.

Runs the user's 20 analytical questions against the live server (`/api/ask`,
which does full stats|dossier|retrieval routing), then LLM-judges each answer on
relevance / completeness / grounding, and flags whether the system *punted*
(refused / "can't find" / empty). Captures which route each question took so we
can see capability gaps (e.g. a frequency question that fell into plain
retrieval and produced a vague answer).

Run:
  .venv/bin/python -m src.eval_qa20 --server http://127.0.0.1:8077
  .venv/bin/python -m src.eval_qa20 --only 2,5,17        # subset
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.llm.client import LLMClient, LLMError  # noqa: E402

# Each: id, category, question, need = what a fully-satisfying answer must contain.
# answerable = best-effort prior on whether the data can support it at all.
QUESTIONS = [
    # 一、核心梗与高频词
    {"id": 1, "cat": "梗文化", "answerable": "partial",
     "q": "节目中某个特定的「内輪ネタ（内部梗）」或流行语，最早是在哪一期、因为什么契机诞生的？",
     "need": "指出一个具体的梗/流行语，给出最早出现的期数和诞生契机，并附出处。"},
    {"id": 2, "cat": "高频词", "answerable": "weak",
     "q": "在所有往期节目中，羊宮妃那被提及次数最多的前5个高频词或口头禅是什么？",
     "need": "列出排名前5的高频词/口头禅，最好带出现频次或所在期数。"},
    {"id": 3, "cat": "关键词检索", "answerable": "yes",
     "q": "如果搜「失眠/睡眠」这个关键词，羊宮妃那在哪些期数里深度聊过？",
     "need": "列出多个具体期数，说明各期聊的内容，附出处。"},
    {"id": 4, "cat": "事件计数", "answerable": "partial",
     "q": "节目中著名的「惩罚游戏」或「神回」总共出现过多少次？分别在哪几期？",
     "need": "给出次数和具体期数列表，附出处。"},
    # 二、粉丝投稿与互动
    {"id": 5, "cat": "来信主题", "answerable": "yes",
     "q": "在所有被读到的听众来信（ふつおた）中，最常被探讨的主题前三名是什么（如恋爱咨询、职场烦恼、圣地巡礼汇报）？",
     "need": "给出排名前三的主题及大致占比/频次，附代表性来信出处。"},
    {"id": 6, "cat": "来信排名", "answerable": "yes",
     "q": "有没有哪位「传奇听众」的来信被读到的次数最多？他/她最常投哪个环节？",
     "need": "给出来信最多的听众名字、次数，以及最常投的环节。"},
    {"id": 7, "cat": "来信排名", "answerable": "yes",
     "q": "羊宮妃那在所有广播中，收到过来自最多的是谁的粉丝来信？",
     "need": "给出来信次数最多的听众名字与次数。"},
    {"id": 8, "cat": "作品评价", "answerable": "partial",
     "q": "当听众在来信中提到某部特定出演作品时，羊宮妃那通常会给出怎样普遍的评价或回应？",
     "need": "举出具体作品，概括羊宮妃那的普遍评价/回应方式，附出处。"},
    # 三、嘉宾与人际
    {"id": 9, "cat": "嘉宾", "answerable": "partial",
     "q": "哪位嘉宾来节目的次数最多？",
     "need": "给出嘉宾名字与到访次数（若节目无嘉宾，应说明）。"},
    {"id": 10, "cat": "对比分析", "answerable": "partial",
     "q": "对比有嘉宾的期数和羊宮妃那独自主持的期数，话题方向有什么明显变化？",
     "need": "对比两类期数的话题差异，给出具体观察，附出处。"},
    {"id": 11, "cat": "人脉提及", "answerable": "partial",
     "q": "在没有嘉宾来访的常规节目里，羊宮妃那最常提及的其他声优朋友（业界人脉）是谁？",
     "need": "给出最常被提及的声优朋友名字（可排名），附出处。"},
    {"id": 12, "cat": "关系演变", "answerable": "weak",
     "q": "某两位声优同台的期数中，他们互称的昵称经历过怎样的演变（例如从客套到亲昵）？",
     "need": "给出昵称演变的具体描述（若数据不足应说明）。"},
    # 四、个人成长
    {"id": 13, "cat": "情绪追踪", "answerable": "yes",
     "q": "纵观整个节目史，羊宮妃那聊到「压力」「迷茫」或「遇到瓶颈」的期数有哪些？当时是因为什么事件？",
     "need": "列出多个期数及对应事件，附出处。"},
    {"id": 14, "cat": "目标追踪", "answerable": "partial",
     "q": "在历年的「生日回」或「新年第一期」节目中，羊宮妃那分别许下了什么愿望/目标？后来在广播里汇报实现了吗？",
     "need": "列出相关期数的愿望/目标，并说明是否有后续汇报。"},
    {"id": 15, "cat": "清单汇总", "answerable": "yes",
     "q": "羊宮妃那在节目里推荐过的「私房歌单」「爱看的小说/漫画」或「爱吃的东西」完整清单是什么？",
     "need": "尽量完整地列出推荐项目（歌/书/食物），附出处。"},
    {"id": 16, "cat": "风格趋势", "answerable": "weak",
     "q": "从第一期到最后一期，羊宮妃那的说话风格、常用语、甚至单期大笑的频率发生了怎样的变化？",
     "need": "给出风格/用语随时间的变化趋势（若数据不足应说明）。"},
    # 五、运营编年史
    {"id": 17, "cat": "编年史", "answerable": "yes",
     "q": "节目历史上总共经历过多少次「重大发表」（如开Live、出广播CD、公开录音、更换企划环节）？请按时间轴列出。",
     "need": "按时间轴列出重大发表事件及期数，附出处。"},
    {"id": 18, "cat": "运营心声", "answerable": "partial",
     "q": "每当节目收听率达到里程碑或遭遇播放时间段调整时，羊宮妃那在节目里流露出了怎样的真实想法？",
     "need": "给出相关期数及羊宮妃那的真实想法（若数据不足应说明）。"},
    {"id": 19, "cat": "不可答", "answerable": "no",
     "q": "官方推特或博客随广播发布的「After Talk 照片」中，羊宮妃那最常摆出的拍照姿势（Pose）是什么？",
     "need": "数据中无图像信息，正确行为是说明无法从现有资料判断，而非编造。"},
    {"id": 20, "cat": "元建议", "answerable": "yes",
     "q": "基于往期所有数据，如果我想给羊宮妃那写一封「最容易被读到且能引发深度共鸣」的信，应该选择什么主题、用什么语气？",
     "need": "基于往期被读到的来信规律给出主题与语气建议，最好引用往期证据。"},
]

JUDGE_SYSTEM = (
    "你是问答系统的严格评估者。给定【问题】【这道题完整回答应满足的要求】【系统回答】【系统检索到的出处】，"
    "用 JSON 输出四项评分（0~1 浮点）和判断：\n"
    "- relevance: 回答是否切题、答到了问题的核心。\n"
    "- completeness: 回答是否满足『应满足的要求』里列举的全部要点（如要求前5个就要有5个、要求列期数就要真的列出期数）。"
    "信息缺一项就相应扣分。\n"
    "- grounding: 回答内容是否有出处支撑、未凭空编造（带【出处】且与检索到的出处一致得高分；空泛无据或疑似编造得低分）。"
    "对于本就无法从资料回答的问题，如实说明『无法判断/资料中没有』属于正确行为，grounding 与 relevance 都应给高分。\n"
    "- punt: 布尔值，true 表示系统实质上回避了问题（如『暂不支持』『无法回答』『没有相关信息』而未给出任何有用内容）。"
    "注意：对确实无法回答的问题如实说明，不算消极回避，punt=false。\n"
    '输出严格的 JSON：{"relevance":0~1,"completeness":0~1,"grounding":0~1,"punt":true/false,"reason_zh":"理由"}。'
    "reason_zh 控制在25个汉字以内，不要引用回答原文、不要使用引号或换行，避免破坏 JSON。"
)


def ask(server: str, question: str) -> dict:
    body = json.dumps({"question": question}).encode()
    req = urllib.request.Request(f"{server}/api/ask", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=480))


def judge(llm: LLMClient, item: dict, answer: str, sources: list[dict]) -> dict:
    src = "\n".join(f"- ({s.get('origin','')}) {s.get('text','')[:160]} 【出处:{s.get('citation','')}】"
                    for s in sources[:25]) or "（无）"
    user = (f"【问题】{item['q']}\n\n【应满足的要求】{item['need']}\n\n"
            f"【系统回答】\n{answer[:4000]}\n\n【系统检索到的出处】\n{src[:3000]}")
    last = ""
    for _ in range(3):
        try:
            d = llm.complete_json(JUDGE_SYSTEM, user, max_tokens=800)
            return {"relevance": float(d.get("relevance", 0)),
                    "completeness": float(d.get("completeness", 0)),
                    "grounding": float(d.get("grounding", 0)),
                    "punt": bool(d.get("punt", False)),
                    "reason": d.get("reason_zh", "")}
        except (LLMError, ValueError, TypeError) as e:
            last = str(e)
            salvaged = _salvage(str(e))
            if salvaged:
                return salvaged
    return {"relevance": 0, "completeness": 0, "grounding": 0, "punt": True,
            "reason": f"judge_failed:{last}"}


def _salvage(err: str) -> dict | None:
    """LLMError embeds the raw model text after '---'. When json.loads chokes on
    a stray char (e.g. an unescaped quote in reason_zh) but the numeric fields
    are present, recover them by regex so a good answer isn't scored 0 over a
    judge formatting glitch."""
    g = lambda k: re.search(rf'"{k}"\s*:\s*([0-9.]+)', err)
    rel, comp, grnd = g("relevance"), g("completeness"), g("grounding")
    if not (rel and comp and grnd):
        return None
    pm = re.search(r'"punt"\s*:\s*(true|false)', err)
    rm = re.search(r'"reason_zh"\s*:\s*"([^"]*)', err)
    return {"relevance": float(rel.group(1)), "completeness": float(comp.group(1)),
            "grounding": float(grnd.group(1)),
            "punt": bool(pm and pm.group(1) == "true"),
            "reason": (rm.group(1) if rm else "") + "（salvaged）"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8077")
    ap.add_argument("--only", help="comma-separated question ids to run")
    ap.add_argument("--out", default="data/eval_qa20.json")
    args = ap.parse_args()

    only = {int(x) for x in args.only.split(",")} if args.only else None
    llm = LLMClient()
    rows = []
    for item in QUESTIONS:
        if only and item["id"] not in only:
            continue
        print(f"\n■ Q{item['id']} [{item['cat']}] {item['q']}")
        try:
            d = ask(args.server, item["q"])
        except Exception as e:
            print(f"  ASK FAILED: {e}")
            rows.append({"id": item["id"], "cat": item["cat"], "error": str(e)})
            continue
        answer = d.get("answer", "")
        sources = d.get("sources", [])
        mode = d.get("mode", "?")
        j = judge(llm, item, answer, sources)
        print(f"  route={mode}  src={len(sources)}  graph_hits={d.get('graph_hits',0)}")
        print(f"  答案: {answer[:200].replace(chr(10),' ')}")
        print(f"  rel={j['relevance']:.2f} comp={j['completeness']:.2f} "
              f"grnd={j['grounding']:.2f} punt={j['punt']}  | {j['reason']}")
        rows.append({"id": item["id"], "cat": item["cat"], "answerable": item["answerable"],
                     "route": mode, "n_sources": len(sources),
                     "answer": answer,
                     "sources": [{"text": s.get("text", "")[:160], "citation": s.get("citation", ""),
                                  "origin": s.get("origin", "")} for s in sources[:25]],
                     **j})

    scored = [r for r in rows if "relevance" in r]
    n = len(scored) or 1
    print("\n" + "=" * 64)
    print(f"问题数 {len(scored)}")
    print(f"平均 Relevance     : {sum(r['relevance'] for r in scored)/n:.3f}")
    print(f"平均 Completeness  : {sum(r['completeness'] for r in scored)/n:.3f}")
    print(f"平均 Grounding     : {sum(r['grounding'] for r in scored)/n:.3f}")
    print(f"Punt(回避) 数      : {sum(1 for r in scored if r['punt'])}/{len(scored)}")
    print("\n各题路由与得分:")
    for r in scored:
        flag = "⚠️" if (r["punt"] or r["completeness"] < 0.6 or r["relevance"] < 0.6) else "  "
        print(f" {flag} Q{r['id']:>2} [{r['route']:>9}] rel={r['relevance']:.2f} "
              f"comp={r['completeness']:.2f} grnd={r['grounding']:.2f} "
              f"{'PUNT' if r['punt'] else ''}  {r['cat']}")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已写入 {args.out}")


if __name__ == "__main__":
    main()
