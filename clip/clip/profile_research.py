"""本地「信息收集 agent」：自然语言描述 → 抓取 ACG/声优资料站 → LLM 合成节目方案草稿。

参考 firecrawl 的「search → scrape → 干净文本 → 结构化抽取」流程，但完全本地、无新依赖、
无外部 API key：
1. search/scrape：聚焦少数 curated 资料站。其中萌娘百科 / 维基 / Fandom 都是 MediaWiki，
   走它们的 `api.php` 取**渲染后 HTML**（比直接爬页面稳、绕反爬），再用标准库 `html.parser`
   清成纯文本；Pixiv 百科等非 wiki 站点用 httpx 直取 HTML 作 best-effort 补充。
2. extract：把多源文本 + 用户描述 + 一个现成方案当结构范例，交给 `LLMClient.complete_json`
   合成一份 ProgramProfile 草稿（字段与 `clip/programs/<id>.yaml` 1:1）。

产出草稿**不落盘**，回前端供人审改后再保存（沿用 program_profile.save_profile）。

这个 agent 在本项目最大的作用：把 VTuber 的曲名/成员/团名/tag/粉丝名/口癖等生僻专名
（正是 ASR 幻听、翻译错写、KG 实体错挂的根源）从权威资料站一次性拉齐，自动生成可直接用的
`terminology` / `name_corrections` / `members` / `summary_style`，省掉每个新节目几小时的手工调研。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

_PROGRAMS_DIR = Path(__file__).resolve().parent / "programs"
_EXEMPLAR_ID = "minetsuki_ritsu"     # 现成方案当结构范例（同团成员还能收敛组合级术语）

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# curated ACG/声优 资料站。两种取数方式：
#   - "api"：MediaWiki api.php（search→parse），稳、可绕反爬。Fandom 走这条。
#   - "page"：直接取渲染后页面 `/wiki/<title>`，按候选名当标题试。萌娘百科禁 parse-api、
#            维基对数据中心 IP 限流，故走 page；命中阈值过滤反爬 stub。住宅 IP 上更稳。
# 该站偏好的查询语种 key：zh/ja/en。
_SOURCES = [
    {"name": "BanG Dream Fandom", "lang": "en", "mode": "api",
     "api": "https://bandori.fandom.com/api.php"},
    {"name": "萌娘百科", "lang": "zh", "mode": "page",
     "page": "https://zh.moegirl.org.cn/{title}"},
    {"name": "日文维基", "lang": "ja", "mode": "page",
     "page": "https://ja.wikipedia.org/wiki/{title}"},
]
_PAGE_CHAR_CAP = 8000        # 每个页面清洗后保留的最大字符数
_MAX_PAGES_PER_SOURCE = 2    # 每站最多抓 成员页 + 组合页
_MIN_PAGE_TEXT = 600         # 渲染页清洗后低于此判为反爬 stub / 空页，跳过
_BOILER = "This site requires JavaScript"


# ───────────────────────── HTML → 纯文本 ─────────────────────────
_SKIP_TAGS = {"script", "style", "sup", "table"}     # 去脚本/样式/引用上标；表格保留文本但不去（见下）
_BLOCK_TAGS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "br", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "sup"):
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "sup") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [ln.strip() for ln in raw.splitlines()]
        out: list[str] = []
        for ln in lines:
            if ln:
                out.append(ln)
            elif out and out[-1] != "":
                out.append("")
        return "\n".join(out).strip()


def strip_html(html: str) -> str:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - 容忍坏标签，尽力而为
        pass
    return p.text()


# ───────────────────────── 抓取 ─────────────────────────
@dataclass
class SourceDoc:
    source: str
    title: str
    url: str
    text: str


def _mediawiki_search_title(client: httpx.Client, api: str, query: str) -> str | None:
    r = client.get(api, params={
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": 1, "format": "json",
    })
    r.raise_for_status()
    hits = r.json().get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def _mediawiki_fetch(client: httpx.Client, api: str, title: str) -> tuple[str, str] | None:
    """返回 (渲染后纯文本, canonical url)。"""
    r = client.get(api, params={
        "action": "parse", "page": title, "prop": "text",
        "redirects": 1, "format": "json",
    })
    r.raise_for_status()
    data = r.json().get("parse", {})
    html = (data.get("text") or {}).get("*", "")
    if not html:
        return None
    base = api.rsplit("/api.php", 1)[0].rsplit("/w", 1)[0]
    url = f"{base}/wiki/{quote((data.get('title') or title).replace(' ', '_'))}"
    return strip_html(html)[:_PAGE_CHAR_CAP], url


def _fetch_page(client: httpx.Client, tmpl: str, title: str) -> tuple[str, str] | None:
    """直接取渲染页（按 title 当 URL）。返回 (纯文本, url)；反爬 stub/空页返回 None。"""
    url = tmpl.format(title=quote(title))
    r = client.get(url)
    if r.status_code != 200:
        return None
    txt = strip_html(r.text)
    txt = "\n".join(ln for ln in txt.splitlines() if _BOILER not in ln).strip()
    if len(txt) < _MIN_PAGE_TEXT:
        return None
    return txt[:_PAGE_CHAR_CAP], url


def collect_sources(
    queries: dict[str, list[str]],
    *,
    extra_urls: list[str] | None = None,
    timeout: float = 15.0,
) -> list[SourceDoc]:
    """对每个 curated 站点按其偏好语种的候选名取数；非 wiki extra_urls 作补充。

    queries: {"zh": [...], "ja": [...], "en": [...]}（成员名 + 组合名，已按重要度排序）。
    任一源失败（超时/反爬/无命中）静默跳过，只要总共拿到 ≥1 篇即可。
    """
    docs: list[SourceDoc] = []
    headers = {"User-Agent": _UA, "Accept-Language": "zh,ja;q=0.8,en;q=0.6"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for src in _SOURCES:
            cands = queries.get(src["lang"]) or queries.get("ja") or queries.get("zh") or []
            got_titles: set[str] = set()
            for q in cands:
                if len(got_titles) >= _MAX_PAGES_PER_SOURCE or not q:
                    break
                try:
                    if src["mode"] == "api":
                        title = _mediawiki_search_title(client, src["api"], q)
                        if not title or title in got_titles:
                            continue
                        got = _mediawiki_fetch(client, src["api"], title)
                    else:  # page
                        title = q
                        if title in got_titles:
                            continue
                        got = _fetch_page(client, src["page"], title)
                    if not got or not got[0].strip():
                        continue
                    got_titles.add(title)
                    docs.append(SourceDoc(src["name"], title, got[1], got[0]))
                except Exception:  # noqa: BLE001 - 单源失败不影响其它
                    continue
        for url in (extra_urls or []):
            try:
                r = client.get(url)
                r.raise_for_status()
                txt = strip_html(r.text)[:_PAGE_CHAR_CAP]
                if txt.strip():
                    docs.append(SourceDoc("补充", url, url, txt))
            except Exception:  # noqa: BLE001
                continue
    return docs


# ───────────────────────── LLM 合成 ─────────────────────────
def _exemplar_yaml() -> str:
    p = _PROGRAMS_DIR / f"{_EXEMPLAR_ID}.yaml"
    return p.read_text(encoding="utf-8") if p.exists() else ""


_QUERY_SYS = """你是 ACG/VTuber/声优 资料检索助手。用户用自然语言描述一个要做「直播二次创作节目方案」的对象（形式/平台/身份/所属组合）。请提取用于在资料站检索的名字，输出 JSON：
{"program_id": "<英文小写下划线 slug，如 sengoku_yuno>",
 "queries": {"zh": ["中文名","组合中文名"], "ja": ["日文名","組合日文名"], "en": ["Romaji Name","Band Name"]},
 "channel_url": "<若描述里给了频道链接则填，否则空字符串>"}
只输出 JSON。名字按「成员在前、组合在后」排序；缺哪个语种就给空数组。"""


@dataclass
class ResearchResult:
    program_id: str
    draft: dict
    yaml_text: str
    sources: list[dict] = field(default_factory=list)
    notes: str = ""


def _build_synthesis_user(description: str, docs: list[SourceDoc], exemplar: str) -> str:
    blocks = [f"## 资料来源 {i+1}：{d.source} · {d.title}\n{d.text}"
              for i, d in enumerate(docs)]
    corpus = "\n\n".join(blocks) if blocks else "（未抓到资料，请仅依据用户描述与常识谨慎填写，拿不准的留空）"
    return (
        f"# 用户描述\n{description}\n\n"
        f"# 抓取到的资料（多源，可能含噪声/繁简混排，请交叉印证后取信）\n{corpus}\n\n"
        f"# 结构范例（一个现成节目方案 YAML，请严格按它的字段层级产出 JSON；"
        f"若本对象与范例同属一个组合，请复用组合级 terminology/members 并保持译名一致）\n"
        f"```yaml\n{exemplar}\n```\n"
    )


_SYNTH_SYS = """你是「节目处理方案 + 归档方案」的资料整理专家，为 VTuber/声优 直播的二次创作生成配置草稿。
依据用户描述与抓取资料，输出**严格 JSON**，字段与范例 YAML 一一对应：
program_id, display_name, performer, band, channel_url, channel_id, accent_color(#RRGGBB),
profile_notes(人设/直播风格 3-6 句中文),
processing: { language, translate_to, translation_prompt_path(留空字符串),
  stt_prompt(只放本对象人名/组合/术语，逗号分隔，防异节目人名幻听),
  summary_prompt_path("_live_summarize.txt"  ← VTuber/直播/歌枠 类一律用这个共享直播总结模板),
  summary{max_summary_chars,target_highlight_count(直播填 0)},
  summary_style(中文，几条侧重，含本对象招牌用语/名场面类型),
  viral_focus(中文一句),
  host:{canonical(规范名，优先权威中文译名), type:"Person", aliases:[昵称/罗马字/日文别名]},
  name_corrections:{ <易被ASR/翻译听错或写错的写法>: <正确写法> ...，含组合名常见误写、成员名 },
  terminology:{ <日文原名/罗马字>: <统一中文或保留原名> ...，**尽量全**：组合名、成员名、应援色、曲名（书名号包裹）、tag、粉丝名、招牌问候 },
  members:[ {name, yomi, role, color, self(本对象为 true)} ... ] }
archiving: { collection_id(=program_id), recordings_root:"../Radio/data/recordings",
  episode_dir_template:"{date}_{label}", kg_program_name(=display_name),
  auto_policy:"confirm", keep_source_video:true, auto_telegram:true }

要求：日文专名保留日文原文；不确定的字段留空字符串/空对象，**不要编造曲名或成员**；
terminology/name_corrections 是本方案最大价值，请尽量从资料里抽全。只输出 JSON。"""


def synthesize_profile(
    description: str,
    docs: list[SourceDoc],
    *,
    program_id: str,
    llm=None,
) -> dict:
    from src.llm.client import LLMClient
    llm = llm or LLMClient()
    user = _build_synthesis_user(description, docs, _exemplar_yaml())
    draft = llm.complete_json(_SYNTH_SYS, user, max_tokens=8192)
    if not isinstance(draft, dict):
        raise ValueError("LLM 未返回方案对象")
    draft.setdefault("program_id", program_id)
    draft["program_id"] = program_id          # 以解析出的 slug 为准
    draft.setdefault("archiving", {}).setdefault("collection_id", program_id)
    return draft


def research_profile(
    description: str,
    *,
    channel_url: str | None = None,
    llm=None,
    extra_urls: list[str] | None = None,
) -> ResearchResult:
    """端到端：描述 → 解析检索名 → 抓取 → 合成草稿。供 API/CLI 调用。"""
    from src.llm.client import LLMClient
    llm = llm or LLMClient()

    plan = llm.complete_json(_QUERY_SYS, description, max_tokens=1024)
    program_id = (plan.get("program_id") or "new_program").strip()
    queries = plan.get("queries") or {}
    urls = list(extra_urls or [])
    cu = channel_url or plan.get("channel_url") or ""
    if cu:
        urls.append(cu)

    docs = collect_sources(queries, extra_urls=urls)
    draft = synthesize_profile(description, docs, program_id=program_id, llm=llm)

    yaml_text = yaml.safe_dump(draft, allow_unicode=True, sort_keys=False)
    sources = [{"source": d.source, "title": d.title, "url": d.url, "chars": len(d.text)}
               for d in docs]
    note = "" if docs else "未抓到任何资料站内容（可能网络受限）；草稿仅据描述生成，请人工补全。"
    return ResearchResult(program_id, draft, yaml_text, sources, note)
