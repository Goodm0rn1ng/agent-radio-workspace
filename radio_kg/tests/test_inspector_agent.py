"""InspectorAgent 纠偏逻辑单元测试（PRD 3.3/5：三层防御的确定性部分）。

三层防御的判定本身是 LLM 驱动的（见 inspector_agent.py 顶部 docstring），这里
只测试不依赖 LLM/Neo4j 的确定性部分：纠偏结果落地（_apply_correction /
_rebuild_entity）与喂给 LLM 的词典/三元组格式化（_dictionary / _as_triplet），
以及 DOMAIN_TERMS 词典本身的数据完整性（别名/易混淆写法不能互相冲突，
否则 _TYPE_BY_NAME 的类型回退会查到错误的词条）。
"""
from __future__ import annotations

import pytest

from src.agents.inspector_agent import DOMAIN_TERMS, InspectorAgent, _TYPE_BY_NAME
from src.mcp_layer.graph_store import entity_id
from src.schema.models import Entity, SourceRef, Triple


def _agent() -> InspectorAgent:
    # llm/graph 只在 __init__ 里保存成属性，_apply_correction 等确定性方法不会触碰它们。
    return InspectorAgent(llm=None, graph=None)


def _triple(subject="青鬼プロダクション", relation="所属事務所", obj="羊宮妃那",
           subject_type="Organization", object_type="Person") -> Triple:
    return Triple(
        subject=Entity(name=subject, type=subject_type),
        relation=relation,
        object=Entity(name=obj, type=object_type),
        source=SourceRef(program="こもれびじかん", episode=1),
    )


class TestRebuildEntity:
    def test_known_domain_name_gets_correct_type(self):
        old = Entity(name="青鬼プロダクション", type="Organization")
        rebuilt = InspectorAgent._rebuild_entity(old, "青二プロダクション")
        assert rebuilt.name == "青二プロダクション"
        assert rebuilt.type == "Organization"

    def test_unknown_name_keeps_old_type(self):
        old = Entity(name="某人", type="Person")
        rebuilt = InspectorAgent._rebuild_entity(old, "某个从未见过的名字")
        assert rebuilt.type == "Person"

    def test_preserves_aliases(self):
        old = Entity(name="青鬼プロダクション", type="Organization", aliases=["青鬼プロ"])
        rebuilt = InspectorAgent._rebuild_entity(old, "青二プロダクション")
        assert rebuilt.aliases == ["青鬼プロ"]


class TestApplyCorrection:
    def test_corrects_object_only(self):
        agent = _agent()
        triple = _triple(subject="羊宮妃那", subject_type="Person",
                         obj="青鬼プロダクション", object_type="Organization")
        corrected = agent._apply_correction(
            triple, {"subject": "羊宮妃那", "predicate": "所属事務所", "object": "青二プロダクション"}
        )
        assert corrected.object.name == "青二プロダクション"
        assert corrected.object.type == "Organization"
        assert corrected.object_id == entity_id("Organization", "青二プロダクション")
        assert corrected.subject.name == "羊宮妃那"  # 未变更的一侧保持原样

    def test_no_op_when_correction_matches_original(self):
        agent = _agent()
        triple = _triple()
        corrected = agent._apply_correction(
            triple, {"subject": triple.subject.name, "predicate": triple.relation,
                     "object": triple.object.name}
        )
        assert corrected is triple  # 无实际变更时不应产生新对象

    def test_corrects_predicate(self):
        agent = _agent()
        triple = _triple()
        corrected = agent._apply_correction(
            triple, {"subject": "", "predicate": "所属事务所（新）", "object": ""}
        )
        assert corrected.relation == "所属事务所（新）"


class TestPromptContextBuilders:
    def test_as_triplet_shape(self):
        triple = _triple()
        d = InspectorAgent._as_triplet(triple)
        assert d == {"subject": "青鬼プロダクション", "predicate": "所属事務所", "object": "羊宮妃那"}

    def test_dictionary_includes_all_domain_terms(self):
        d = InspectorAgent._dictionary()
        assert len(d) == len(DOMAIN_TERMS)
        canonicals = {entry["canonical"] for entry in d}
        assert "青二プロダクション" in canonicals

    def test_dictionary_entry_shape(self):
        d = InspectorAgent._dictionary()
        entry = next(e for e in d if e["canonical"] == "青二プロダクション")
        assert "青鬼プロダクション" in entry["asr_confusable_forms"]
        assert "あおに" in entry["readings"]


class TestDomainTermsIntegrity:
    """词典数据本身的一致性：写错一条会让 _TYPE_BY_NAME 的类型回退查到错词条。"""

    def test_no_duplicate_canonical_names(self):
        names = [t.canonical for t in DOMAIN_TERMS]
        assert len(names) == len(set(names))

    def test_confusable_forms_never_collide_with_a_real_alias(self):
        # 「易混淆写法」代表 ASR 误抓，绝不应该同时也是某个词条的正式别名——
        # 否则 InspectorAgent 会把明确的脏数据当成合法实体放过。
        real_names = set(_TYPE_BY_NAME.keys())
        for term in DOMAIN_TERMS:
            for confusable in term.confusable_aliases:
                assert confusable not in real_names, (
                    f"{confusable!r} 既是易混淆写法又被登记为合法别名"
                )

    def test_every_canonical_and_alias_resolves_its_own_type(self):
        for term in DOMAIN_TERMS:
            assert _TYPE_BY_NAME[term.canonical] == term.type
            for alias in term.aliases:
                assert _TYPE_BY_NAME[alias] == term.type
