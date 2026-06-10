"""StatsAgent: answers aggregation/statistics questions that retrieval cannot
(e.g. "how many distinct letter-writers", "who wrote the most mail").

It is a TOOL agent: the LLM maps a statistics question onto ONE of a fixed menu
of safe, parameterized graph-aggregation tools (no free Cypher — same security
boundary as the rest). Numbers in the answer come from the query result, not the
LLM, so there is no fabricated-statistic risk.
"""
from __future__ import annotations

import re

from config.settings import settings
from src.canonical import canonical_name, HOST
from src.llm.client import LLMClient, LLMError
from src.mcp_layer.graph_store import GraphStore

# domain vocabulary the router/mapper needs to know
VOCAB = (
    "实体类型(type): Listener=来信者/听众投稿者, Person=人物, Organization=事务所/组织, "
    "Program=节目, Project=企划, Segment=环节, Work=作品。"
    "关系(relation): 投稿=来信/投稿, 所属=所属事务所, 出演=出演。"
)

CLASSIFY_SYSTEM = (
    "判断用户问题是「统计聚合类」还是「普通检索类」。"
    "统计聚合类：涉及数量/多少/几位/总共/最多/排名/分布/平均/逐期统计 等需要对知识库做计数或排序。"
    "普通检索类：询问具体事实/原因/经过。"
    '只输出 JSON：{"kind": "stats" | "retrieval"}'
)

ROUTE_SYSTEM = (
    "判断用户问题属于以下哪一类，并在需要时抽取目标实体名。\n"
    "- dossier（实体档案／完整追溯）：针对【某一个具体的人物或来信者的人名】，要求其【全部/所有/完整/历来】"
    "的记录。例如「村上まなつ都写过哪些信」「追溯XX的全部来信」「XX做过什么/干了哪些事」"
    "「关于XX的所有记录」。这类问题必须能识别出一个具体的【人名】name；"
    "若问题里的关键词是【话题/作品/关键词】（如「失眠」「某部动画」），而不是某个人，则不属于 dossier。\n"
    "- stats（统计聚合）：数量/多少/几位/最多/排名/分布/逐期计数、以及『来信主题排名』『来信最多的听众』等需要对全库做计数或排序的问题。\n"
    "- retrieval（普通检索）：其它询问具体事实/原因/经过、关键词在哪些期出现过、或不针对单一人物做完整罗列的问题。\n"
    '只输出 JSON：{"kind":"dossier|stats|retrieval","name":"人名(仅 dossier 必填，否则空字符串)"}'
)

DOSSIER_SYSTEM = (
    "你是知识库的人物/来信者档案助手。下面给出关于目标对象的【全部】图谱事实记录，"
    "部分附带广播原话片段。请据此【完整、无遗漏】地汇总该对象的全部记录：\n"
    "- 若是来信者(投稿/来信)，逐条说明每封来信出现在哪一期、主题或内容是什么；\n"
    "- 若是人物，罗列其做过的所有事、参与的企划/作品/出演、所属关系等；\n"
    "- 按期数（时间）顺序组织，可适当归类，但不得省略任何一条记录；\n"
    "- 每条事实后保留对应的【出处:...】标记，不要改写出处；\n"
    "- 只依据给定记录作答，不臆造；记录之外的内容不要补充。\n"
    "- 结尾用一句话给出总量（共多少条记录、涉及多少期）。"
)

MAP_SYSTEM = f"""把用户的统计问题映射到下列工具之一。{VOCAB}
可用工具:
- count_type(type): 统计某类型实体数量（如来信者总数 type=Listener）。
- list_type(type): 列出某类型实体名称。
- count_relation(relation): 统计某关系的边总数（如来信总条目 relation=投稿）。
- top_subjects(relation, n): 按某关系出现次数排名的主体（如来信最多者 relation=投稿）。
- per_episode(relation): 某关系按期数的分布。
- type_distribution(): 各实体类型的数量分布。
只输出 JSON：{{"tool": "...", "type": "...", "relation": "...", "n": 10}}（无关字段可省略）"""


class StatsAgent:
    def __init__(self, llm: LLMClient, graph: GraphStore, vector=None, mail_analytics=None):
        self.llm = llm
        self.graph = graph
        self.vector = vector
        self.mail_analytics = mail_analytics
        self._route_fewshot = self._load_route_fewshot()

    @staticmethod
    def _load_route_fewshot(per_route: int = 4) -> str:
        """Curated few-shot from the behavioral-knowledge exemplars
        (`persona/route_exemplars.json`): teaches the router the decision
        boundary (question -> route + why) by example, not by memorizing
        answers. Empty string if the artifact isn't built yet."""
        import json
        path = settings.abspath("persona") / "route_exemplars.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        lines = []
        for route in ("stats", "dossier", "retrieval"):
            for e in (data.get(route) or [])[:per_route]:
                q = str(e.get("question", "")).strip()
                why = str(e.get("routing_rationale", "")).strip()
                if q:
                    lines.append(f'  问:「{q}」-> {route}（依据:{why}）')
        if not lines:
            return ""
        return "\n参考范例（学习如何判断路由，不要照抄答案）：\n" + "\n".join(lines)

    def is_stats(self, question: str) -> bool:
        try:
            d = self.llm.complete_json(CLASSIFY_SYSTEM, question, max_tokens=64)
            return (d.get("kind") or "").lower() == "stats"
        except LLMError:
            return bool(self._heuristic_plan(question).get("tool"))

    # ── episode metadata (broadcast date) — deterministic, no LLM ─────
    # "第37期是什么时候放送的" type questions: the air date lives as edge
    # metadata (broadcast_date), never in the transcript/summary text, so
    # retrieval can't surface it and the no-fabrication verifier abstains.
    # Detect them up front and answer from a direct graph query instead.
    _EP_RE = re.compile(r"(?:第\s*|#|＃)?\s*(\d{1,4})\s*(?:期|回)")
    _AIRDATE_KW = ("放送", "播出", "播放", "上线", "上線", "开播", "開播",
                   "什么时候", "什麼時候", "何時", "何时", "几时", "幾時",
                   "日期", "哪天", "哪一天", "几号", "幾號", "几月", "幾月", "日子")

    def _episode_meta_q(self, question: str) -> bool:
        return bool(self._EP_RE.search(question)) and any(k in question for k in self._AIRDATE_KW)

    @staticmethod
    def _fmt_date(d: str) -> str:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", d or "")
        return f"{int(m.group(1))}年{int(m.group(2))}月{int(m.group(3))}日" if m else (d or "")

    def _episode_meta_answer(self, question: str) -> dict | None:
        m = self._EP_RE.search(question)
        if not m:
            return None
        ep = int(m.group(1))
        pm = re.search(r"《([^》]+)》", question)
        rows = self.graph.episode_broadcast_date(ep, pm.group(1) if pm else "")
        if not rows:
            # date unknown for this episode → let retrieval try instead of
            # asserting a date we don't have.
            return {"fallback": True, "tool": "episode_meta", "result": None}
        by_date: dict[str, list[dict]] = {}
        for r in rows:
            by_date.setdefault(r["broadcast_date"], []).append(r)
        lines, sources = [], []
        for date, rs in sorted(by_date.items()):
            progs = sorted({r["program"] for r in rs if r.get("program")})
            prog = progs[0] if progs else ""
            head = f"《{prog}》第{ep}期" if prog else f"第{ep}期"
            lines.append(f"{head} 的放送日期是 {self._fmt_date(date)}。【出处:{head}】")
            sources.append({"text": f"{head} 放送日期 {date}", "citation": head, "origin": "graph"})
        return {"answer": "\n".join(lines), "tool": "episode_meta", "result": rows, "sources": sources}

    def route(self, question: str) -> dict:
        """Single 3-way router used by the server: stats | dossier | retrieval
        (+ entity name for dossier). One LLM call replaces a separate is_stats
        check. Falls back to the keyword heuristic on LLM failure."""
        # deterministic short-circuit: episode broadcast-date questions are
        # answered from graph metadata (handled in answer()), not retrieval.
        if self._episode_meta_q(question):
            return {"kind": "stats", "name": ""}
        try:
            system = ROUTE_SYSTEM + (self._route_fewshot or "")
            d = self.llm.complete_json(system, question, max_tokens=96)
            kind = (d.get("kind") or "").lower()
            if kind not in ("stats", "dossier", "retrieval"):
                kind = "retrieval"
            return {"kind": kind, "name": (d.get("name") or "").strip()}
        except LLMError:
            return {"kind": "stats" if self._heuristic_plan(question).get("tool") else "retrieval",
                    "name": ""}

    # ── entity dossier: complete, untruncated trace for one entity ────
    def dossier(self, name: str, question: str = "") -> dict | None:
        """Pull EVERY graph record for a named person/listener (plus the exact
        transcript window for each), then compose a complete cited answer.
        Completeness-first: no top-n truncation. Returns None if the name does
        not resolve, so the caller can fall back to normal retrieval."""
        if not name:
            return None
        # A dossier of the host is the entire graph — meaningless and it just
        # dumps/aborts. Thematic questions that merely name the host (e.g. "the
        # host's recommended books", "how her speaking style changed") belong in
        # retrieval, so decline and let the caller fall back.
        if canonical_name(name) == HOST or name.strip() in (HOST, "羊宮", "妃那"):
            return None
        entities = self.graph.resolve_entities(name)
        if not entities:
            return None
        eids = [e["eid"] for e in entities if e.get("eid")]
        names = sorted({e["name"] for e in entities if e.get("name")})
        records = self.graph.entity_records(eids)
        if not records:
            return None

        target_norm = {n.strip().lower() for n in names}
        fact_lines, sources = [], []
        windows: dict[tuple, str] = {}
        episodes = set()
        for r in records:
            subj, rel, obj = r.get("subject", ""), r.get("relation", ""), r.get("object", "")
            cit = r.get("citation") or ""
            ep = r.get("episode")
            if ep is not None:
                episodes.add(ep)
            expired = "（历史/已更新）" if r.get("end_epoch") is not None else ""
            line = f"- {subj} 「{rel}」 {obj}{expired} 【出处:{cit}】"
            fact_lines.append(line)
            sources.append({"text": f"{subj} —{rel}→ {obj}", "citation": cit, "origin": "graph"})
            # enrich with the spoken context for the entity's own actions/mails
            if (self.vector is not None and ep is not None
                    and subj.strip().lower() in target_norm
                    and r.get("start_time") is not None and len(windows) < 150):
                key = (ep, round(float(r["start_time"]), 1))
                if key not in windows:
                    try:
                        rows = self.vector.get_window(
                            ep, float(r["start_time"]), float(r.get("end_time") or r["start_time"]),
                            episode_label=r.get("episode_label") or "")
                        text = "\n".join(x["text"] for x in rows)[:1200]
                        if text:
                            windows[key] = f"【出处:{cit}】\n{text}"
                    except Exception:
                        pass

        context = "目标对象：" + "、".join(names) + "\n\n【全部图谱记录】\n" + "\n".join(fact_lines)
        if windows:
            context += "\n\n【相关广播原话片段】\n" + "\n\n".join(windows.values())
        user = f"问题：{question}\n\n{context}" if question else context
        answer = self.llm._complete_text(
            DOSSIER_SYSTEM, user, max_tokens=settings.qa_answer_max_tokens)
        return {
            "answer": answer,
            "names": names,
            "n_records": len(records),
            "n_episodes": len(episodes),
            "sources": sources,
        }

    def _map(self, question: str) -> dict:
        """Map a stats question to a tool plan. LLM first (one retry), then a
        keyword heuristic so transient empty/invalid LLM output never dead-ends."""
        for _ in range(2):
            try:
                plan = self.llm.complete_json(MAP_SYSTEM, question, max_tokens=128)
                if plan.get("tool"):
                    return plan
            except LLMError:
                pass
        return self._heuristic_plan(question)

    @staticmethod
    def _heuristic_plan(question: str) -> dict:
        """Deterministic fallback for the common stats questions."""
        q = question
        mail = any(t in q for t in ("来信", "投稿", "お便り", "メール", "听众", "聽眾", "来信者", "來信人", "来信人"))
        rank = any(t in q for t in ("最多", "排名", "谁", "誰", "哪位", "哪个", "top", "最常"))
        count = any(t in q for t in ("多少", "几位", "幾位", "几个", "总共", "總共", "总数", "数量", "有多少"))
        per_ep = any(t in q for t in ("逐期", "每期", "按期", "分布"))
        listing = any(t in q for t in ("列出", "有哪些", "都有谁", "名单", "列表"))
        if mail and per_ep:
            return {"tool": "per_episode", "relation": "投稿"}
        if mail and rank:
            return {"tool": "top_subjects", "relation": "投稿", "n": 10}
        if mail and listing:
            return {"tool": "list_type", "type": "Listener"}
        if mail and count:
            return {"tool": "count_type", "type": "Listener"}
        if "类型" in q or "分布" in q:
            return {"tool": "type_distribution"}
        return {}

    # mail-analytics intent detection (corpus-wide, over mail_exemplars.json)
    _MAIL_CTX = ("来信", "投稿", "お便り", "メール", "听众", "聽眾", "来信者",
                 "來信人", "来信人", "粉丝来信", "粉絲來信", "投稿者")
    _THEME_KW = ("主题", "主題", "話題", "话题", "最常被探讨", "最常探讨", "最常聊",
                 "探讨的主题", "テーマ", "什么主题", "哪些主题", "前三", "前3")
    _RANK_KW = ("最多", "排名", "传奇听众", "傳奇聽眾", "谁", "誰", "哪位", "收到最多",
                "次数最多", "最常投", "top")

    def _mail_answer(self, question: str) -> dict | None:
        """Route mail-statistics questions to corpus-wide MailAnalytics. Returns
        None when this isn't a mail-stats question or analytics is unavailable."""
        if self.mail_analytics is None or not self.mail_analytics.available:
            return None
        q = question
        if not any(t in q for t in self._MAIL_CTX):
            return None
        if any(t in q for t in self._THEME_KW):
            r = self.mail_analytics.theme_distribution_answer(n=3)
            return None if r.get("fallback") else {"tool": "mail_theme", **r}
        if any(t in q for t in self._RANK_KW):
            r = self.mail_analytics.sender_ranking_answer(n=10)
            return None if r.get("fallback") else {"tool": "mail_rank", **r}
        return None

    def answer(self, question: str) -> dict:
        """Return {answer, tool, result} for a statistics question. When the
        question maps to no safe tool, returns {"fallback": True} so the caller
        can fall back to normal retrieval instead of dead-ending."""
        em = self._episode_meta_answer(question)
        if em is not None:
            return em
        mail = self._mail_answer(question)
        if mail is not None:
            return mail
        # guest questions have no clean graph relation (出演 = the host's own
        # appearances, not visitors) → let retrieval handle them.
        if any(t in question for t in ("嘉宾", "嘉賓", "来宾", "來賓", "ゲスト")):
            return {"fallback": True, "tool": None, "result": None}
        plan = self._map(question)
        tool = plan.get("tool")
        etype = plan.get("type") or ""
        relation = plan.get("relation") or ""
        n = int(plan.get("n") or 10)

        if tool == "count_type":
            c = self.graph.count_by_type(etype)
            ans = f"知识库中「{self._type_zh(etype)}」共有 {c} 个。"
            return {"answer": ans, "tool": tool, "result": c}
        if tool == "count_relation":
            c = self.graph.count_relation(relation)
            ans = f"关系「{relation}」共有 {c} 条记录。"
            return {"answer": ans, "tool": tool, "result": c}
        if tool == "list_type":
            rows = self.graph.list_by_type(etype)
            names = [r["name"] for r in rows]
            ans = f"「{self._type_zh(etype)}」共 {len(names)} 个：" + "、".join(names)
            return {"answer": ans, "tool": tool, "result": names}
        if tool == "top_subjects":
            # mail rankings are better served by corpus-wide MailAnalytics
            # (adds favourite segment + citations); use it when available.
            if relation in ("投稿", "投稿する") and self.mail_analytics \
                    and self.mail_analytics.available:
                r = self.mail_analytics.sender_ranking_answer(n=n)
                if not r.get("fallback"):
                    return {"tool": "mail_rank", **r}
            rows = self.graph.top_subjects_by_relation(relation, n)
            listed = "；".join(f"{r['name']}（{r['cnt']}次）" for r in rows)
            ans = f"按「{relation}」次数排名前 {len(rows)}：{listed}。"
            return {"answer": ans, "tool": tool, "result": rows}
        if tool == "per_episode":
            rows = self.graph.relation_per_episode(relation)
            listed = "，".join(f"第{r['episode']}期 {r['n']}条" for r in rows)
            ans = f"「{relation}」逐期分布：{listed}。"
            return {"answer": ans, "tool": tool, "result": rows}
        if tool == "type_distribution":
            rows = self.graph.type_distribution()
            listed = "，".join(f"{self._type_zh(r['type'])} {r['n']}" for r in rows)
            ans = f"实体类型分布：{listed}。"
            return {"answer": ans, "tool": tool, "result": rows}
        # no safe tool matched → let the caller fall back to retrieval
        return {"fallback": True, "tool": tool, "result": None}

    @staticmethod
    def _type_zh(t: str) -> str:
        return {"Listener": "来信者", "Person": "人物", "Organization": "组织/事务所",
                "Program": "节目", "Project": "企划", "Segment": "环节",
                "Work": "作品"}.get(t, t or "实体")
