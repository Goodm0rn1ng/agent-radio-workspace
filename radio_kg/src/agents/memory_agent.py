"""MemoryAgent: turn a user's natural-language statement into knowledge-base
edits (self-evolving memory).

The user is authoritative: explicit "记住：/订正：…" messages are parsed into a
small set of graph operations (add / update / delete triples), previewed for
confirmation, then written with a `用户订正` provenance. On conflict the existing
broadcast-derived edge is expired (history kept), the user edge wins.

Security boundary unchanged: this only emits structured ops; the actual writes
go through the fixed parameterized GraphStore tools, never free Cypher.
"""
from __future__ import annotations

import re
import time

from config.settings import settings
from src.canonical import canonical_name, canonical_type
from src.llm.client import LLMClient, LLMError
from src.mcp_layer.graph_store import GraphStore

# explicit triggers — a chat message is a KB edit only if it starts with one of
# these (decided with the user: explicit prefix, no silent auto-classification).
_TRIGGERS = ("记住", "訂正", "订正", "更正", "纠正", "糾正", "改为", "改成",
             "教学", "教學", "remember", "更新知识", "知识库")
# correction verbs → a conflicting existing object should be expired (user wins,
# history kept). plain "记住/remember" stays additive.
_CORRECTION = ("訂正", "订正", "更正", "纠正", "糾正", "改为", "改成")
_TRIGGER_RE = re.compile(
    r"^\s*(?:" + "|".join(re.escape(t) for t in _TRIGGERS) + r")\s*[:：]?\s*",
    re.IGNORECASE,
)

PARSE_SYSTEM = """你是知识库编辑助手。用户会用自然语言陈述一条或多条「事实订正/新增/删除」。
把它解析成结构化操作，供写入广播知识图谱。实体类型(type)取值：
Person=人物, Organization=事务所/组织, Program=节目, Project=企划, Segment=环节, Work=作品, Listener=来信者。
关系(relation)用简短动词短语，如：所属(所属事务所)、主持、出演、本名、别名、担当。

每条操作字段：
- op: "add"(新增事实) | "update"(订正/修改某关系的对象) | "delete"(删除某事实)
- subject / subject_type
- relation
- object / object_type
- note: 一句中文说明这条操作

要求：
- 忠实于用户陈述，不要臆测补充用户没说的事实。
- 人名/专名保持用户给出的写法。
- 无法解析出明确三元组时返回空列表。
只输出 JSON：{"ops": [{"op":"...","subject":"...","subject_type":"...","relation":"...","object":"...","object_type":"...","note":"..."}]}"""


class MemoryAgent:
    def __init__(self, llm: LLMClient, graph: GraphStore):
        self.llm = llm
        self.graph = graph

    @staticmethod
    def is_kb_update(message: str) -> bool:
        return bool(_TRIGGER_RE.match(message or ""))

    @staticmethod
    def strip_trigger(message: str) -> str:
        return _TRIGGER_RE.sub("", message or "", count=1).strip()

    def parse(self, message: str) -> list[dict]:
        """Parse a user statement into a list of cleaned graph operations."""
        statement = self.strip_trigger(message)
        if not statement:
            return []
        is_correction = any(t in (message or "") for t in _CORRECTION)
        try:
            data = self.llm.complete_json(PARSE_SYSTEM, statement, max_tokens=1024)
        except LLMError:
            return []
        out = []
        for o in data.get("ops", []):
            op = (o.get("op") or "").lower()
            subj = (o.get("subject") or "").strip()
            rel = (o.get("relation") or "").strip()
            obj = (o.get("object") or "").strip()
            if op not in ("add", "update", "delete") or not (subj and rel and obj):
                continue
            # a correction-verb trigger means "replace", so additive becomes update
            if is_correction and op == "add":
                op = "update"
            subj_c = canonical_name(subj) or subj
            obj_c = canonical_name(obj) or obj
            out.append({
                "op": op,
                "subject": subj_c,
                "subject_type": canonical_type(subj_c) if canonical_name(subj) else (o.get("subject_type") or "Person"),
                "relation": rel,
                "object": obj_c,
                "object_type": canonical_type(obj_c) if canonical_name(obj) else (o.get("object_type") or "Person"),
                "note": (o.get("note") or "").strip(),
            })
        return out

    def apply(self, ops: list[dict]) -> list[dict]:
        """Write confirmed ops to the graph. Returns per-op results."""
        now = int(time.time())
        date = time.strftime("%Y-%m-%d")
        citation = f"用户订正 @ {date}"
        results = []
        for o in ops:
            subj_eid = self.graph.merge_node(o["subject"], o["subject_type"])
            obj_eid = self.graph.merge_node(o["object"], o["object_type"])
            rel = o["relation"]
            if o["op"] == "delete":
                # keep history line: expire the active edge rather than hard-delete
                self.graph.expire_relationship(subj_eid, rel, obj_eid, now)
                results.append({**o, "status": "deleted(expired)"})
                continue
            if o["op"] == "update":
                # close any conflicting active object(s) of this relation, keep history
                for edge in self.graph.get_active_relationship(subj_eid, rel):
                    if edge["object_eid"] != obj_eid:
                        self.graph.expire_relationship(subj_eid, rel, edge["object_eid"], now)
            self.graph.merge_directed_relationship(
                subj_eid, rel, obj_eid,
                start_epoch=now, program=settings.program_name, episode=None,
                episode_label="用户订正", broadcast_date=date,
                start_time=None, end_time=None, source_type="user",
                file_name="", page=None, segment=None, confidence=1.0, citation=citation,
            )
            results.append({**o, "status": "written"})
        return results

    @staticmethod
    def preview_line(o: dict) -> str:
        verb = {"add": "新增", "update": "订正", "delete": "删除"}.get(o["op"], o["op"])
        return f"[{verb}] {o['subject']} —[{o['relation']}]→ {o['object']}"
