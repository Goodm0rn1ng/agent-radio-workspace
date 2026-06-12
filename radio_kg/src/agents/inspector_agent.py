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


ADJUDICATE_SYSTEM = """# Role
你是知识入库的终审裁决专家。前置审核 Agent 对下列三元组提出了疑点但无法定论。
你拿到的是比前置审核更完整的上下文：节目逐字稿原文片段（transcript_excerpt）、
历史图谱关联（historical_graph_context）、行业词典（local_domain_dictionary）、
以及前置审核的疑点说明（prior_doubt）。请基于全部上下文做出最终裁决，结果将直接入库，无人工复核。

# 裁决选项
- ACCEPT_CORRECTION：上下文支持修正。若 suggested_triplet 合理则采纳之；若你能从上下文推出更准确的修正，
  在 final_triplet 中给出（否则 final_triplet 与 suggested_triplet 相同）。
- KEEP_ORIGINAL：疑点不成立或证据不足以推翻原文，按逐字稿原样入库（宁可保留原文，不做无把握的改写）。
- DROP：该三元组本身是 ASR 幻听/无意义碎片/与上下文明显矛盾的噪声，不应入库。

# Output Format (Strict JSON)
decisions 的顺序与数量必须与输入 items 完全一致。
{"decisions": [{"decision": "ACCEPT_CORRECTION | KEEP_ORIGINAL | DROP",
  "final_triplet": {"subject": "string", "predicate": "string", "object": "string"},
  "reason_zh": "string"}]}"""


CONFLICT_ADJUDICATE_SYSTEM = """# Role
你是时序知识图谱的变更终审专家。单值关系（如「担当する」）出现了新旧值不一致，
需要你裁决这是真实的时间性变更、旧数据错误，还是新数据噪声。结果将直接入库，无人工复核。

# 裁决选项
- CONFIRM：新值是真实发生的变更（如节目环节更替、负责人变动）。旧边封存为历史线（保留 end_epoch），新边生效。
  逐字稿上下文或期数先后顺序支持「先有旧值、后有新值」时选它。
- OVERWRITE：旧值本身是错误数据（如早期 ASR 错误入库），新值才是正确的。删除旧边，不保留历史线。
- IGNORE：新值是 ASR 错误/抽取噪声/同义改写（与旧值实为同一事物），保持图谱不变，丢弃新值。

# Output Format (Strict JSON)
decisions 的顺序与数量必须与输入 items 完全一致。
{"decisions": [{"decision": "CONFIRM | OVERWRITE | IGNORE", "reason_zh": "string"}]}"""


def transcript_excerpt(chunks: list[dict], source, pad: float = 45.0,
                       cap: int = 700) -> str:
    """Pull the transcript text around a triple's source time window from the
    pipeline state's chunks (list of Chunk.model_dump()). Best-effort: returns
    "" when chunks/time info are unavailable."""
    try:
        lo = float(source.start_time or 0) - pad
        hi = float(source.end_time or 0) + pad
    except (TypeError, AttributeError):
        return ""
    parts = []
    for c in chunks or []:
        src = c.get("source") or {}
        try:
            s, e = float(src.get("start_time") or 0), float(src.get("end_time") or 0)
        except (TypeError, ValueError):
            continue
        if e >= lo and s <= hi:
            parts.append(c.get("annotated_text") or c.get("text") or "")
    return " ".join(" ".join(parts).split())[:cap]


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

    # ── final adjudication (replaces human interrupts, PRD 4.3 修订) ──
    def adjudicate(self, pending: list[InspectionResult],
                   chunks: list[dict]) -> list[tuple[str, Triple | None, str]]:
        """Second-pass contextual ruling on review_required items. Returns one
        (decision, final_triple, reason) per item, aligned with `pending`:
        decision ∈ accept_correction | keep_original | drop. Fail-open to
        keep_original (never lose transcript facts to a flaky judge call)."""
        if not pending:
            return []
        items = []
        for r in pending:
            issue = r.issues[-1] if r.issues else None
            items.append({
                "original_triplet": self._as_triplet(r.triple) if r.triple else None,
                "suggested_triplet": (self._as_triplet(r.suggested_triple)
                                      if r.suggested_triple else None),
                "prior_doubt": getattr(issue, "reason", "") if issue else "",
                "transcript_excerpt": transcript_excerpt(
                    chunks, r.triple.source if r.triple else None),
            })
        triples = [r.triple for r in pending if r.triple is not None]
        user = json.dumps({
            "local_domain_dictionary": self._dictionary(),
            "historical_graph_context": self._graph_context(triples),
            "items": items,
        }, ensure_ascii=False)
        try:
            data = self.llm.complete_json(ADJUDICATE_SYSTEM, user, max_tokens=2048)
            decisions = data.get("decisions", [])
        except LLMError:
            decisions = []
        out: list[tuple[str, Triple | None, str]] = []
        for i, r in enumerate(pending):
            d = decisions[i] if i < len(decisions) else None
            if not d:
                out.append(("keep_original", r.triple, "裁决调用失败，保留原文"))
                continue
            decision = (d.get("decision") or "KEEP_ORIGINAL").upper()
            reason = d.get("reason_zh", "")
            if decision == "DROP":
                out.append(("drop", None, reason))
            elif decision == "ACCEPT_CORRECTION":
                base = r.suggested_triple or r.triple
                final = (self._apply_correction(base, d.get("final_triplet") or {})
                         if base is not None else None)
                if final is None:
                    out.append(("keep_original", r.triple, reason))
                else:
                    out.append(("accept_correction", final, reason))
            else:
                out.append(("keep_original", r.triple, reason))
        return out

    def adjudicate_conflicts(self, pending: list[tuple],
                             chunks: list[dict]) -> list[tuple[str, str]]:
        """Rule on single-valued-relation conflicts (Conflict, new Triple) pairs.
        Returns (decision, reason) per item: confirm | overwrite | ignore.
        Fail-safe to ignore (graph unchanged) on judge failure."""
        if not pending:
            return []
        items = []
        for conflict, triple in pending:
            eid_hits = self.graph.search_nodes(conflict.subject_name)
            history = []
            if eid_hits:
                history = [
                    {"object": h["object_name"], "mentions": h["mentions"]}
                    for h in self.graph.relationship_object_counts(
                        eid_hits[0]["eid"], [conflict.relation], limit=5)
                ]
            items.append({
                "subject": conflict.subject_name,
                "relation": conflict.relation,
                "existing_object_in_graph": conflict.existing_object,
                "new_object_from_this_episode": conflict.new_object,
                "new_source": triple.source.citation(),
                "historical_object_mentions": history,
                "transcript_excerpt": transcript_excerpt(chunks, triple.source),
            })
        user = json.dumps({"items": items}, ensure_ascii=False)
        try:
            data = self.llm.complete_json(
                CONFLICT_ADJUDICATE_SYSTEM, user, max_tokens=1024)
            decisions = data.get("decisions", [])
        except LLMError:
            decisions = []
        out: list[tuple[str, str]] = []
        for i in range(len(pending)):
            d = decisions[i] if i < len(decisions) else None
            decision = ((d or {}).get("decision") or "IGNORE").lower()
            if decision not in ("confirm", "overwrite", "ignore"):
                decision = "ignore"
            out.append((decision, (d or {}).get("reason_zh", "") if d else "裁决调用失败，保持图谱不变"))
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
