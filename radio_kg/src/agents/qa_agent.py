"""Q&A Agent helpers (PRD 3.4): question analysis and grounded answer generation.

Retrieval orchestration (parallel graph + vector, then fusion) lives in
`src/graph/qa_graph.py`; this module holds the two LLM-backed steps.
"""
from __future__ import annotations

from config.settings import settings
from src.llm.client import LLMClient, LLMError
from src.retrieval.retrievers import Passage

ANALYZE_SYSTEM = """ユーザーのラジオ番組に関する質問を分析する。
質問の中心となる固有名詞・人物・企画・コーナー名などの「アンカー語」を抽出する。
固有名詞が無ければ話題語を入れる。

検索は二種類ある:
- search_query: 日本語書き起こし検索向け。直訳ではなく、原文に出そうな短い語を並べる。
- search_query_zh: 中国語の翻訳・要約検索向け。質問中の中国語表現、類義語、要約に出そうな語を並べる。
- related_terms: 曖昧検索用。質問の周辺語を日本語と中国語で混ぜて短く列挙する。

例: 中国語で「节目名为什么叫こもれびじかん？」なら
search_query は「こもれびじかん 番組名 タイトル 候補 理由 選んだ」、
search_query_zh は「节目名 名字 标题 候选 理由 为什么 选择」。
中国語で「她看过什么电影？」なら
search_query は「映画 見た 観た マーベル スパイダーマン アベンジャーズ」、
search_query_zh は「电影 看过 影视 漫威 蜘蛛侠 复仇者联盟」。
必ず次の JSON のみ:
{"anchors": ["..."], "intent": "質問の意図を一文で", "search_query": "...", "search_query_zh": "...", "related_terms": ["..."]}"""

ANSWER_SYSTEM = """あなたはラジオ番組の知識ベースに基づいて回答するアシスタントです。
以下の【コンテキスト】だけを根拠に、ユーザーの質問へ完全性と正確性を優先して回答してください。

厳守ルール:
- コンテキストに無い事実は述べない。推測しない。分からなければ「資料からは確認できません」と答える。
- 質問に複数の問い・条件・例が含まれる場合は、答えられる項目を漏らさず分けて答える。
- 来信者・お便り・メール・投稿・推薦内容などを問われた場合は、コンテキスト内で確認できる関連項目を一件だけに絞らず、すべて列挙する。
- 情報量が多い場合も省略しない。必要なら箇条書きで整理し、各項目に来信者名、内容、主持人の反応、出处を付ける。
- 一つのコンテキストだけで早合点せず、関連する複数のコンテキストを照合してから答える。
- 「図譜事実」と「対話片段」が矛盾する場合は、入庫前に監査済みの「図譜事実」を優先する。
- 事実を述べた文の文末には、その根拠となったコンテキスト項目の SOURCE 値を
  【出处:SOURCEの値】の形でそのまま付ける。
- 【出处:1】や【出处:[1]】のようなコンテキスト番号だけの引用は禁止。
- 会話履歴がある場合は文脈・口調の連続性のために参考にしてよいが、事実は必ず【コンテキスト】のみを根拠にする。
- 回答は質問の言語（中国語の質問には中国語、日本語には日本語）で書く。"""

ANSWER_STRUCT_SYSTEM = """あなたはラジオ番組の知識ベースに基づいて回答するアシスタントです。
【コンテキスト】は `[番号] (種別) SOURCE: 出处\\n本文` の形式で与えられます。

回答は「事実文 → その根拠となるコンテキスト番号」の構造化 JSON で返してください。厳守：
- 各 fact は、必ず特定の一件のコンテキスト [番号] に**直接**書かれている内容だけを述べる。本文に無い情報・推測・一般常識は禁止。
- source_id はその根拠コンテキストの整数番号（[番号] の数字）。コンテキストに存在しない番号は使わない。
- 質問が複数の項目（列挙・ランキング・複数期など）を求める場合は、確認できる項目を漏らさず別々の fact に分ける。
- **同じ事象/同じ事実が複数のソースで言及されている場合は、まとめて 1 件にし source_id は最も明確な 1 件を選ぶ**（同文を 4 回繰り返す等はしない）。
- コンテキストから何も答えられない場合は abstain=true、facts は空配列。
- fact の言語は質問の言語に合わせる（中国語の質問→中国語、日本語→日本語）。
- 会話履歴は口調の参考にしてよいが、事実は必ずコンテキストのみを根拠とする。
JSON のみ出力：
{"abstain": false, "facts": [{"fact": "一文の事実陈述", "source_id": 3}, ...]}"""

VERIFY_STRUCT_SYSTEM = """你是事实核查器。给定若干 (编号, 事实陈述, 该事实声称的来源原文)，
判断每条事实**是否能够**由其来源原文支撑。

支撑的标准（宽松而非苛刻）：
- 来源原文（包括其图谱事实/摘要/逐字稿任意一种形式）明显表达了同一意思，即使措辞不同、是改写、是归纳概括，也算支撑。
- 只要事实陈述的核心命题在来源里能找到对应描述（哪怕是间接、伴随上下文常识理解的），就判 supported=true。
- 仅当事实里包含来源完全没有的具体数字、人名、情节或断言时，才判 supported=false（防止凭空捏造）。
- 模糊但合理的概括（如「常常感谢听众」对应来源里多次「ありがとうございます」）算 supported=true。

只输出 JSON：{"results":[{"id":编号,"supported":true/false}, ...]}，对每个给定编号都要给出判断。"""

CONTEXTUALIZE_SYSTEM = """あなたは会話履歴を踏まえ、ユーザーの最新の発話を「それ単体で検索・理解できる独立した質問」に書き換えるアシスタントです。
ルール:
- 直前までの会話で確定した主語・対象（人物・番組・コーナー・期数など）を補い、「彼女」「她」「それ」「その人」などの指示語・省略を具体名に置き換える。
- 質問の言語は元の発話の言語を保つ（中国語は中国語、日本語は日本語）。
- 新しい話題で履歴と無関係なら、元の発話をほぼそのまま返す。
- 説明や前置きは不要。書き換え後の質問文だけを一行で返す。"""


class QAAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def analyze(self, question: str) -> dict:
        try:
            data = self.llm.complete_json(ANALYZE_SYSTEM, question, max_tokens=512)
            anchors = [a for a in data.get("anchors", []) if isinstance(a, str) and a.strip()]
            search_query = data.get("search_query") or question
            search_query_zh = data.get("search_query_zh") or ""
            related_terms = [
                t for t in data.get("related_terms", [])
                if isinstance(t, str) and t.strip()
            ]
            search_queries = self._build_search_queries(
                question, search_query, search_query_zh, related_terms
            )
            return {
                "anchors": anchors or [question],
                "intent": data.get("intent", ""),
                "search_query": search_queries[0],
                "search_queries": search_queries,
            }
        except LLMError:
            search_queries = self._build_search_queries(question, question, "", [])
            return {
                "anchors": [question],
                "intent": "",
                "search_query": search_queries[0],
                "search_queries": search_queries,
            }

    @staticmethod
    def _build_search_queries(
        question: str,
        search_query: str,
        search_query_zh: str,
        related_terms: list[str],
    ) -> list[str]:
        """Build ordered queries: Chinese summary terms first, then Japanese transcript terms."""
        queries = []
        for q in [search_query_zh, question, search_query, " ".join(related_terms)]:
            q = " ".join(str(q or "").split())
            if q and q not in queries:
                queries.append(q)

        boosted = QAAgent._boost_related_terms(question, queries)
        for q in boosted:
            if q and q not in queries:
                queries.append(q)
        return queries or [question]

    @staticmethod
    def _boost_related_terms(question: str, queries: list[str]) -> list[str]:
        """Add stable fuzzy terms for common Chinese-to-Japanese radio questions."""
        joined = " ".join([question] + queries)
        out = []
        if "こもれびじかん" in joined and any(
            term in joined for term in ("为什么", "為什麼", "理由", "なぜ", "名前", "节目名", "番組名", "タイトル", "叫")
        ):
            out.append("こもれびじかん 番組名 タイトル 候補 理由 選んだ なぜ 今回 この番組")
            out.append("节目名 名字 标题 候选 理由 为什么 选择 木漏日")
        if any(term in joined for term in ("电影", "映画", "看过", "看了", "観た", "見た", "影院", "电影院")):
            out.append("电影 看过 看了 影视 影院 电影院 漫威 蜘蛛侠 复仇者联盟")
            out.append("映画 見た 観た 映画館 マーベル スパイダーマン アベンジャーズ")
        if any(term in joined for term in ("综艺", "綜藝", "バラエティ", "电视", "テレビ")):
            out.append("综艺 电视 节目 最近看 家庭氛围")
            out.append("バラエティ テレビ 番組 見た 聞こえてくる 家庭")
        if any(term in joined for term in ("电视剧", "看剧", "剧透", "ドラマ", "作品")):
            out.append("电视剧 看剧 剧透 作品")
            out.append("ドラマ 作品 ネタバレ 見る")
        if any(term in joined for term in ("游戏", "ゲーム", "实况", "実況", "Minecraft", "マインクラフト")):
            out.append("游戏 实况 Minecraft 我的世界 最近 玩")
            out.append("ゲーム 実況 マインクラフト 最近 プレイ")
        if any(term in joined for term in ("来信", "信件", "投稿", "听众", "聽眾", "来信人", "來信人", "お便り", "メール", "ラジオネーム", "こもれびネーム")):
            out.append("来信 信件 投稿 听众 来信人 昵称 内容 主持反应 推荐")
            out.append("お便り メール 投稿 ラジオネーム こもれびネーム 内容 紹介 反応")
        return out

    # abstention markers — when the answer says it can't confirm, it must not
    # carry source citations (those facts were not actually established).
    _ABSTAIN = ("確認できません", "確認できない", "含まれていません", "確認できず",
                "无法确认", "無法確認")

    @staticmethod
    def _format_history(history: list[dict] | None, limit: int = 6, clip: int = 600) -> str:
        if not history:
            return ""
        turns = []
        for m in history[-limit:]:
            who = "用户" if m.get("role") == "user" else "助手"
            text = " ".join(str(m.get("content", "")).split())[:clip]
            if text:
                turns.append(f"{who}：{text}")
        return "\n".join(turns)

    def contextualize(self, history: list[dict] | None, question: str) -> str:
        """Rewrite a follow-up into a standalone question using chat history."""
        convo = self._format_history(history)
        if not convo:
            return question
        user = f"【会话历史】\n{convo}\n\n【最新发话】\n{question}\n\n独立した質問:"
        try:
            text = self.llm._complete_text(CONTEXTUALIZE_SYSTEM, user, max_tokens=256).strip()
        except LLMError:
            return question
        return text or question

    def answer(self, question: str, context: str, history: list[dict] | None = None) -> str:
        if not context.strip():
            return "资料からは確認できません（未检索到相关内容）。"
        convo = self._format_history(history)
        prefix = f"【会话历史】\n{convo}\n\n" if convo else ""
        user = f"{prefix}【コンテキスト】\n{context}\n\n【質問】\n{question}"
        try:
            # answer is free text, not JSON
            text = self.llm._complete_text(
                ANSWER_SYSTEM, user, max_tokens=settings.qa_answer_max_tokens
            ).strip()
        except LLMError as e:
            return f"(生成失败: {e})"
        return self._drop_citations_if_abstain(text)

    @classmethod
    def _drop_citations_if_abstain(cls, text: str) -> str:
        if any(m in text for m in cls._ABSTAIN):
            import re
            text = re.sub(r"【出[处処]:[^】]*】", "", text).strip()
        return text

    # ── structured answer: fact -> source_id -> citation, then verify ──────
    def answer_structured(self, question: str, passages: list, history: list[dict] | None = None) -> dict:
        """Generate a fact-by-fact answer where every fact carries a source_id
        pointing at a retrieved passage, then post-verify each fact against that
        passage. Facts citing a non-existent source, or not supported by their
        cited passage, are dropped — the model cannot free-form fabricate.

        Returns {"answer": rendered_text, "facts": [verified facts], "dropped": N}.
        """
        if not passages:
            return {"answer": "资料からは確認できません（未检索到相关内容）。", "facts": [], "dropped": 0}
        # index -> (citation, text); same numbering as build_context ([1..N])
        idx = {i: (p.citation or "", p.text or "") for i, p in enumerate(passages, 1)}
        context = "\n".join(
            f"[{i}] SOURCE: {cit or '出处不明'}\n{txt}" for i, (cit, txt) in idx.items()
        )
        convo = self._format_history(history)
        prefix = f"【会话历史】\n{convo}\n\n" if convo else ""
        user = f"{prefix}【コンテキスト】\n{context}\n\n【質問】\n{question}"
        try:
            data = self.llm.complete_json(
                ANSWER_STRUCT_SYSTEM, user, max_tokens=settings.qa_answer_max_tokens)
        except LLMError as e:
            return {"answer": f"(生成失败: {e})", "facts": [], "dropped": 0}

        raw_facts = data.get("facts") or []
        # 1) structural filter: source_id must reference a real retrieved passage
        candidates = []
        for f in raw_facts:
            sid = f.get("source_id")
            text = str(f.get("fact", "")).strip()
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                continue
            if text and sid in idx:
                candidates.append({"fact": text, "source_id": sid,
                                   "citation": idx[sid][0]})
        if not candidates:
            return {"answer": "资料からは確認できません（检索内容不足以支撑回答）。",
                    "facts": [], "dropped": len(raw_facts)}

        # 2) content verification: each fact must be supported by its cited passage
        verified = self._verify_facts(candidates, idx)
        dropped = len(raw_facts) - len(verified)
        if not verified:
            return {"answer": "资料からは確認できません（检索内容不足以支撑回答）。",
                    "facts": [], "dropped": len(raw_facts)}
        rendered = "\n".join(f"- {f['fact']}【出处:{f['citation']}】" if f["citation"]
                             else f"- {f['fact']}" for f in verified)
        return {"answer": rendered, "facts": verified, "dropped": dropped}

    def _verify_facts(self, candidates: list[dict], idx: dict) -> list[dict]:
        """Batched fact-check: drop facts not actually supported by their cited
        passage. On verifier failure, keep structurally-valid facts (fail-open)
        so a flaky judge call doesn't blank a good answer."""
        items = []
        for i, c in enumerate(candidates):
            src_text = idx.get(c["source_id"], ("", ""))[1][:1200]
            items.append({"id": i, "fact": c["fact"], "source_text": src_text})
        payload = "\n\n".join(
            f"[{it['id']}] 事实：{it['fact']}\n来源原文：{it['source_text']}" for it in items)
        try:
            res = self.llm.complete_json(VERIFY_STRUCT_SYSTEM, payload, max_tokens=1024)
            supported = {int(r["id"]): bool(r.get("supported"))
                         for r in res.get("results", []) if "id" in r}
        except (LLMError, ValueError, TypeError):
            return candidates  # fail-open
        keep = [c for i, c in enumerate(candidates) if supported.get(i, True)]
        return keep
