"""节目方案 YAML 读写单元测试：纠错回流(add_mapping)是运行时唯一会
自动改写节目方案文件的路径，写坏了会污染 ASR prompt / 术语库，必须有回归防线。
"""
from __future__ import annotations

import pytest
import yaml

from clip.program_profile import (
    _validate_program_id,
    _yaml_quote,
    _yaml_upsert_mapping,
    add_mapping,
)

_MINIMAL_YAML = """\
program_id: test_program
display_name: テスト番組
processing:
  language: ja
  translate_to: zh
  name_corrections:
    青鬼プロダクション: 青二プロダクション
  terminology: {}
"""


class TestYamlQuote:
    def test_plain_string_is_unquoted(self):
        assert _yaml_quote("hello") == "hello"

    def test_string_with_colon_is_quoted(self):
        assert _yaml_quote("a: b") == '"a: b"'

    def test_string_with_hash_is_quoted(self):
        assert _yaml_quote("#tag") == '"#tag"'

    def test_embedded_quote_is_escaped(self):
        assert _yaml_quote('say "hi"') == '"say \\"hi\\""'


class TestValidateProgramId:
    def test_valid_id_passes(self):
        _validate_program_id("minetsuki_ritsu")  # no raise

    def test_uppercase_is_rejected(self):
        with pytest.raises(ValueError):
            _validate_program_id("Minetsuki")

    def test_path_traversal_is_rejected(self):
        with pytest.raises(ValueError):
            _validate_program_id("../../etc/passwd")

    def test_empty_is_rejected(self):
        with pytest.raises(ValueError):
            _validate_program_id("")


class TestYamlUpsertMapping:
    def test_updates_existing_key_in_place(self):
        out = _yaml_upsert_mapping(_MINIMAL_YAML, "name_corrections", "青鬼プロダクション", "青二プロダクション改")
        data = yaml.safe_load(out)
        assert data["processing"]["name_corrections"]["青鬼プロダクション"] == "青二プロダクション改"
        # 其余字段不受影响
        assert data["display_name"] == "テスト番組"

    def test_inserts_new_key_into_existing_block(self):
        out = _yaml_upsert_mapping(_MINIMAL_YAML, "name_corrections", "羊宮妃那", "羊宮妃那")
        data = yaml.safe_load(out)
        nc = data["processing"]["name_corrections"]
        assert nc["羊宮妃那"] == "羊宮妃那"
        assert nc["青鬼プロダクション"] == "青二プロダクション"  # 原有键保留

    def test_upgrades_inline_empty_mapping_to_block(self):
        # `terminology: {}`（信息收集 agent 生成草稿时 yaml.safe_dump 对空 dict 的
        # 序列化写法）必须原地升级为块级映射，而不是在别处新建同名块——否则会
        # 产生重复键，PyYAML 解析时后出现的 `{}` 静默覆盖掉刚写入的纠错。
        out = _yaml_upsert_mapping(_MINIMAL_YAML, "terminology", "こもれび", "小森林")
        assert out.count("terminology:") == 1
        data = yaml.safe_load(out)
        assert data["processing"]["terminology"]["こもれび"] == "小森林"

    def test_creates_block_when_section_absent_entirely(self):
        text = _MINIMAL_YAML.replace("  terminology: {}\n", "")
        out = _yaml_upsert_mapping(text, "terminology", "こもれび", "小森林")
        data = yaml.safe_load(out)
        assert data["processing"]["terminology"]["こもれび"] == "小森林"

    def test_value_with_special_chars_round_trips(self):
        out = _yaml_upsert_mapping(_MINIMAL_YAML, "terminology", "key", "a: b # c")
        data = yaml.safe_load(out)
        assert data["processing"]["terminology"]["key"] == "a: b # c"

    def test_preserves_unrelated_comments(self):
        text = _MINIMAL_YAML.replace(
            "  name_corrections:", "  # 纠错映射\n  name_corrections:"
        )
        out = _yaml_upsert_mapping(text, "name_corrections", "新错误", "新正确")
        assert "# 纠错映射" in out
        data = yaml.safe_load(out)
        assert data["processing"]["name_corrections"]["新错误"] == "新正确"


class TestAddMapping:
    def _write(self, tmp_path, name="test_program"):
        p = tmp_path / f"{name}.yaml"
        p.write_text(_MINIMAL_YAML, encoding="utf-8")
        return p

    def test_writes_and_validates_round_trip(self, tmp_path):
        self._write(tmp_path)
        result = add_mapping("test_program", "name_corrections", "青鬼", "青二", programs_dir=tmp_path)
        assert result == {
            "id": "test_program", "section": "name_corrections", "key": "青鬼", "value": "青二",
        }
        saved = yaml.safe_load((tmp_path / "test_program.yaml").read_text(encoding="utf-8"))
        assert saved["processing"]["name_corrections"]["青鬼"] == "青二"

    def test_missing_program_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            add_mapping("does_not_exist", "name_corrections", "a", "b", programs_dir=tmp_path)

    def test_invalid_section_raises(self, tmp_path):
        self._write(tmp_path)
        with pytest.raises(ValueError):
            add_mapping("test_program", "not_a_real_section", "a", "b", programs_dir=tmp_path)

    def test_empty_key_or_value_raises(self, tmp_path):
        self._write(tmp_path)
        with pytest.raises(ValueError):
            add_mapping("test_program", "terminology", "", "b", programs_dir=tmp_path)
        with pytest.raises(ValueError):
            add_mapping("test_program", "terminology", "a", "  ", programs_dir=tmp_path)
