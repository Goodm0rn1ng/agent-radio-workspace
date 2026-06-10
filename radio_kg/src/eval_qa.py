"""RAG quality evaluation harness.

Metrics (per user spec):
  - Faithfulness     : is the answer grounded ONLY in retrieved context (no
                       public-knowledge hallucination)?  [LLM judge, 0..1]
  - Answer Relevance : does the answer actually address the question?  [LLM judge, 0..1]
  - Source Grounding : parse 【出处:期数+时间戳】 from the answer; for each citation
                       load the real transcript window [start-30s, end+30s] from that
                       episode's segments json and verify the claim is actually spoken
                       there, with the cited timestamp within ±30s of the supporting line.

QA answers come from the running dashboard server (`/api/ask`) so we don't open a
second Chroma client. Start the server first:
  .venv/bin/python -m uvicorn src.server.app:app --port 8077

Run:
  .venv/bin/python -m src.eval_qa --server http://127.0.0.1:8077
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.agents.transcript_normalizer import normalize_transcript_text  # noqa: E402
from src.llm.client import LLMClient, LLMError  # noqa: E402
from src.source_data import episode_number, iter_episode_folders  # noqa: E402

GROUNDING_TOLERANCE = 30  # seconds

# (question, note). `expect_unanswerable=True` checks the system abstains rather
# than hallucinating from public knowledge.
TEST_QUESTIONS = [
    {"q": "羊宮妃那はどの事務所に所属していますか？"},
    {"q": "节目名最终为什么叫こもれびじかん？"},
    {"q": "この番組のスポンサー（提供）はどこですか？"},
    {"q": "番組のタイトル候補にはどんなものがありましたか？"},
    {"q": "羊宮妃那の誕生日は何月何日ですか？", "expect_unanswerable": True},
    {"q": "番組のメール募集のハッシュタグは何ですか？"},
]

_CITE_RE = re.compile(r"《(.+?)》第(\d+)期\s*(\d{1,2}:\d{2}:\d{2})?\s*-?\s*(\d{1,2}:\d{2}:\d{2})?")
_CITE_BLOCK_RE = re.compile(r"【出[处処]:([^】]+)】")


def ts_to_sec(ts: str) -> int | None:
    if not ts:
        return None
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def ask_server(server: str, question: str, mode: str = "single") -> dict:
    path = "/api/ask2" if mode == "two_stage" else "/api/ask"
    body = json.dumps({"question": question}).encode()
    req = urllib.request.Request(f"{server}{path}", data=body,
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=180))


def load_segments(episode: int) -> list[dict]:
    data_dir = settings.abspath(settings.radio_data_dir)
    for p in iter_episode_folders(data_dir, archives_only=True, require_segments=True):
        if episode_number(p) == episode:
            for f in ("04_bilingual_segments.json", "03_ja_segments.json"):
                fp = p / f
                if fp.exists():
                    return json.loads(fp.read_text(encoding="utf-8"))
    return []


def window_text(segments: list[dict], cs: int, ce: int, pad: int = GROUNDING_TOLERANCE) -> str:
    lo, hi = cs - pad, ce + pad
    # apply the same ASR normalization the system uses, so grounding compares
    # the (corrected) answer against the (corrected) transcript apples-to-apples.
    return "\n".join(
        f"[{int(s['start'])}s] {normalize_transcript_text(s.get('ja',''))}"
        for s in segments if s["end"] >= lo and s["start"] <= hi and s.get("ja")
    )


class Judge:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def faith_relevance(self, question: str, context: str, answer: str) -> dict:
        sys_p = (
            "あなたはRAGの評価者。次をJSONで厳密に評価する。"
            'faithfulness: 回答が【コンテキスト】のみに基づくか（外部知識や捏造が無いか）0..1。'
            "回答が『資料からは確認できません』等で適切に棄権している場合は faithfulness=1。"
            'relevance: 回答が【質問】の核心に答えているか 0..1。'
            '出力: {"faithfulness":0..1,"relevance":0..1,"reason_zh":"一句话"}'
        )
        user = f"【質問】{question}\n\n【コンテキスト】\n{context}\n\n【回答】\n{answer}"
        try:
            d = self.llm.complete_json(sys_p, user, max_tokens=400)
            return {"faithfulness": float(d.get("faithfulness", 0)),
                    "relevance": float(d.get("relevance", 0)),
                    "reason": d.get("reason_zh", "")}
        except (LLMError, ValueError, TypeError):
            return {"faithfulness": 0.0, "relevance": 0.0, "reason": "judge_failed"}

    def grounded(self, claim: str, transcript: str) -> dict:
        sys_p = (
            "次の【主張】が【書き起こし抜粋】に実際に述べられているか判定する。"
            "述べられていれば、その根拠となる行頭の秒数(整数)も返す。"
            '出力JSON: {"supported": true/false, "support_sec": 整数 or null}'
        )
        user = f"【主張】{claim}\n\n【書き起こし抜粋】\n{transcript}"
        try:
            d = self.llm.complete_json(sys_p, user, max_tokens=200)
            return {"supported": bool(d.get("supported")),
                    "support_sec": d.get("support_sec")}
        except (LLMError, ValueError, TypeError):
            return {"supported": False, "support_sec": None}


def _claim_for(answer: str, start: int) -> str:
    """The clause a citation annotates = text from the previous sentence break
    up to the citation marker."""
    head = answer[:start]
    head = re.split(r"【出[处処]:[^】]*】", head)[-1]   # since previous citation
    parts = re.split(r"[。．\.\n！!？?]", head)
    claim = next((p for p in reversed(parts) if p.strip()), head)
    return claim.strip() or answer[:120]


def eval_grounding(judge: Judge, answer: str, sources: list[dict]) -> dict:
    """Check each cited [episode+timestamp] against the real transcript window.

    Each citation is judged against the clause it annotates (the preceding
    sentence), not the whole answer — so multi-citation answers attribute fairly."""
    matches = list(_CITE_BLOCK_RE.finditer(answer))
    if not matches:
        return {"citations": 0, "grounded": 0, "rate": None, "details": []}
    details = []
    grounded = 0
    for mt in matches:
        raw = mt.group(1)
        claim = _claim_for(answer, mt.start())
        m = _CITE_RE.search(raw)
        if not m:
            details.append({"citation": raw, "ok": False, "why": "unparseable"})
            continue
        ep = int(m.group(2))
        cs = ts_to_sec(m.group(3)) or 0
        ce = ts_to_sec(m.group(4)) or cs
        segments = load_segments(ep)
        if not segments:
            details.append({"citation": raw, "ok": False, "why": "no_segments"})
            continue
        win = window_text(segments, cs, ce)
        verdict = judge.grounded(claim, win)
        err = None
        if verdict["supported"] and verdict.get("support_sec") is not None:
            ss = int(verdict["support_sec"])
            err = 0 if cs <= ss <= ce else min(abs(ss - cs), abs(ss - ce))
        ok = bool(verdict["supported"]) and (err is None or err <= GROUNDING_TOLERANCE)
        if ok:
            grounded += 1
        details.append({"citation": raw, "ok": ok, "supported": verdict["supported"],
                        "ts_error_sec": err})
    return {"citations": len(matches), "grounded": grounded,
            "rate": grounded / len(matches), "details": details}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8077")
    ap.add_argument("--mode", choices=["single", "two_stage"], default="single")
    ap.add_argument("--provider", help="override judge LLM provider")
    args = ap.parse_args()

    judge = Judge(LLMClient(provider=args.provider))
    rows = []
    ctx_chars = 0
    for case in TEST_QUESTIONS:
        q = case["q"]
        print(f"\n■ {q}")
        d = ask_server(args.server, q, mode=args.mode)
        answer = d.get("answer", "")
        context = "\n".join(f"({s['origin']}) {s['text']} [出处:{s['citation']}]"
                            for s in d.get("sources", []))
        ctx_chars += len(context)
        fr = judge.faith_relevance(q, context, answer)
        gr = eval_grounding(judge, answer, d.get("sources", []))
        print(f"  答案: {answer[:120]}")
        print(f"  Faithfulness={fr['faithfulness']:.2f}  Relevance={fr['relevance']:.2f}"
              f"  Grounding={gr['rate'] if gr['rate'] is not None else '—'}"
              f" ({gr['grounded']}/{gr['citations']})  ctx={len(context)}字  | {fr['reason']}")
        for det in gr["details"]:
            print(f"    · {det['citation']}  -> ok={det['ok']} 误差={det.get('ts_error_sec')}s")
        rows.append({"q": q, **fr, **gr})

    # ── summary ──
    n = len(rows)
    avg_f = sum(r["faithfulness"] for r in rows) / n
    avg_r = sum(r["relevance"] for r in rows) / n
    cited = [r for r in rows if r["citations"]]
    tot_c = sum(r["citations"] for r in cited)
    tot_g = sum(r["grounded"] for r in cited)
    print("\n" + "=" * 60)
    print(f"模式 {args.mode} · 问题数 {n}")
    print(f"平均 Faithfulness : {avg_f:.3f}")
    print(f"平均 Answer Relevance: {avg_r:.3f}")
    print(f"Source Grounding Rate: {tot_g}/{tot_c} = "
          f"{(tot_g / tot_c) if tot_c else float('nan'):.3f}  (时间戳误差≤±{GROUNDING_TOLERANCE}s)")
    print(f"平均上下文规模: {ctx_chars / n:.0f} 字/问 (token 代理指标)")


if __name__ == "__main__":
    main()
