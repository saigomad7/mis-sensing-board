# -*- coding: utf-8 -*-
"""tools — 노트북에서 감춰도 되는 배관만 모았다.

여기 있는 것은 **한 번 붙이면 안 건드리는 것**이다.
    · `.env` 읽기
    · 사내 모델 호출 (임베딩 · 생성)
    · DB 열기 · 표 만들기
    · Milvus 연결
    · 벡터 ↔ 이진 변환
    · 표로 보기 (Spyder 변수 탐색기용)

노트북(`pipeline_step_by_step.py`)에는 **사내 문서에 맞춰 손볼 로직만** 남겼다 —
정규화 규칙 · 조각 규칙 · 점수 계산. 그게 눈에 보여야 고칠 수 있다.

사내에서 바꿀 곳은 `.env` 값 세 개뿐이다. 이 파일도 안 건드린다.
"""
from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import pandas as _pd
    HAS_PANDAS = True
except Exception:
    _pd, HAS_PANDAS = None, False


# ── 설정 ─────────────────────────────────────────────────────────
def load_config(folder: Path, name: str = ".env") -> Dict[str, str]:
    """.env 를 읽는다. ★키를 코드에 절대 넣지 않는다."""
    cfg: Dict[str, str] = {}
    p = Path(folder) / name
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def check_connections(cfg: Dict[str, str]) -> List[Tuple[str, bool, str]]:
    """무엇이 붙었고 무엇이 안 붙었는지 → [(이름, 붙음, 설명)]"""
    has_embed = bool(cfg.get("EMBED_BASE_URL") and cfg.get("EMBED_MODEL"))
    has_llm = bool(cfg.get("LLM_BASE_URL") and cfg.get("LLM_MODEL"))
    has_milvus = cfg.get("VECTOR_KIND") == "milvus" and bool(cfg.get("MILVUS_URI"))
    return [
        ("① 임베딩 모델", has_embed,
         cfg.get("EMBED_MODEL", "") if has_embed else "없음 → 가짜 벡터로 흐름만 돈다"),
        ("② 생성 모델", has_llm,
         cfg.get("LLM_MODEL", "") if has_llm else "없음 → ⑦ 은 지문만 보여 준다"),
        ("③ 벡터 저장소", True,
         "Milvus · " + cfg.get("MILVUS_COLLECTION", "mis_chunk") if has_milvus else "sqlite"),
    ]


# ── DB ───────────────────────────────────────────────────────────
#  raw    ①② 수집·보관   손대지 않은 그대로
#  docs   ③ 정규화       유형이 달라도 칸이 같아진다
#  chunks ④⑤ 조각·벡터   검색의 단위
DDL = """
CREATE TABLE IF NOT EXISTS raw(
  id TEXT PRIMARY KEY, hash TEXT, mtype TEXT, org TEXT, access TEXT,
  date TEXT, title TEXT, body TEXT, path TEXT, fetched TEXT);

CREATE TABLE IF NOT EXISTS docs(
  id TEXT PRIMARY KEY, raw_id TEXT, hash TEXT, mtype TEXT, org TEXT,
  access TEXT, date TEXT, title TEXT, body TEXT, chars INTEGER);

CREATE TABLE IF NOT EXISTS chunks(
  id TEXT PRIMARY KEY, doc_id TEXT, ord INTEGER, section TEXT,
  body TEXT, embed_text TEXT, chars INTEGER,
  vec BLOB, dim INTEGER, model TEXT,
  mtype TEXT, org TEXT, date TEXT, access TEXT);

CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS chunks_access ON chunks(access);
"""


def open_db(path: Path) -> sqlite3.Connection:
    """★사내 Oracle/Postgres 로 옮길 때 바꿀 곳은 여기와 DDL 뿐이다."""
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    c.executescript(DDL)
    c.commit()
    return c


def fingerprint(x: Any) -> str:
    """내용 지문. 같으면 안 바뀐 것이다."""
    return hashlib.sha256(str(x).encode("utf-8")).hexdigest()[:16]


# ── 모델 호출 ────────────────────────────────────────────────────
def _headers(cfg, key_name):
    h = {"Content-Type": "application/json"}
    if cfg.get(key_name):
        h["Authorization"] = "Bearer " + cfg[key_name]
    return h


def _net(cfg):
    return {"timeout": float(cfg.get("TIMEOUT") or 60),
            "verify": cfg.get("VERIFY_SSL", "1") != "0",
            "proxy": cfg.get("HTTP_PROXY") or None}


FAKE_DIM = 256


def fake_embed(text: str) -> List[float]:
    """모델이 없을 때. 낱말을 해시해 자리에 더한다. **뜻은 못 잰다.**"""
    import re
    v = [0.0] * FAKE_DIM
    for w in re.findall(r"[가-힣]{2,}|[A-Za-z0-9]{2,}", (text or "").lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % FAKE_DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed(texts: Sequence[str], cfg: Dict[str, str]) -> Tuple[List[List[float]], str]:
    """→ (벡터목록, 모델이름). OpenAI 호환 REST 를 그대로 쓴다."""
    if not (cfg.get("EMBED_BASE_URL") and cfg.get("EMBED_MODEL")):
        return [fake_embed(t) for t in texts], "fake(no-model)"
    import httpx
    r = httpx.post(cfg["EMBED_BASE_URL"].rstrip("/") + "/embeddings",
                   json={"model": cfg["EMBED_MODEL"], "input": list(texts)},
                   headers=_headers(cfg, "EMBED_API_KEY"), **_net(cfg))
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]], cfg["EMBED_MODEL"]


def generate(system: str, prompt: str, cfg: Dict[str, str]) -> Optional[str]:
    """생성 모델을 부른다. 없으면 None."""
    if not (cfg.get("LLM_BASE_URL") and cfg.get("LLM_MODEL")):
        return None
    import httpx
    r = httpx.post(cfg["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
                   json={"model": cfg["LLM_MODEL"], "temperature": 0,
                         "messages": [{"role": "system", "content": system},
                                      {"role": "user", "content": prompt}]},
                   headers=_headers(cfg, "LLM_API_KEY"), **_net(cfg))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# ── 벡터 ↔ 이진 ──────────────────────────────────────────────────
def pack(vec: Sequence[float]) -> bytes:
    """float32 이진. 글자(JSON)로 넣는 것보다 약 3배 작다."""
    return struct.pack("<%df" % len(vec), *vec)


def unpack(blob: bytes) -> List[float]:
    return list(struct.unpack("<%df" % (len(blob) // 4), blob))


# ── Milvus ───────────────────────────────────────────────────────
def milvus_client(cfg: Dict[str, str]):
    """★인증값이 비면 아예 안 넘긴다. token=None 을 넘기면 안 된다 —
    pymilvus 기본값은 None 이 아니라 "" 라서 안 주는 것과 다르다."""
    from pymilvus import MilvusClient
    kw: Dict[str, Any] = {"uri": cfg["MILVUS_URI"]}
    if cfg.get("MILVUS_TOKEN"):
        kw["token"] = cfg["MILVUS_TOKEN"]
    elif cfg.get("MILVUS_USER"):
        kw["user"] = cfg["MILVUS_USER"]
        kw["password"] = cfg.get("MILVUS_PASSWORD", "")
    return MilvusClient(**kw)


def milvus_collection(mc, name: str, dim: int) -> str:
    """없으면 만든다. ★차원은 첫 벡터가 정한다 — 모델을 바꾸면 다시 만들어야 한다."""
    if mc.has_collection(name):
        return name
    from pymilvus import DataType
    s = mc.create_schema(auto_id=False, enable_dynamic_field=False)
    s.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
    s.add_field("vec", DataType.FLOAT_VECTOR, dim=dim)
    s.add_field("access", DataType.VARCHAR, max_length=16)
    s.add_field("mtype", DataType.VARCHAR, max_length=32)
    s.add_field("org", DataType.VARCHAR, max_length=128)
    s.add_field("date", DataType.VARCHAR, max_length=10)
    s.add_field("body", DataType.VARCHAR, max_length=8192)
    idx = mc.prepare_index_params()
    idx.add_index(field_name="vec", index_type="HNSW", metric_type="COSINE",
                  params={"M": 16, "efConstruction": 200})
    mc.create_collection(collection_name=name, schema=s, index_params=idx)
    return name


def milvus_upsert(conn, cfg) -> int:
    rows = conn.execute("SELECT id,vec,dim,access,mtype,org,date,body FROM chunks"
                        " WHERE vec IS NOT NULL").fetchall()
    if not rows:
        return 0
    mc = milvus_client(cfg)
    name = milvus_collection(mc, cfg.get("MILVUS_COLLECTION", "mis_chunk"), rows[0]["dim"])
    mc.upsert(collection_name=name, data=[{
        "id": r["id"], "vec": unpack(r["vec"]), "access": r["access"] or "전사",
        "mtype": r["mtype"] or "", "org": (r["org"] or "")[:128],
        "date": (r["date"] or "")[:10], "body": (r["body"] or "")[:8192]} for r in rows])
    return len(rows)


def milvus_search(qvec, visible, k, cfg):
    mc = milvus_client(cfg)
    expr = "access in [%s]" % ",".join('"%s"' % x for x in visible)   # ★Milvus 안에서 걸린다
    res = mc.search(collection_name=cfg.get("MILVUS_COLLECTION", "mis_chunk"),
                    data=[qvec], filter=expr, limit=k,
                    search_params={"metric_type": "COSINE"},
                    output_fields=["id", "mtype", "org", "date", "body"])
    return [{"score": round(float(h["distance"]), 4), "section": "",
             **{k2: h["entity"].get(k2) for k2 in ("id", "mtype", "org", "date", "body")}}
            for h in (res[0] if res else [])]


# ── 보기 (Spyder 변수 탐색기·콘솔) ────────────────────────────────
_HIDE = {"vec", "embed_text"}


def table(rows, cols: Optional[List[str]] = None, cut: int = 100):
    """list[dict] 또는 sqlite Row 목록 → pandas 표.
    ★변수 탐색기에서 더블클릭하면 표가 열린다. pandas 가 없으면 리스트 그대로."""
    data = [dict(r) for r in rows]
    data = [{k: v for k, v in r.items() if k not in _HIDE} for r in data]
    if cut:
        data = [{k: (v[:cut] + "…" if isinstance(v, str) and len(v) > cut else v)
                 for k, v in r.items()} for r in data]
    if cols:
        data = [{k: r.get(k) for k in cols} for r in data]
    if not HAS_PANDAS:
        return data
    df = _pd.DataFrame(data)
    return df[cols] if cols and len(df) else df


def show(rows, cols: Optional[List[str]] = None, n: int = 20, width: int = 30) -> None:
    """콘솔에 표로. 변수 탐색기를 안 열고 바로 볼 때."""
    data = rows.to_dict("records") if (HAS_PANDAS and hasattr(rows, "to_dict")) \
        else [dict(r) for r in rows]
    if not data:
        print("  (없음)")
        return
    cols = cols or [c for c in data[0] if c not in _HIDE][:8]
    w = {c: min(width, max(len(str(c)), *(len(str(r.get(c, ""))) for r in data[:n])))
         for c in cols}
    print("  " + " ".join(str(c)[:w[c]].ljust(w[c]) for c in cols))
    print("  " + "-" * (sum(w.values()) + len(cols) - 1))
    for r in data[:n]:
        print("  " + " ".join(str(r.get(c, ""))[:w[c]].ljust(w[c]) for c in cols))
    if len(data) > n:
        print("  … 총 %d행 중 %d행" % (len(data), n))


def status(conn):
    """단계별로 몇 건 쌓였나 → 표"""
    out = []
    for stage, tbl in (("①② collect·raw", "raw"), ("③ normalize", "docs"),
                       ("④⑤ chunk·vector", "chunks")):
        out.append({"stage": stage, "table": tbl,
                    "rows": conn.execute("SELECT COUNT(*) FROM %s" % tbl).fetchone()[0]})
    out[-1]["vectors"] = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE vec IS NOT NULL").fetchone()[0]
    return table(out, cut=0)


def by_type(conn):
    return table([dict(zip(("mtype", "docs", "chunks", "vectors", "avg_chars"), r))
                  for r in conn.execute(
        "SELECT mtype, COUNT(DISTINCT doc_id), COUNT(*), SUM(vec IS NOT NULL),"
        " CAST(AVG(chars) AS INT) FROM chunks GROUP BY 1 ORDER BY 3 DESC")], cut=0)
