# -*- coding: utf-8 -*-
"""화면 서버 — rev07 시안을 실제로 돌아가게 붙인 것.

    python web/server.py          → http://127.0.0.1:8700

왜 표준 라이브러리만 쓰나
    폐쇄망에서 `pip install` 이 막힐 수 있다. FastAPI 를 안 쓰고
    `http.server` 로 짰다. 설치할 것이 없다.

★로직이 여기 없다
    브리핑을 만드는 것은 전부 노트북(`sensing_pipeline.py`)의 ⑨ 절에 있다.
    이 파일은 **그 함수를 불러 화면에 실어 나르기만** 한다.

        AXES · CAT_FLOOR · SUMMARY_SYSTEM · build_axis · build_brief
        → 전부 노트북에 있다. 고치려면 노트북을 고친다.

    그래서 노트북에서 셀을 돌려 확인한 것이 곧 화면에 나오는 것이다.
    불러오는 동안 노트북이 한 번 돈다 — 새 문서가 있으면 자동으로 적재된다.
"""
from __future__ import annotations

import contextlib
import io
import json
import runpy
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))      # 노트북이 sensing_tools 를 찾을 수 있게

print("파이프라인 불러오는 중…", end=" ", flush=True)
_t0 = time.time()
with contextlib.redirect_stdout(io.StringIO()):
    NB = runpy.run_path(str(ROOT / "sensing_pipeline.py"))
print("%.1f초" % (time.time() - _t0))

# ── 노트북에서 가져다 쓰는 것 (여기서 다시 만들지 않는다) ──────────
build_brief = NB["build_brief"]
search = NB["search"]
evidence_block = NB["evidence_block"]
verify = NB["verify"]
run_once = NB["run_once"]
CHIP, TIER = NB["CHIP"], NB["TIER"]
CFG, SYSTEM, tools = NB["CFG"], NB["SYSTEM"], NB["tools"]


def ask(question, access="전사", k=6):
    """질문 하나 → 답변 + 근거. ⑥⑦ 을 그대로 쓴다."""
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
                    int(q.get("days", ["14"])[0]),
                    compare=q.get("compare", ["0"])[0] == "1"), ensure_ascii=False))
            if u.path == "/api/ask":
                return self._send(200, json.dumps(ask(
                    q.get("q", [""])[0], q.get("access", ["전사"])[0]),
                    ensure_ascii=False))
            if u.path == "/api/sync":
                return self._send(200, json.dumps(run_once(), ensure_ascii=False))
            self._send(404, json.dumps({"error": "not found"}))
        except Exception as e:               # ★화면이 죽지 않게 사유를 돌려준다
            self._send(500, json.dumps({"error": str(e)[:200]}, ensure_ascii=False))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = 8700
    print("\n  http://127.0.0.1:%d  — Ctrl+C 로 종료\n" % port)
    print("  모델 : 임베딩 %s · 생성 %s" % (
        "붙음" if CFG.get("EMBED_MODEL") else "없음(가짜 벡터)",
        "붙음" if CFG.get("LLM_MODEL") else "없음(요약은 근거 첫 줄)"))
    print("  ★축·하한·지시문을 고치려면 sensing_pipeline.py 의 ⑨ 절을 고친다.\n")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
