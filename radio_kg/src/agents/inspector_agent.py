"""InspectorAgent: LLM-driven three-layer fact audit between Extractor and Sync.

A senior JA broadcast / ACGN data-proofreading expert. For every extracted
"tentative triple" it runs a three-layer defense (industry dictionary, Japanese
phonetic distance, historical graph context) to intercept ASR near-homophone
hallucinations (e.g. real「青二(あおに)」mis-heard as non-existent「青鬼(あおおに)」)
before dirty data reaches the temporal graph.

Output per triple maps to one of: APPROVED / AUTO_CORRECTED / HIGH_RISK_INTERRUPT.
Audits are batched (one LLM call per chunk/episode slice) per the prompt's
`audit_results` list contract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.llm.client import LLMClient, LLMError
from src.mcp_layer.graph_store import GraphStore, entity_id
from src.schema.models import Entity, InspectionIssue, Triple


@dataclass(frozen=True)
class DomainTerm:
    canonical: str
    type: str
    aliases: tuple[str, ...] = ()
    readings: tuple[str, ...] = ()
    confusable_aliases: tuple[str, ...] = ()


@dataclass
class InspectionResult:
    triple: Triple | None
    issues: list[InspectionIssue] = field(default_factory=list)
    review_required: bool = False
    suggested_triple: Triple | None = None


# local_domain_dictionary — 行业标准词汇（声优事务所专名等），喂给 LLM 做第①层比对
DOMAIN_TERMS = [
    DomainTerm("青二プロダクション", "Organization", ("青二", "青二プロ", "Aoni Production"),
               ("あおに", "アオニ", "aoni", "あおき"),
               ("青鬼プロダクション", "青鬼プロ", "青鬼",
                "青木プロダクション", "青木プロナクション", "青木プロ", "青鬼プロナクション")),
    DomainTerm("大沢事務所", "Organization", ("大沢", "大沢事務所"),
               ("おおさわじむしょ", "osawa"), ("大澤事務所",)),
    DomainTerm("アーツビジョン", "Organization", ("Arts Vision", "ARTSVISION"),
               ("あーつびじょん", "arts vision")),
    DomainTerm("アイムエンタープライズ", "Organization", ("I'm Enterprise", "アイム"),
               ("あいむえんたーぷらいず", "i'm enterprise")),
    DomainTerm("81プロデュース", "Organization", ("81 Produce", "エイティワンプロデュース"),
               ("えいてぃわんぷろでゅーす", "81 produce")),
    DomainTerm("シグマ・セブン", "Organization", ("Sigma Seven", "シグマセブン"),
               ("しぐませぶん", "sigma seven")),
]

_TYPE_BY_NAME = {}
for _t in DOMAIN_TERMS:
    _TYPE_BY_NAME[_t.canonical] = _t.type
    for _a in _t.aliases:
        _TYPE_BY_NAME[_a] = _t.type


SYSTEM = """# Role
你是一名资深的日语广播/ACGN行业数据校对专家（InspectorAgent）。你位于多Agent系统的核心质量控制环节，负责对 ASR 语音转文字后提取出的“临时三元组”进行严苛的真实性、声学同音、以及行业常识审核。

# Context & User Profile
- 数据源特征：日语广播剧/Talk Show。语速快、连读多、口语化严重，ASR 极其容易产生“同音/近音幻觉”。
- 业务目标：拦截由于发音相似导致的脏数据（例如：将真实的「青二（あおに）」误听为现实中不存在的「青鬼（あおおに）」），确保进入图数据库的知识具备硬核准确性。

# Core Defense Strategy (三层防御过滤机制)
对于每一个输入的临时三元组，你必须按照以下逻辑链进行深度审计：

1. 【行业常识与词典比对】：检查 object 或 subject 中的专有名词（事务所/企划/人名）。若该名词在现实常识中不存在，且与 local_domain_dictionary 的行业标准词汇存在极高的编辑距离相似度（Jaro-Winkler），必须怀疑其为 ASR 错误。
2. 【日语读音发音相似度（Phonetic Distance）】：将疑似错误文本转为假名/罗马字注音，对比发音（如「あおに(Aoni)」与「あおおに(Aooni)」「あおき(Aoki)」）。若读音仅一字之差或长音/元音混淆，且后者现实中无意义，判定为【ASR同音字幻觉】。
3. 【历史上下文拟合（Graph Context）】：结合 historical_graph_context。若历史图谱中该实体长期关联 A，而本期孤立地出现与其发音相似的 B，优先判定 B 为噪声，需修正为 A。

# 判级规则
- APPROVED：未发现问题，原样通过。
- AUTO_CORRECTED：三层防御高置信定位为 ASR 错误且修正目标明确（命中词典/历史图谱强证据），自动修正。
- HIGH_RISK_INTERRUPT：疑似错误但修正目标不唯一或证据不足，需人工复核。

# Output Format (Strict JSON)
你必须且只能输出标准 JSON，严禁任何前导词或后缀。audit_results 的顺序与数量必须与输入 triplets_to_audit 完全一致。corrected_triplet 在 APPROVED 时与 original_triplet 相同。
{
  "audit_results": [
    {
      "original_triplet": {"subject": "string", "predicate": "string", "object": "string"},
      "status": "APPROVED | AUTO_CORRECTED | HIGH_RISK_INTERRUPT",
      "corrected_triplet": {"subject": "string", "predicate": "string", "object": "string"},
      "reason_ja": "string",
      "reason_zh": "string"
    }
  ]
}"""


class InspectorAgent:
    def __init__(self, llm: LLMClient, graph: GraphStore, batch_size: int = 12):
        self.llm = llm
        self.graph = graph
        self.batch_size = batch_size

    # ── public API ────────────────────────────────────────────────
    def inspect(self, triple: Triple) -> InspectionResult:
        return self.inspect_batch([triple])[0]

    def inspect_batch(self, triples: list[Triple]) -> list[InspectionResult]:
        results: list[InspectionResult] = []
        for i in range(0, len(triples), self.batch_size):
            results += self._audit_slice(triples[i : i + self.batch_size])
        return results

    # ── one LLM call per slice ────────────────────────────────────
    def _audit_slice(self, triples: list[Triple]) -> list[InspectionResult]:
        if not triples:
            return []
        user = json.dumps(
            {
                "local_domain_dictionary": self._dictionary(),
                "historical_graph_context": self._graph_context(triples),
                "triplets_to_audit": [self._as_triplet(t) for t in triples],
            },
            ensure_ascii=False,
        )
        try:
            data = self.llm.complete_json(SYSTEM, user, max_tokens=4096)
            audits = data.get("audit_results", [])
        except LLMError:
            audits = []
        # align by position; on any shape mismatch, default to APPROVED (no data loss)
        out = []
        for idx, t in enumerate(triples):
            audit = audits[idx] if idx < len(audits) else None
            out.append(self._to_result(t, audit))
        return out

    # ── result mapping ────────────────────────────────────────────
    def _to_result(self, triple: Triple, audit: dict | None) -> InspectionResult:
        if not audit:
            return InspectionResult(triple=triple)
        status = (audit.get("status") or "APPROVED").upper()
        if status == "APPROVED":
            return InspectionResult(triple=triple)

        corrected = self._apply_correction(triple, audit.get("corrected_triplet") or {})
        role, orig_name, new_name = self._diff(triple, corrected)
        reason_zh = audit.get("reason_zh", "")
        reason_ja = audit.get("reason_ja", "")

        if status == "AUTO_CORRECTED":
            issue = self._issue(corrected, role, orig_name, new_name,
                                "auto_corrected", 0.95, reason_zh, reason_ja)
            return InspectionResult(triple=corrected, issues=[issue],
                                    suggested_triple=corrected)
        # HIGH_RISK_INTERRUPT
        issue = self._issue(triple, role, orig_name, new_name,
                            "review_required", 0.7, reason_zh, reason_ja)
        return InspectionResult(triple=triple, issues=[issue],
                                review_required=True, suggested_triple=corrected)

    def _apply_correction(self, triple: Triple, corrected: dict) -> Triple:
        updates: dict = {}
        new_subj = (corrected.get("subject") or "").strip()
        new_pred = (corrected.get("predicate") or "").strip()
        new_obj = (corrected.get("object") or "").strip()
        if new_subj and new_subj != triple.subject.name:
            updates["subject"] = self._rebuild_entity(triple.subject, new_subj)
            updates["subject_id"] = entity_id(updates["subject"].type, new_subj)
        if new_obj and new_obj != triple.object.name:
            updates["object"] = self._rebuild_entity(triple.object, new_obj)
            updates["object_id"] = entity_id(updates["object"].type, new_obj)
        if new_pred and new_pred != triple.relation:
            updates["relation"] = new_pred
        return triple.model_copy(update=updates) if updates else triple

    @staticmethod
    def _rebuild_entity(old: Entity, new_name: str) -> Entity:
        etype = _TYPE_BY_NAME.get(new_name, old.type)
        return Entity(name=new_name, type=etype, aliases=old.aliases)

    @staticmethod
    def _diff(orig: Triple, corrected: Triple) -> tuple[str, str, str]:
        if corrected.object.name != orig.object.name:
            return "object", orig.object.name, corrected.object.name
        if corrected.subject.name != orig.subject.name:
            return "subject", orig.subject.name, corrected.subject.name
        return "object", orig.object.name, corrected.object.name

    @staticmethod
    def _issue(triple, role, orig_name, new_name, severity, conf, reason_zh, reason_ja):
        return InspectionIssue(
            severity=severity,
            issue_type="llm_audit",
            entity_role=role,
            relation=triple.relation,
            original_name=orig_name,
            suggested_name=new_name,
            suggested_type=_TYPE_BY_NAME.get(new_name, ""),
            confidence=conf,
            mechanisms=["llm_three_layer"],
            reason=(reason_zh or reason_ja),
            source=triple.source,
        )

    # ── prompt context builders ───────────────────────────────────
    @staticmethod
    def _as_triplet(t: Triple) -> dict:
        return {"subject": t.subject.name, "predicate": t.relation, "object": t.object.name}

    @staticmethod
    def _dictionary() -> list[dict]:
        return [
            {
                "canonical": t.canonical,
                "type": t.type,
                "aliases": list(t.aliases),
                "readings": list(t.readings),
                "asr_confusable_forms": list(t.confusable_aliases),
            }
            for t in DOMAIN_TERMS
        ]

    def _graph_context(self, triples: list[Triple]) -> list[dict]:
        names = {t.subject.name for t in triples} | {t.object.name for t in triples}
        context = []
        for name in names:
            hits = self.graph.search_nodes(name)
            if not hits:
                continue
            eid = hits[0]["eid"]
            history = self.graph.relationship_object_counts(eid, None, limit=5)
            if history:
                context.append({
                    "entity": hits[0]["name"],
                    "known_relations": [
                        {"object": h["object_name"], "mentions": h["mentions"]}
                        for h in history
                    ],
                })
        return context
