# -*- coding: utf-8 -*-
"""화면 서버 — rev07 시안을 실제로 돌아가게 붙인 것.

    python web/server.py          → http://127.0.0.1:8700

왜 표준 라이브러리만 쓰나
    폐쇄망에서 `pip install` 이 막힐 수 있다. FastAPI 를 안 쓰고
    `http.server` 로 짰다. 설치할 것이 없다.

★로직을 다시 쓰지 않는다
    노트북(`sensing_pipeline.py`)을 **그대로 불러** 그 함수를 쓴다.
    화면과 노트북이 **같은 코드**를 보므로 규칙을 고치면 화면에도 바로 반영된다.
    (불러오는 동안 노트북이 한 번 돈다 — 자료가 최신이 된다)

화면의 행이 유형과 맞아떨어진다
    시장  news · official · filing      밖에서 무슨 일이
    전망  report · research             시장은 어떻게 보나
    기준  external_doc                  정해진 규격·통계
    사내  internal_doc                  우리는 어떤 상태인가
    논의  mail                          최근 어떤 이야기가
"""
from __future__ import annotations

import contextlib
import io
import json
import runpy
import sys
import time
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))      # 노트북이 tools.py 를 찾을 수 있게

# ── 노트북을 그대로 불러온다 (출력은 삼킨다) ──────────────────────
print("파이프라인 불러오는 중…", end=" ", flush=True)
_t0 = time.time()
with contextlib.redirect_stdout(io.StringIO()):
    NB = runpy.run_path(str(ROOT / "sensing_pipeline.py"))
print("%.1f초" % (time.time() - _t0))

search = NB["search"]
run_once = NB["run_once"]
evidence_block = NB["evidence_block"]
verify = NB["verify"]
conn = NB["conn"]
CFG = NB["CFG"]
SYSTEM = NB["SYSTEM"]
tools = NB["tools"]

# ── 주간 기록 — ② 지난번과 무엇이 달라졌나 ────────────────────────
#   ★마켓 센싱의 핵심은 "지금 무엇이 있나" 가 아니라 "무엇이 달라졌나" 다.
#     축 요약을 매번 남겨 두고, 다음 번에 그것을 함께 넘겨 비교하게 한다.
conn.executescript("""
CREATE TABLE IF NOT EXISTS briefs(
  id TEXT PRIMARY KEY, made TEXT, axis INTEGER, name TEXT,
  access TEXT, lead TEXT, cats TEXT);
CREATE INDEX IF NOT EXISTS briefs_axis ON briefs(axis, made);
""")
conn.commit()


def last_record(axis_no, access):
    """이 축의 **직전 기록**. 없으면 None."""
    r = conn.execute("SELECT made, lead, cats FROM briefs"
                     " WHERE axis=? AND access=? ORDER BY made DESC LIMIT 1",
                     (axis_no, access)).fetchone()
    if not r:
        return None
    cats = json.loads(r["cats"] or "{}")
    줄 = ["(%s 기록)" % r["made"][:10], "축: %s" % r["lead"]]
    줄 += ["%s: %s" % (k, v) for k, v in cats.items()]
    return "\n".join(줄)


def save_record(ax, access):
    """이번 결과를 남긴다. 같은 날 같은 축은 덮어쓴다."""
    made = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute(
        "INSERT OR REPLACE INTO briefs(id,made,axis,name,access,lead,cats)"
        " VALUES(?,?,?,?,?,?,?)",
        ("%s|%d|%s" % (made[:10], ax["no"], access), made, ax["no"], ax["name"],
         access, ax["lead"],
         json.dumps({r["label"]: r["text"] for r in ax["rows"]}, ensure_ascii=False)))
    conn.commit()


# ── 주제축 — ★사내에서 여기만 고치면 화면이 바뀐다 ────────────────
#   name  화면에 뜨는 이름
#   q     이 축을 대표하는 질문. 검색은 이 문장으로 돈다
AXES = [
    {"no": 1, "name": "HBM · 고대역폭 메모리", "q": "HBM4 수율과 양산 일정"},
    {"no": 2, "name": "증설 · 투자 판단", "q": "M16 증설 판단 근거와 전환 투자"},
    {"no": 3, "name": "일반 DRAM · 수급", "q": "DRAM 공급 증가율과 가격 전망"},
    {"no": 4, "name": "공정 · 표준", "q": "하이브리드 본딩 접합 기준"},
    {"no": 5, "name": "장비 · 설비투자", "q": "반도체 장비 출하와 설비투자 동향"},
]

# 화면의 행 ← 유형
ROW_OF = {"news": "시장", "official": "시장", "filing": "시장",
          "report": "전망", "research": "전망",
          "external_doc": "기준", "internal_doc": "사내", "mail": "논의"}
ROW_ORDER = ["시장", "전망", "기준", "사내", "논의"]

# ★비었을 때 뭐라고 말할지 — 공백 자체가 정보다
NONE_TEXT = {
    "시장": "이 축에 걸린 외부 뉴스·공시가 없습니다.",
    "전망": "이 축을 다룬 증권사·기관 자료가 없습니다.",
    "기준": "관련 표준·협회 자료가 없습니다.",
    "사내": "★이 축에 대한 사내 문서가 없습니다 — 대응이 없거나 아직 안 올라온 것입니다.",
    "논의": "관련 메일·회의록이 없습니다.",
}
CHIP = {"news": "뉴스", "official": "공식", "filing": "공시", "report": "리포트",
        "research": "기관", "external_doc": "표준", "internal_doc": "사내", "mail": "메일"}
TIER = {"internal_doc": "t1", "filing": "t1", "official": "t1", "mail": "t4"}


# ── 얼마나 걸러낼까 ──────────────────────────────────────────────
#   ★빈칸을 남기지 않으려고 억지로 채우면 안 된다.
#     그 위에 요약을 만들면 **없는 이야기를 지어낸다.**
#     "이 축에 우리 문서는 없음" 이 정확한 정보다.
CAT_FLOOR = 0.55      # 그 축 1위 대비 이 비율 미만인 근거는 뺀다
CAT_MAX = 4           # 한 카테고리에 최대 몇 건까지 묶을까

SUMMARY_SYSTEM = """자료를 카테고리별로 한 줄씩 요약하고, 축 전체를 판단합니다.

규칙
1. 아래 [근거] 에 적힌 내용만 씁니다. 없는 사실·수치를 지어내지 않습니다.
2. 카테고리마다 **한 문장**, 40~90자. 여러 건이면 **묶어서** 한 문장으로.
   서로 어긋나는 내용이면 "…인 반면 …" 처럼 **양쪽을 다 적습니다.**
3. `축:` 은 요약이 아니라 **판단**입니다. 다음 순서로 봅니다.
   ⑴ 사내 근거와 외부 근거가 **어긋나는 곳**이 있으면 그것을 먼저 적습니다.
      예) "시장은 우리 부진을 강점으로 보나, 사내 문서는 원인을 아직 못 잡았다"
   ⑵ 어긋남이 없으면 **무엇이 관건인지** 한 문장으로 적습니다.
   60~110자. 임원이 읽고 무엇을 결정해야 할지 알 수 있어야 합니다.
4. `변화:` 는 [지난 기록] 이 있을 때만 씁니다. 지난번과 **달라진 점**만 적습니다.
   논조가 뒤집혔으면 그것을 먼저 적습니다. 달라진 것이 없으면 "없음" 이라고만 합니다.
5. 정확히 이 형식으로만 답합니다. 다른 말을 붙이지 않습니다.

시장: …
전망: …
기준: …
사내: …
논의: …
축: …
변화: …

근거가 없는 카테고리는 그 줄을 통째로 뺍니다.
★특히 `사내:` 가 없으면 `축:` 에 "사내 대응 문서가 없다" 는 점을 반드시 적습니다."""


def _summarize(groups, cfg, last=None):
    """카테고리별 조각 묶음 → {카테고리: 한 줄} + {"축": 한 줄}

    ★축 하나당 모델을 **한 번만** 부른다.
      카테고리마다 따로 부르면 축 5개 × 카테고리 5개 = 25번이 된다.
      한 번에 묶어 물으면 5번으로 끝난다.
    """
    if not groups:
        return {}
    블록 = []
    for label in ROW_ORDER:
        hs = groups.get(label) or []
        if not hs:
            continue
        줄 = ["%s:" % label]
        for i, h in enumerate(hs, 1):
            줄.append("  [%d] %s · %s — %s" % (
                i, h["org"] or "-", (h["date"] or "")[:10],
                (h["body"] or "").replace("\n", " ")[:260]))
        블록.append("\n".join(줄))

    프롬프트 = "[근거]\n" + "\n\n".join(블록)
    if last:                                    # 지난주와 비교하게 한다
        프롬프트 += "\n\n[지난 기록]\n" + last
    text = tools.generate(SUMMARY_SYSTEM, 프롬프트, cfg)
    if not text:
        # 모델이 없으면 — 카테고리 첫 조각을 줄여서 쓴다(요약이 아님을 화면에 표시)
        out = {}
        for label, hs in groups.items():
            b = (hs[0]["body"] or "").replace("\n", " ").strip()
            out[label] = b[:90] + ("…" if len(b) > 90 else "")
        top = groups.get("사내") or groups.get("전망") or next(iter(groups.values()))
        b = (top[0]["body"] or "").replace("\n", " ").strip()
        out["축"] = b[:90] + ("…" if len(b) > 90 else "")
        out["_raw"] = True                      # ★요약이 아니라 원문 조각이다
        return out

    out = {}
    for 줄 in text.splitlines():
        if ":" not in 줄:
            continue
        k, v = 줄.split(":", 1)
        k, v = k.strip(), v.strip()
        if k in ROW_ORDER or k in ("축", "변화"):
            out[k] = v
    return out


def build_axis(ax, access, days, used=None, last=None):
    """축 하나 → 화면 한 장

    ★점수 하한을 넘긴 근거만 쓴다. 빈칸을 억지로 채우지 않는다.
    ★해석(뉴스·리포트·기관)은 이미 다른 축이 쓴 것을 다시 쓰지 않는다.
      사실(사내문서·공시·기업공식)은 여러 축에서 근거가 되므로 중복을 허용한다.
    """
    hits = search(ax["q"], k=16, access=access)
    if not hits:
        return {"no": ax["no"], "name": ax["name"], "q": ax["q"], "rows": [],
                "lead": "이번 기간에 이 축에 걸린 근거가 없습니다.", "change": "",
                "raw": False, "count": 0, "new": 0, "cont": 0, "empty": True, "move": 0}

    바닥 = hits[0]["score"] * CAT_FLOOR
    used = used if used is not None else set()

    groups, low, dropped, dup = {}, {}, 0, 0
    for h in hits:
        label0 = ROW_OF.get(h["mtype"], "시장")
        if h["score"] < 바닥:
            # ★요약에는 넣지 않는다. 다만 **버리지도 않는다.**
            #   억지로 남긴 근거는 요약에 들어가고, 요약은 임원이 그대로 읽는다.
            #   그렇다고 조용히 사라지면 "왜 없지" 를 알 수 없다.
            #   → 화면에 '관련 낮음' 으로 접어서 남긴다.
            dropped += 1
            if len(low.get(label0, [])) < 3:
                low.setdefault(label0, []).append(h)
            continue
        # ★해석은 한 축에서만. 사실은 여러 축에서 근거가 된다.
        사실 = h["mtype"] in ("internal_doc", "filing", "official")
        if not 사실 and h["id"] in used:
            dup += 1
            continue
        label = ROW_OF.get(h["mtype"], "시장")
        if len(groups.get(label, [])) >= CAT_MAX:
            continue
        groups.setdefault(label, []).append(h)
        if not 사실:
            used.add(h["id"])

    if not groups:
        return {"no": ax["no"], "name": ax["name"], "q": ax["q"], "rows": [],
                "lead": "이번 기간에 이 축에서 볼 만한 근거가 없습니다.",
                "change": "", "raw": False, "count": 0, "new": 0, "cont": 0,
                "empty": True, "dropped": dropped, "move": 0}

    summary = _summarize(groups, CFG, last)
    raw = summary.pop("_raw", False)

    def _srcs(hs):
        return [{"chip": CHIP.get(h["mtype"], h["mtype"]),
                 "tier": TIER.get(h["mtype"], ""),
                 "org": h["org"] or "-", "date": (h["date"] or "")[5:],
                 "score": h["score"],
                 "body": (h["body"] or "").replace("\n", " ")[:200]} for h in hs]

    # ★카테고리를 **다섯 줄 모두** 만든다. 없으면 '없음' 이라고 말한다.
    #   조용히 빠지면 "이 축에 우리 문서가 없다" 는 사실이 가려진다.
    #   그 공백 자체가 임원이 봐야 할 정보다.
    rows = []
    for label in ROW_ORDER:
        hs = groups.get(label)
        lo = low.get(label) or []
        if hs:
            rows.append({"label": label, "state": "ok",
                         "text": summary.get(label) or (hs[0]["body"] or "")[:90],
                         "n": len(hs), "raw": raw, "srcs": _srcs(hs),
                         "low": _srcs(lo)})
        elif lo:
            rows.append({"label": label, "state": "low",
                         "text": "요약할 만큼 관련 있는 근거가 없습니다 (관련 낮음 %d건, 최고 %.2f)"
                                 % (len(lo), lo[0]["score"]),
                         "n": 0, "raw": False, "srcs": [], "low": _srcs(lo)})
        else:
            rows.append({"label": label, "state": "none",
                         "text": NONE_TEXT.get(label, "해당 자료가 없습니다."),
                         "n": 0, "raw": False, "srcs": [], "low": []})

    빈것 = [r["label"] for r in rows if r["state"] != "ok"]
    쓴것 = sum(len(v) for v in groups.values())
    fresh = sum(1 for v in groups.values() for h in v if _is_fresh(h["date"], days))
    change = (summary.get("변화") or "").strip()
    if change in ("없음", "-", ""):
        change = ""

    # ★축을 어떻게 정렬할까 — 변화가 큰 축이 위로 와야 한다.
    #   변화 문장이 있으면 크게, 새 근거가 많을수록 크게, 근거가 강할수록 크게.
    move = (100 if change else 0) + fresh * 10 + round(hits[0]["score"] * 10)

    return {"no": ax["no"], "name": ax["name"], "q": ax["q"],
            "lead": summary.get("축") or "판단할 근거가 부족합니다.",
            "change": change, "raw": raw, "rows": rows,
            "count": 쓴것, "new": fresh, "cont": 쓴것 - fresh,
            "dropped": dropped, "dup": dup, "gaps": 빈것,
            "empty": False, "move": move}


def _is_fresh(d, days):
    if not d:
        return False
    try:
        y, m, dd = map(int, str(d)[:10].split("-"))
        return (date.today() - date(y, m, dd)).days <= days
    except Exception:
        return False


def build_brief(access="전사", days=14, save=True):
    t0 = time.time()
    used = set()                                # ★해석 근거를 축끼리 나눠 갖게
    axes = []
    for a in AXES:
        ax = build_axis(a, access, days, used=used, last=last_record(a["no"], access))
        axes.append(ax)
        if save and not ax["empty"]:
            save_record(ax, access)

    # ★변화가 큰 축이 위로. 임원이 스크롤하지 않게 한다.
    axes.sort(key=lambda x: -x["move"])
    for i, ax in enumerate(axes, 1):
        ax["rank"] = i

    tot = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    inside = conn.execute("SELECT COUNT(*) FROM docs WHERE mtype IN"
                          " ('internal_doc','mail')").fetchone()[0]
    new_docs = sum(1 for r in conn.execute("SELECT date FROM docs")
                   if _is_fresh(r["date"], days))
    model = conn.execute("SELECT model FROM chunks WHERE vec IS NOT NULL"
                         " LIMIT 1").fetchone()
    회차 = conn.execute("SELECT COUNT(DISTINCT substr(made,1,10)) FROM briefs"
                      " WHERE access=?", (access,)).fetchone()[0]
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "next": (date.today() + timedelta(days=7)).isoformat(),
        "period": "%s ~ %s" % ((date.today() - timedelta(days=days)).isoformat(),
                               date.today().isoformat()),
        "access": access, "axes": axes,
        "changed": [a["name"] for a in axes if a["change"]],
        "no_inside": [a["name"] for a in axes
                      if not a["empty"] and "사내" in (a.get("gaps") or [])],
        "empty": [a["name"] for a in axes if a["empty"]],
        "runs": 회차,
        "kpi": {"new": new_docs, "cont": tot - new_docs,
                "axes": len([a for a in axes if not a["empty"]]),
                "docs": tot, "inside": inside, "outside": tot - inside},
        "model": (model["model"] if model else "-"),
        "live": bool(CFG.get("EMBED_BASE_URL") and CFG.get("EMBED_MODEL")),
        "llm": bool(CFG.get("LLM_BASE_URL") and CFG.get("LLM_MODEL")),
        "ms": int((time.time() - t0) * 1000),
    }


def ask(question, access="전사", k=6):
    hits = search(question, k=k, access=access)
    prompt = "[근거]\n%s\n\n[질문]\n%s" % (evidence_block(hits), question)
    answer = tools.generate(SYSTEM, prompt, CFG)
    return {"question": question, "answer": answer,
            "checks": verify(answer, hits) if answer else [],
            "prompt_chars": len(prompt),
            "hits": [{"score": h["score"], "mtype": h["mtype"],
                      "chip": CHIP.get(h["mtype"], h["mtype"]),
                      "tier": TIER.get(h["mtype"], ""),
                      "org": h["org"], "date": (h["date"] or "")[5:],
                      "section": h["section"],
                      "body": (h["body"] or "").replace("\n", " ")[:220]}
                     for h in hits]}


# ── 서버 ─────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "index.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if u.path == "/api/brief":
                return self._send(200, json.dumps(build_brief(
                    q.get("access", ["전사"])[0],
                    int(q.get("days", ["14"])[0])), ensure_ascii=False))
            if u.path == "/api/ask":
                return self._send(200, json.dumps(ask(
                    q.get("q", [""])[0], q.get("access", ["전사"])[0]),
                    ensure_ascii=False))
            if u.path == "/api/sync":
                return self._send(200, json.dumps(run_once(), ensure_ascii=False))
            self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:                  # ★화면이 죽지 않게 사유를 돌려준다
            self._send(500, json.dumps({"error": str(e)[:200]}, ensure_ascii=False))

    def log_message(self, *a):                  # 접속 로그를 찍지 않는다
        pass


if __name__ == "__main__":
    port = 8700
    print("\n  http://127.0.0.1:%d  — Ctrl+C 로 종료\n" % port)
    print("  모델 : 임베딩 %s · 생성 %s" % (
        "붙음" if CFG.get("EMBED_MODEL") else "없음(가짜 벡터)",
        "붙음" if CFG.get("LLM_MODEL") else "없음(요약은 근거 첫 줄)"))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
