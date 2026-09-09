# -*- coding: utf-8 -*-
"""collect → raw → normalize → chunk → embed → search → answer

한 셀씩 실행하며 데이터가 만들어지는 과정을 보고, **그대로 사내 운영에 쓴다.**


쓰는 법
    Spyder 로 이 파일을 연다. 셀 경계는 `# %%`.
    맨 위 setup 셀부터 **Ctrl+Enter** 로 하나씩 내려간다.
    각 셀은 ⑴ 함수를 정의하고 ⑵ 바로 실행해 ⑶ 결과를 **표로** 남긴다.
    변수 탐색기에서 그 표를 **더블클릭하면 열린다.**

무엇이 어디 있나
    이 파일    사내 문서에 맞춰 **손볼 로직**만  — 정규화 규칙 · 조각 규칙 · 점수
    tools.py   한 번 붙이면 안 건드리는 배관     — .env · 모델호출 · DB · Milvus

사내에서 갈아끼울 곳 — **`.env` 값 세 개뿐. 코드는 손대지 않는다.**
    ① 임베딩 모델   EMBED_BASE_URL · EMBED_MODEL      ⑤ embed · ⑥ search
    ② 생성 모델     LLM_BASE_URL · LLM_MODEL          ⑦ answer
    ③ 벡터 저장소   VECTOR_KIND=milvus · MILVUS_URI   ⑤ 저장 · ⑥ 검색

운영에 쓸 수 있는 이유
    · **이미 한 일은 건너뛴다.** 새 문서만 처리한다. 몇 번을 돌려도 안전하다.
    · **문서가 바뀌면 알아서 다시 만든다.** 내용 지문(hash)으로 판단한다.
    · **한 건이 실패해도 나머지는 계속한다.**
    · 비밀값이 코드에 없다. 전부 `.env` 경유.
"""

# %% ═══════════════════════════════════════════════════════════════
#  setup — 설정을 읽고 무엇이 붙었는지 본다
# ══════════════════════════════════════════════════════════════════
import datetime
import json
import math
import re
import time
from pathlib import Path

import sensing_tools as tools

HERE = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
CFG = tools.load_config(HERE)

DOC_DIR = Path(CFG.get("DOC_DIR") or (HERE / "문서")).expanduser()
DB_PATH = Path(CFG.get("DB_PATH") or (HERE / "sensing.db")).expanduser()
STORE = "milvus" if (CFG.get("VECTOR_KIND") == "milvus" and CFG.get("MILVUS_URI")) else "sqlite"

conn = tools.open_db(DB_PATH)

print("  문서 폴더 : %s  (%d건)" % (DOC_DIR, len(list(DOC_DIR.glob("*")))))
print("  적재 DB   : %s\n" % DB_PATH)
for name, ok, desc in tools.check_connections(CFG):
    print("  %s %-14s %s" % ("✔" if ok else "✘", name, desc))
print("\n  값을 채우려면  cp .env.example .env  후 편집하고 이 셀을 다시 돌린다.")

status = tools.status(conn)          # ★변수 탐색기에서 더블클릭
print()
tools.show(status)


# %% ═══════════════════════════════════════════════════════════════
#  ① collect — 폴더의 파일을 읽어 온다
# ══════════════════════════════════════════════════════════════════
#  파일 맨 위 `--- ... ---` 가 메타다.
#
#  ★유형·출처·권한을 **이 단계에서** 정한다.
#    나중에 정하면 늦다. 권한을 모르는 채로 표에 들어가면
#    검색이 "누가 볼 수 있는가" 를 판단할 수 없다.

FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
READABLE = (".md", ".txt", ".markdown")


def split_front_matter(text):
    """--- 사이의 메타를 떼어 낸다 → (meta, body)"""
    m = FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end():]


def guess_from_name(p):
    """머리말이 없을 때. `report_A증권_….md` → mtype report, org A증권"""
    parts = p.stem.split("_")
    mtype = parts[0] if parts[0] in ("report", "research", "external_doc",
                                     "internal_doc", "mail", "news") else "other"
    return mtype, (parts[1] if len(parts) >= 3 else "-")


def collect(folder=None):
    """폴더를 훑어 목록으로. 아직 저장하지 않는다."""
    folder = Path(folder or DOC_DIR)
    found, skipped = [], []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in READABLE:
            skipped.append(p.name)          # PDF·한글은 별도 파서가 필요하다
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        meta, body = split_front_matter(text)
        g_type, g_org = guess_from_name(p)
        found.append({
            "id": "raw:" + tools.fingerprint(str(p)),
            "hash": tools.fingerprint(text),          # ★내용이 바뀌면 이 값이 바뀐다
            "mtype": meta.get("material_type", g_type),
            "org": meta.get("owner_org", g_org),
            "access": meta.get("access_level", "전사"),
            "date": meta.get("published_at"),
            "title": meta.get("title", p.stem),
            "body": body,
            "path": str(p),
            "fetched": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    return found, skipped


found, unreadable = collect()
found_tbl = tools.table(found, ["mtype", "org", "access", "date", "title"])  # ★변수 탐색기

print("%d건 거뒀다 (읽을 수 없는 확장자 %d건)\n" % (len(found), len(unreadable)))
tools.show(found_tbl)


# %% ═══════════════════════════════════════════════════════════════
#  ② save_raw — 손대지 않고 그대로. **바뀐 것만 갱신한다**
# ══════════════════════════════════════════════════════════════════
#  ★왜 손대지 않나
#    뒤 단계(정규화·조각) 규칙은 반드시 바뀐다. 그때 ③부터 다시 돌리면 된다.
#    여기서 손질해 버리면 원본이 없어 되돌릴 수 없다.
#
#  ★운영에서 중요한 곳 — 지문이 같으면 건너뛴다.
#    매일 돌려도 새 문서·고쳐진 문서만 처리된다.

def save_raw(items):
    known = {r["id"]: r["hash"] for r in conn.execute("SELECT id,hash FROM raw")}
    fresh = [x for x in items if x["id"] not in known]
    changed = [x for x in items if x["id"] in known and known[x["id"]] != x["hash"]]

    for x in changed:                    # 문서가 바뀌었으면 뒤 단계 결과를 지운다
        conn.execute("DELETE FROM chunks WHERE doc_id IN"
                     " (SELECT id FROM docs WHERE raw_id=?)", (x["id"],))
        conn.execute("DELETE FROM docs WHERE raw_id=?", (x["id"],))

    conn.executemany(
        "INSERT OR REPLACE INTO raw(id,hash,mtype,org,access,date,title,body,path,fetched)"
        " VALUES(:id,:hash,:mtype,:org,:access,:date,:title,:body,:path,:fetched)",
        fresh + changed)
    conn.commit()
    return {"new": len(fresh), "changed": len(changed),
            "unchanged": len(items) - len(fresh) - len(changed)}


saved = save_raw(found)
raw_tbl = tools.table(conn.execute(
    "SELECT mtype,org,access,date,title,LENGTH(body) chars FROM raw"))

print(json.dumps(saved, ensure_ascii=False))
print("★이 셀을 다시 돌려 보라. 두 번째부터는 전부 unchanged 다.\n")
tools.show(raw_tbl)


# %% ═══════════════════════════════════════════════════════════════
#  ③ normalize — 유형별로 다르게 손질해 칸을 통일한다
# ══════════════════════════════════════════════════════════════════
#  여기를 지나면 유형이 달라도 뒤 단계는 유형을 몰라도 된다.
#  ★사내 문서 형식에 맞춰 고칠 곳이 대부분 이 셀 안이다.

TAG = re.compile(r"<[^>]+>")
BLANKS = re.compile(r"\n{3,}")
LONG_URL = re.compile(r"https?://\S{60,}")

# 메일에서 버릴 줄 — 안 버리면 **모든 메일이 서로 비슷해 보인다**
MAIL_DROP = [re.compile(p) for p in (
    r"^(안녕|감사|수고|고맙)", r"^\s*[-—=_]{3,}\s*$", r"^\s*>",
    r"^(보낸사람|받는사람|보낸\s*날짜|제목)\s*[:：]",
    r"본\s*메일은|무단\s*전재|기밀", r"\d{2,3}-\d{3,4}-\d{4}", r"^[\w.]+@[\w.]+$")]

# 리포트에서 버릴 줄 — 모든 리포트에 똑같이 붙는 문구
REPORT_DROP = [re.compile(p) for p in (
    r"투자\s*참고\s*자료|법적\s*책임", r"Compliance\s*Notice|제공된\s*사실이\s*없",
    r"무단\s*전재|저작권은\s*당사", r"^\s*Analyst\s", r"\d{2,3}-\d{3,4}-\d{4}")]

# "1. 현황" "3.2 검사" "4.2.1 접합" 을 절 제목으로 본다
SEC_NO = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+(\S.{0,80})$")


def clean(text):
    """모든 유형에 공통 — 태그·군더더기 제거"""
    import html
    text = html.unescape(text or "")
    text = TAG.sub(" ", text)
    text = LONG_URL.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = BLANKS.sub("\n\n", text)
    return "\n".join(l.strip() for l in text.split("\n")).strip()


def drop_lines(text, rules):
    """규칙에 걸리는 줄을 버린다 → (남은 글, 버린 줄 수)"""
    keep, dropped = [], 0
    for line in text.split("\n"):
        s = line.strip()
        if s and any(p.search(s) for p in rules):
            dropped += 1
            continue
        keep.append(line)
    return "\n".join(keep), dropped


def mark_sections(text):
    """번호 붙은 줄을 `## ` 로 바꾼다 — ④에서 자르는 경계가 된다"""
    out = []
    for line in text.split("\n"):
        s = line.strip()
        m = None if s.startswith("|") else SEC_NO.match(s)
        out.append("## %s %s" % (m.group(1), m.group(2)) if m else line)
    return "\n".join(out)


def normalize_one(r):
    """raw 한 건 → docs 한 건. ★유형마다 다르게."""
    body, mtype, dropped = clean(r["body"]), r["mtype"], 0

    if mtype == "mail":
        body, dropped = drop_lines(body, MAIL_DROP)
        body = " ".join(l for l in body.split("\n") if l.strip())
    elif mtype == "report":
        body, dropped = drop_lines(body, REPORT_DROP)
        body = re.sub(r"^\s*\[?\s*(Executive Summary|요약)\s*\]?\s*$",
                      r"## \1", body, flags=re.I | re.M)
        body = mark_sections(body)
    elif mtype in ("research", "external_doc", "internal_doc"):
        body = re.sub(r"^\s*\d+\)\s.*$", "", body, flags=re.M)      # 각주
        body = mark_sections(body)

    body = body.strip() or r["title"]        # 본문이 비면 제목만이라도
    return {"id": "doc:" + r["id"].split(":")[1], "raw_id": r["id"], "hash": r["hash"],
            "mtype": mtype, "org": r["org"], "access": r["access"], "date": r["date"],
            "title": r["title"], "body": body, "chars": len(body), "dropped": dropped}


def normalize():
    """아직 docs 가 안 만들어진 raw 만 처리한다."""
    todo = conn.execute(
        "SELECT * FROM raw WHERE id NOT IN (SELECT raw_id FROM docs)").fetchall()
    made, failed = [], []
    for r in todo:
        try:
            made.append(normalize_one(r))
        except Exception as e:               # ★한 건이 실패해도 나머지는 계속
            failed.append({"title": r["title"], "reason": str(e)[:60]})

    if made:
        conn.executemany(
            "INSERT OR REPLACE INTO docs(id,raw_id,hash,mtype,org,access,date,title,body,chars)"
            " VALUES(:id,:raw_id,:hash,:mtype,:org,:access,:date,:title,:body,:chars)", made)
        conn.commit()
    return made, failed


new_docs, norm_failed = normalize()

# 원문과 나란히 놓고 무엇이 얼마나 줄었는지 본다
shrink_tbl = tools.table(conn.execute("""
    SELECT docs.mtype, docs.org, LENGTH(raw.body) raw_chars, docs.chars doc_chars,
           ROUND(100.0*docs.chars/LENGTH(raw.body)) kept_pct, docs.title
    FROM docs JOIN raw ON raw.id=docs.raw_id ORDER BY kept_pct"""))

print("새로 만든 문서 %d건 · 실패 %d건\n" % (len(new_docs), len(norm_failed)))
tools.show(shrink_tbl)
print("\n★mail 을 보라 — 원문의 4분의 1만 남는다.")
print("  인사말·서명·인용부를 안 지우면 모든 메일이 서로 비슷해 보인다.")


# %% ═══════════════════════════════════════════════════════════════
#  ③-b 무엇이 지워졌는지 눈으로 — 메일 하나만
# ══════════════════════════════════════════════════════════════════
_m = conn.execute(
    "SELECT raw.body before_, docs.body after_ FROM docs JOIN raw ON raw.id=docs.raw_id"
    " WHERE docs.mtype='mail' LIMIT 1").fetchone()
if _m:
    print("── 원문 ──")
    print("  " + "\n  ".join(_m["before_"].strip().split("\n")[:14]))
    print("\n── 정규화 뒤 ──")
    print("  " + _m["after_"])
else:
    print("mail 유형 문서가 없다")


# %% ═══════════════════════════════════════════════════════════════
#  ④ chunk — 검색의 단위로 자른다
# ══════════════════════════════════════════════════════════════════
#  자르는 우선순위
#    ① 절 제목(## …)  뜻이 온전히 유지된다. 가장 좋다
#    ② 문단           절이 너무 길 때
#
#  ★news·mail 은 자르지 않는다. 이미 한 덩어리다.
#  ★절 제목을 본문에 **함께 남긴다.** 빼면 그 줄의 내용이 사라진다.

SIZE = {"report": 1500, "research": 1200, "external_doc": 1000,
        "internal_doc": 1200, "mail": 0, "news": 0, "other": 0}      # 0 = 자르지 않음


def split_by_section(body):
    """`## ` 를 경계로 나눈다 → [(section, text)]"""
    out, sec, buf = [], "", []

    def flush(s, lines):
        text = "\n".join(lines).strip()
        text = (s + "\n" + text).strip() if s else text
        if text:
            out.append((s, text))

    for line in body.split("\n"):
        if line.strip().startswith("## "):
            flush(sec, buf)
            sec, buf = line.strip()[3:], []
        else:
            buf.append(line)
    flush(sec, buf)
    return out


def split_long(text, limit):
    """절 하나가 너무 크면 문단으로 나눈다"""
    if len(text) <= limit:
        return [text]
    out, buf = [], ""
    for para in [p for p in text.split("\n\n") if p.strip()]:
        if buf and len(buf) + len(para) > limit:
            out.append(buf)
            buf = para
        else:
            buf = (buf + "\n\n" + para).strip()
    if buf:
        out.append(buf)
    return out


def top_no(section):
    """'4.2.1 접합 방식' → '4'. 형제 절인지 보는 데 쓴다."""
    m = re.match(r"^\s*(\d+)", section or "")
    return m.group(1) if m else ""


def merge_small(pieces, floor=80):
    """너무 짧은 조각을 앞에 붙인다. 50자짜리가 쏟아지는 것을 막는다.

    ★아무거나 합치면 안 된다. 절은 의미 단위다. 합치는 것은 둘뿐 —
        ① 절 이름이 없는 자투리(문서 머리말 등)
        ② **같은 상위 절 밑의 형제**  4.1 · 4.2 · 4.2.1 → 모두 '4' 밑

      실측: 이 조건 없이 합쳤더니 표준 문서(JEDEC) 절 13개가
            **한 조각으로 뭉개졌다.** 절 구조가 통째로 사라진다.
    """
    out = []
    for sec, text in pieces:
        sibling = (out and top_no(out[-1][0]) and top_no(out[-1][0]) == top_no(sec))
        if out and len(text) < floor and (sibling or not sec):
            psec, ptext = out[-1]
            out[-1] = (psec + (" · " + sec if sec and sec not in psec else ""),
                       ptext + "\n\n" + text)
        else:
            out.append((sec, text))

    # 맨 앞이 자투리면 **뒤 조각에** 붙인다 (앞에 붙일 것이 없으므로)
    if len(out) > 1 and len(out[0][1]) < floor:
        head = out.pop(0)
        out[0] = (out[0][0], head[1] + "\n\n" + out[0][1])
    return out


def make_embed_text(doc, text):
    """벡터로 만들 글. ★저장하는 글과 다르다.

    조각만 임베딩하면 '어느 문서의 어느 부분인지' 가 벡터에 안 담긴다.
    앞에 머리말을 붙여 준다. 저장본에는 안 붙인다 — AI 입력 토큰이 늘기 때문.
    """
    head = " · ".join(x for x in (doc["org"], doc["title"], doc["date"]) if x)
    return "[%s]\n%s" % (head, text)


def chunk():
    """아직 조각이 없는 문서만 처리한다."""
    todo = conn.execute(
        "SELECT * FROM docs WHERE id NOT IN (SELECT DISTINCT doc_id FROM chunks)").fetchall()
    made = []
    for d in todo:
        limit = SIZE.get(d["mtype"], 0)
        if not limit:
            pieces = [("", d["body"])]
        else:
            pieces = []
            for sec, text in split_by_section(d["body"]):
                for part in split_long(text, limit):
                    pieces.append((sec, part))
            pieces = merge_small(pieces)

        for i, (sec, text) in enumerate(pieces):
            made.append({
                "id": "%s#%d" % (d["id"], i), "doc_id": d["id"], "ord": i,
                "section": sec, "body": text, "embed_text": make_embed_text(d, text),
                "chars": len(text), "mtype": d["mtype"], "org": d["org"],
                "date": d["date"], "access": d["access"]})

    if made:
        conn.executemany(
            "INSERT OR REPLACE INTO chunks"
            "(id,doc_id,ord,section,body,embed_text,chars,mtype,org,date,access)"
            " VALUES(:id,:doc_id,:ord,:section,:body,:embed_text,:chars,:mtype,:org,:date,:access)",
            made)
        conn.commit()
    return made


new_chunks = chunk()
chunk_tbl = tools.table(conn.execute(
    "SELECT mtype,org,ord,section,chars,body FROM chunks ORDER BY doc_id,ord"))
loss_tbl = tools.table(conn.execute("""
    SELECT docs.mtype, docs.org, docs.chars doc_chars, COUNT(chunks.id) n_chunks,
           SUM(chunks.chars) sum_chars, ROUND(100.0*SUM(chunks.chars)/docs.chars) kept_pct
    FROM docs JOIN chunks ON chunks.doc_id=docs.id GROUP BY docs.id ORDER BY kept_pct"""))

print("새 조각 %d개 (전체 %d개)\n" % (
    len(new_chunks), conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]))
tools.show(chunk_tbl, ["mtype", "org", "section", "chars", "body"], n=12)
print("\n★손실이 없는지 글자 수로 확인한다")
tools.show(loss_tbl)


# %% ═══════════════════════════════════════════════════════════════
#  ④-b 저장하는 글과 임베딩하는 글이 어떻게 다른가
# ══════════════════════════════════════════════════════════════════
_c = conn.execute("SELECT body,embed_text FROM chunks LIMIT 1").fetchone()
print("── body (AI 에 넣을 것) ──")
print("  " + _c["body"][:150].replace("\n", "\n  "))
print("\n── embed_text (벡터로 만들 것) ──")
print("  " + _c["embed_text"][:150].replace("\n", "\n  "))
print("\n★앞에 [출처 · 제목 · 날짜] 가 붙었다.")
print("  안 붙이면 문서를 자른 뒤 둘째 조각부터 '어느 문서인지' 가 사라진다.")


# %% ═══════════════════════════════════════════════════════════════
#  ⑤ embed — 글을 숫자 목록으로 바꿔 넣는다  ★모델을 처음 쓰는 자리
# ══════════════════════════════════════════════════════════════════
#  `.env` 에 EMBED_BASE_URL·EMBED_MODEL 이 있으면 **사내 모델을 부른다.**
#  없으면 가짜 벡터로 흐름만 돈다(뜻은 못 잰다).

def embed_all(batch=64, redo=False):
    """아직 벡터가 없는 조각만. ★묶어서 부른다 — 한 건씩이면 매우 느리다."""
    q = "SELECT id, embed_text FROM chunks" + ("" if redo else " WHERE vec IS NULL")
    todo = conn.execute(q).fetchall()
    if not todo:
        return {"made": 0, "note": "새로 만들 것이 없다"}

    t0, model, dim = time.time(), "-", 0
    for i in range(0, len(todo), batch):
        part = todo[i:i + batch]
        vecs, model = tools.embed([r["embed_text"] for r in part], CFG)
        dim = len(vecs[0])
        conn.executemany("UPDATE chunks SET vec=?, dim=?, model=? WHERE id=?",
                         [(tools.pack(v), dim, model, r["id"]) for v, r in zip(vecs, part)])
    conn.commit()
    pushed = tools.milvus_upsert(conn, CFG) if STORE == "milvus" else 0
    return {"made": len(todo), "model": model, "dim": dim,
            "milvus_pushed": pushed, "seconds": round(time.time() - t0, 1)}


embed_result = embed_all()
print(json.dumps(embed_result, ensure_ascii=False), "\n")

vec_tbl = tools.table(conn.execute(
    "SELECT mtype,org,section,dim,model,LENGTH(vec) bytes FROM chunks WHERE vec IS NOT NULL"))
tools.show(vec_tbl, n=8)

_v = conn.execute("SELECT vec,model FROM chunks WHERE vec IS NOT NULL LIMIT 1").fetchone()
if _v:
    nums = tools.unpack(_v["vec"])
    print("\n  model  : %s" % _v["model"])
    print("  앞 8개 : %s" % [round(x, 4) for x in nums[:8]])
    print("  길이   : %.4f  (1이면 정상 — 방향만 남기고 크기를 없앴다)"
          % math.sqrt(sum(x * x for x in nums)))
if str(embed_result.get("model", "")).startswith("fake"):
    print("\n  ★가짜 벡터다. .env 를 채우고  embed_all(redo=True)  로 다시 만든다.")


# %% ═══════════════════════════════════════════════════════════════
#  다 됐다 — 무엇이 얼마나 쌓였나
# ══════════════════════════════════════════════════════════════════
status = tools.status(conn)          # ★변수 탐색기
per_type = tools.by_type(conn)       # ★변수 탐색기
tools.show(status)
print()
tools.show(per_type)


# %% ═══════════════════════════════════════════════════════════════
#  ⑥-a bm25 — 어휘검색. 벡터가 못 하는 것을 한다
# ══════════════════════════════════════════════════════════════════
#  벡터는 **뜻**으로 찾는다.   "낸드 감산" → "NAND 웨이퍼 투입 축소"  ○
#  어휘는 **글자**로 찾는다.   "M16"      → M16 이 적힌 문서          ○
#
#  각자 못 하는 것이 있다.
#    벡터  고유명사·모델명·숫자에 약하다. HBM4 와 HBM3E 를 비슷하게 본다
#    어휘  같은 뜻 다른 말을 못 찾는다. "수율" 과 "yield" 가 남남이다
#
#  ★사내 모델이 없을 때 특히 값어치가 있다 — BM25 는 모델 없이도 제대로 작동한다.
#
#  한국어를 어떻게 자르나
#    형태소 분석기(은전한닢·nori)가 사내에 없을 수 있다. 그것에 기대지 않는다.
#      한글    **두 글자 묶음**.  "초기수율" → 초기·기수·수율
#      영문·숫자  낱말 그대로 + 소문자
#    ★사내에 분석기가 있으면 `tokenize()` 하나만 바꾼다.

K1, B = 1.2, 0.75          # BM25 상수 — 빈도 포화, 길이 보정
HANGUL = re.compile(r"[가-힣]{2,}")
LATIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
STOP = {"그리고", "그러나", "때문", "위해", "대한", "관련", "있다", "없다",
        "무엇", "어떻게", "어떤", "이다", "인가", "the", "and", "for", "with"}
JOSA = re.compile(r"(은|는|이|가|을|를|의|에|에서|으로|로|와|과|도|만|이나|인가)$")


def tokenize(text, is_query=False):
    """글 → 낱말 묶음. ★사내에 형태소 분석기가 있으면 이 함수만 바꾼다.

    ★질문 쪽은 조사를 뗀다. 안 떼면 쓰레기 낱말이 생긴다 —
      "기준은" → 기준은·기준·**준은** … '준은' 이 엉뚱한 문서와 맞는다(실측).
    """
    out = []
    for m in HANGUL.finditer(text or ""):
        w = JOSA.sub("", m.group()) if is_query else m.group()
        if len(w) < 2:
            continue
        out.append(w)
        out += [w[i:i + 2] for i in range(len(w) - 1)]      # 두 글자 묶음
    for m in LATIN.finditer(text or ""):
        w = m.group().lower()
        if len(w) >= 2:
            out.append(w)
    return [x for x in out if x not in STOP]


def build_index():
    """조각 전부를 훑어 BM25 색인을 만든다.

    ★수십만 건이 되면 메모리에 두면 안 된다.
      그때는 사내 검색엔진(Elasticsearch/OpenSearch)으로 옮긴다.
      돌려주는 모양이 같으면 ⑥-b 는 손대지 않는다.
    """
    ids, tf, dl, df = [], [], [], {}
    for r in conn.execute("SELECT id, embed_text FROM chunks"):
        words = tokenize(r["embed_text"])
        f = {}
        for w in words:
            f[w] = f.get(w, 0) + 1
        ids.append(r["id"])
        tf.append(f)
        dl.append(len(words))
        for w in f:
            df[w] = df.get(w, 0) + 1
    return {"ids": ids, "tf": tf, "dl": dl, "df": df, "n": len(ids),
            "avg_len": sum(dl) / len(dl) if dl else 0}


def bm25_search(query, index, allow=None, k=200):
    """→ [(chunk_id, score)] 내림차순. allow 를 주면 그 안에서만."""
    qwords = set(tokenize(query, is_query=True))
    if not qwords or not index["n"]:
        return []
    out = []
    for i, cid in enumerate(index["ids"]):
        if allow is not None and cid not in allow:
            continue
        f, length, score = index["tf"][i], index["dl"][i], 0.0
        for w in qwords:
            freq = f.get(w)
            if not freq:
                continue
            idf = math.log(1 + (index["n"] - index["df"].get(w, 0) + 0.5)
                           / (index["df"].get(w, 0) + 0.5))
            score += idf * (freq * (K1 + 1)) / (
                freq + K1 * (1 - B + B * length / max(1e-9, index["avg_len"])))
        if score > 0:
            out.append((cid, round(score, 4)))
    out.sort(key=lambda x: -x[1])
    return out[:k]


index = build_index()
_probe = bm25_search("HBM4 수율이 왜 낮은가?", index)

print("색인 — 조각 %d개 · 낱말 %d종 · 평균길이 %.0f\n" % (
    index["n"], len(index["df"]), index["avg_len"]))
print("  어휘검색 상위 5")
for cid, s in _probe[:5]:
    r = conn.execute("SELECT mtype,org,body FROM chunks WHERE id=?", (cid,)).fetchone()
    print("    [%6.2f] %-14s %-11s %s" % (
        s, r["mtype"], (r["org"] or "-")[:11], r["body"].replace("\n", " ")[:40]))

print("\n  ★어느 낱말이 얼마나 기여했나 (1위 조각)")
if _probe:
    _i = index["ids"].index(_probe[0][0])
    for w in sorted(set(tokenize("HBM4 수율이 왜 낮은가?", is_query=True)),
                    key=lambda w: -index["tf"][_i].get(w, 0))[:5]:
        freq = index["tf"][_i].get(w, 0)
        if freq:
            print("    %-10s 문서내 %d회 · 이 낱말이 든 조각 %d개"
                  % (w, freq, index["df"].get(w, 0)))


# %% ═══════════════════════════════════════════════════════════════
#  ⑥-b search — 두 갈래를 합치고(RRF) 골라낸다(MMR)
# ══════════════════════════════════════════════════════════════════
#  순서
#    ① 좁히기   권한          ★가장 먼저
#    ② 두 갈래  벡터 + BM25
#    ③ 융합     RRF — 점수가 아니라 **등수**를 합친다
#    ④ 보정     유형가중 × 시의성  (힘을 40%로 묶는다)
#    ⑤ 고르기   MMR + 유형별 몫 + 중복 제거
#
#  ★권한을 먼저 거르는 이유
#    나중에 거르면 근거가 비고, "뭔가 있는데 안 보여준다" 를 알 수 있어
#    그 자체가 정보 노출이 된다.

VISIBLE = {"전사": ["전사"], "부서": ["전사", "부서"],
           "제한": ["전사", "부서", "제한"]}

TYPE_WEIGHT = {"internal_doc": 1.00, "filing": 1.00, "official": 0.95,
               "external_doc": 0.95, "report": 0.95, "research": 0.95,
               "news": 0.85, "mail": 0.80, "other": 0.90}

HALF_LIFE = {"news": 7, "official": 14, "report": 30, "research": 90,
             "external_doc": 180, "mail": 60, "internal_doc": 0, "filing": 0, "other": 30}

QUOTA = {"internal_doc": 2, "report": 2, "research": 2, "external_doc": 2,
         "official": 2, "filing": 2, "news": 3, "mail": 1, "other": 2}

RRF_K = 60           # 등수 역수를 더할 때의 완충값. 널리 쓰이는 값
PRIOR = 0.4          # 유형·시의성이 관련도를 흔들 수 있는 최대 폭
LEX_FLOOR = 0.20     # 어휘 갈래에서 1위 대비 이 비율 미만은 버린다
MMR_LAMBDA = 0.7     # 1이면 점수만, 낮출수록 다양해진다


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b))      # 길이가 1이라 내적이 곧 코사인


def recency(mtype, date, today=None):
    """유형별 반감기로 깎는다. ★사내 문서는 안 깎는다."""
    half = HALF_LIFE.get(mtype, 30)
    if not half or not date:
        return 1.0
    try:
        from datetime import date as _d
        y1, m1, d1 = map(int, str(date)[:10].split("-"))
        today = today or time.strftime("%Y-%m-%d")
        y2, m2, d2 = map(int, today[:10].split("-"))
        age = (_d(y2, m2, d2) - _d(y1, m1, d1)).days
    except Exception:
        return 1.0
    return 0.5 ** (max(0, age) / half)


def overlap(a, b):
    """두 조각이 얼마나 겹치나 — MMR 벌점.
    ★벡터가 아니라 낱말로 잰다. 임베딩이 가짜여도 다양화는 제대로 되게."""
    A, B_ = set(tokenize(a)), set(tokenize(b))
    return len(A & B_) / len(A | B_) if (A and B_) else 0.0


def search(query, k=5, access="전사", hybrid=True, use_mmr=True, depth=200):
    visible = VISIBLE[access]

    # ── ① 좁히기 — 권한이 가장 먼저 ────────────────────────────
    allow = {r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE vec IS NOT NULL AND access IN (%s)"
        % ",".join("?" * len(visible)), visible)}
    if not allow:
        return []

    # ── ② 두 갈래 ─────────────────────────────────────────────
    qvec = tools.embed([query], CFG)[0][0]
    vec_rank = []
    if STORE == "milvus":
        vec_rank = [h["id"] for h in tools.milvus_search(qvec, visible, depth, CFG)]
    else:
        scored = []
        for r in conn.execute("SELECT id,vec FROM chunks WHERE vec IS NOT NULL"):
            if r["id"] in allow:
                scored.append((r["id"], cosine(qvec, tools.unpack(r["vec"]))))
        scored.sort(key=lambda x: -x[1])
        vec_rank = [i for i, _ in scored[:depth]]

    lex_rank = []
    if hybrid:
        hits = bm25_search(query, index, allow=allow, k=depth)
        # ★갈래 안에서 먼저 거른다. 합친 뒤에 거르면 늦다.
        #   RRF 는 등수만 쓰므로 꼴찌도 '그 갈래의 한 자리' 를 차지해 버린다.
        #   BM25 는 흔한 낱말 하나만 겹쳐도 점수가 붙는다(실측: 1.96 vs 36.86).
        if hits:
            floor = hits[0][1] * LEX_FLOOR
            lex_rank = [i for i, s in hits if s >= floor]

    # ── ③ 융합 (RRF) ──────────────────────────────────────────
    #   벡터 점수는 0~1, BM25 는 0~37 쯤. **자릿수가 다르다.**
    #   그냥 더하면 BM25 가 이긴다. 정규화해도 묶음마다 분포가 달라 흔들린다.
    #   ★그래서 점수를 버리고 **등수만** 쓴다.
    #       RRF = Σ 1 / (K + rank)
    #     · 두 갈래에서 모두 상위면 두 항이 더해져 확실히 앞선다
    #     · 한 갈래에서만 1등이어도 살아남는다   ← 핵심
    #     · 모델을 바꿔도 다시 잡을 값이 없다
    fused = {}
    for rank_list in (vec_rank, lex_rank):
        for rank, cid in enumerate(rank_list, 1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    if not fused:
        return []

    # ── ④ 보정 — ★곱셈으로 그대로 걸면 안 된다 ──────────────────
    #   RRF 점수는 폭이 매우 좁다(1위 0.0328 ~ 10위 0.0258, 차이 27%).
    #   여기에 시의성(0.5~0.9)을 그대로 곱하면 **보정이 관련도를 눌러 버린다.**
    #   실측: 벡터 1위·어휘 1위인 표준 문서가 **최종 5위**로 밀렸다.
    #         4월 문서라 시의성 0.52 로 깎였고 그날치 무관한 뉴스(0.91)가 앞섰다.
    #   ★시의성은 순위를 뒤집는 힘이 아니라 **밀어주는 힘**이어야 한다.
    top = max(fused.values())
    cand = []
    for cid, rrf in fused.items():
        r = conn.execute("SELECT mtype,org,section,body,date FROM chunks WHERE id=?",
                         (cid,)).fetchone()
        w = TYPE_WEIGHT.get(r["mtype"], 0.9)
        fresh = recency(r["mtype"], r["date"])
        rel = rrf / top                                   # 0~1 로 편다
        adj = 1 - PRIOR * (1 - w * fresh)                 # 0.6~1.0
        cand.append({"id": cid, "score": round(rel * adj, 4),
                     "rel": round(rel, 3), "weight": w,
                     "recency": round(fresh, 3), "adj": round(adj, 3),
                     "mtype": r["mtype"], "org": r["org"], "section": r["section"],
                     "date": r["date"], "body": r["body"]})
    cand.sort(key=lambda x: -x["score"])

    # ── ⑤ 고르기 — MMR + 유형별 몫 + 중복 제거 ─────────────────
    #   ★몫은 '최대'다. 자리를 채우려고 무관한 것을 넣지 않는다.
    #     못 채우면 덜 넣는다 — 근거가 적은 편이 틀린 근거보다 낫다.
    #   ★MMR — 점수는 높고 **이미 고른 것과는 다른** 조각을 차례로 뽑는다.
    #     상위가 서로 비슷하면 근거가 6건이어도 내용은 1건이다.
    picked, used = [], {}
    pool = list(cand)
    while pool and len(picked) < k:
        if use_mmr and picked:
            for x in pool:
                penalty = max(overlap(x["body"], p["body"]) for p in picked)
                x["_value"] = MMR_LAMBDA * x["score"] - (1 - MMR_LAMBDA) * penalty
            pool.sort(key=lambda x: -x["_value"])
        best = pool.pop(0)
        if used.get(best["mtype"], 0) >= QUOTA.get(best["mtype"], 2):
            continue
        if any(overlap(best["body"], p["body"]) > 0.8 for p in picked):
            continue
        used[best["mtype"]] = used.get(best["mtype"], 0) + 1
        picked.append(best)
    return picked


query = "HBM4 수율이 왜 낮은가?"
hits = search(query, access="제한")
hits_tbl = tools.table(hits, ["score", "rel", "weight", "recency", "adj",
                              "mtype", "org", "date", "body"], cut=50)   # ★변수 탐색기

print("Q. %s   (권한 제한 · 저장소 %s)\n" % (query, STORE))
tools.show(hits_tbl, width=26)


# %% ═══════════════════════════════════════════════════════════════
#  ⑥-c 비교 실험 — 하이브리드가 순위를 얼마나 고치나
# ══════════════════════════════════════════════════════════════════
#  ★사내 문서로 바꾼 뒤 이 셀이 가장 쓸모 있다.
#    어휘 갈래가 실제로 도움이 되는지, 오히려 방해가 되는지 바로 보인다.
#  같은 것을 화면에서도 볼 수 있다 — 상단 '검증' 을 켜면 축마다 접힌 채로 나온다.

for 이름, 옵션 in (("vector only", dict(hybrid=False, use_mmr=False)),
                 ("hybrid + RRF + MMR", dict())):
    print("▶ %s" % 이름)
    for i, h in enumerate(search(query, k=5, access="제한", **옵션), 1):
        print("   %d. [%.4f] %-14s %-11s %s" % (
            i, h["score"], h["mtype"], (h["org"] or "-")[:11],
            (h["body"] or "").replace("\n", " ")[:44]))
    print()

print("★권한을 바꿔 보라 — 볼 수 있는 것이 달라진다")
for a in ("전사", "부서", "제한"):
    print("  %-4s → 근거 %d건" % (a, len(search(query, k=99, access=a))))


# %% ═══════════════════════════════════════════════════════════════
#  ⑦ answer — 근거만 쓰게 하고 결과를 확인한다  ★모델을 쓰는 자리
# ══════════════════════════════════════════════════════════════════
#  검색 결과를 그대로 넣지 않는다. **유형·출처·날짜**를 함께 적어
#  AI 가 "이게 사실인지 남의 의견인지" 를 구분하게 한다.

TRUST = {"internal_doc": "1차 사실", "filing": "1차 사실", "official": "1차 사실",
         "report": "2차 해석", "research": "2차 해석", "external_doc": "2차 해석",
         "news": "3차 전달", "mail": "4차 정황", "other": "3차 전달"}

SYSTEM = """당신은 사내 임원을 돕는 정리 도우미입니다.

규칙
1. 아래 [근거] 에 적힌 내용만 씁니다. 근거 밖 지식으로 보강하지 않습니다.
2. 문장마다 사용한 근거 번호를 [1] [3] 처럼 답니다.
3. 근거에서 답을 찾을 수 없으면 "제공된 근거에서 답을 찾을 수 없습니다." 라고만 합니다.
4. 한국어로, 임원이 30초 안에 읽을 밀도로 씁니다. 없는 사실·수치를 지어내지 않습니다."""


def evidence_block(hits, limit=300):
    out = []
    for i, h in enumerate(hits, 1):
        out.append("[%d] %s · %s · %s\n    %s" % (
            i, TRUST.get(h["mtype"], "?"), h["org"] or "-", h["date"] or "-",
            (h["body"] or "").replace("\n", " ")[:limit]))
    return "\n\n".join(out)


def verify(answer, hits):
    """★근거 밖으로 나갔는지 코드가 본다. 시켜만 놓고 믿지 않는다."""
    ev = " ".join(h["body"] or "" for h in hits)
    cited = {int(x) for x in re.findall(r"\[(\d+)\]", answer or "")}
    bad_no = sorted(n for n in cited if not 1 <= n <= len(hits))
    nums = lambda t: set(re.findall(r"\d+(?:\.\d+)?", t or ""))       # noqa: E731
    bad_num = sorted(nums(re.sub(r"\[\d+\]", " ", answer or "")) - nums(ev))[:6]
    return [{"check": "출처 번호 실존", "result": "통과" if not bad_no else "위반",
             "detail": str(bad_no or "-")},
            {"check": "출처 표기 있음",
             "result": "통과" if (cited or "찾을 수 없습니다" in (answer or "")) else "위반",
             "detail": "%d개" % len(cited)},
            {"check": "숫자에 근거 있음", "result": "통과" if not bad_num else "위반",
             "detail": str(bad_num or "-")}]


prompt = "[근거]\n%s\n\n[질문]\n%s" % (evidence_block(hits), query)
answer = tools.generate(SYSTEM, prompt, CFG)
check_tbl = tools.table(verify(answer, hits) if answer else [], cut=0)  # ★변수 탐색기

print("지문 %d자 · 그중 근거가 %d%%\n" % (
    len(prompt), 100 * len(evidence_block(hits)) // max(1, len(prompt))))
if answer:
    print("── 답변 ──")
    print("  " + answer.replace("\n", "\n  "))
    print("\n── 확인 ──")
    tools.show(check_tbl)
else:
    print("★생성 모델이 없다. .env 에 LLM_BASE_URL·LLM_MODEL 을 채우면 여기에 답이 나온다.")
    print("  지금은 AI 에 넘길 지문만 보여 준다.\n")
    print(prompt[:900])

print("\n★비용이 드는 곳은 여기 하나다. 그래서 ⑥ 이 잘 골라야 한다.")



# %% ═══════════════════════════════════════════════════════════════
#  ⑨-a 주제축 — 임원이 관심 갖는 테마. **여기만 고치면 화면이 바뀐다**
# ══════════════════════════════════════════════════════════════════
#  축 하나 = 임원 관심 테마 하나
#  그 안에 **규격이 고정된 카테고리** 다섯을 둔다.
#  카테고리가 고정이라 매주 같은 형식으로 읽을 수 있다 — 그게 정기 브리핑이다.

AXES = [
    {"no": 1, "name": "HBM · 고대역폭 메모리", "q": "HBM4 수율과 양산 일정"},
    {"no": 2, "name": "증설 · 투자 판단", "q": "M16 증설 판단 근거와 전환 투자"},
    {"no": 3, "name": "일반 DRAM · 수급", "q": "DRAM 공급 증가율과 가격 전망"},
    {"no": 4, "name": "공정 · 표준", "q": "하이브리드 본딩 접합 기준"},
    {"no": 5, "name": "장비 · 설비투자", "q": "반도체 장비 출하와 설비투자 동향"},
]

# 화면의 행 ← 자료 유형. **행이 곧 카테고리다.**
ROW_OF = {"news": "시장", "official": "시장", "filing": "시장",
          "report": "전망", "research": "전망",
          "external_doc": "기준", "internal_doc": "사내", "mail": "논의"}
ROW_ORDER = ["시장", "전망", "기준", "사내", "논의"]

# ★비었을 때 뭐라고 말할지 — **공백 자체가 정보다**
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

CAT_FLOOR = 0.55     # 그 축 1위 대비 이 비율 미만인 근거는 요약에서 뺀다
CAT_MAX = 4          # 한 카테고리에 최대 몇 건까지 묶을까

print("축 %d개 · 카테고리 %s" % (len(AXES), " · ".join(ROW_ORDER)))
for a in AXES:
    print("  [%d] %-22s ← %s" % (a["no"], a["name"], a["q"]))


# %% ═══════════════════════════════════════════════════════════════
#  ⑨-b 축 하나 만들기 — 하한 → 카테고리 → 요약
# ══════════════════════════════════════════════════════════════════
#  ★왜 하한을 두나
#    빈칸을 남기지 않으려고 무관한 근거로 채우면, 그 위에 만든 요약이
#    **없는 이야기를 지어낸다.** 임원은 그 문장을 그대로 읽는다.
#    실측: HBM 축에 장비 출하 통계(0.47)를 넣으면
#          "기준: 국내 장비 출하가 18% 늘었다" 가 되어 HBM 표준 이야기로 읽힌다.
#
#  ★질의응답은 반대다
#    ⑥ search() 는 유형별 몫을 쓴다. 사용자가 직접 읽고 판단하기 때문이다.

SUMMARY_SYSTEM = """자료를 카테고리별로 한 줄씩 요약하고, 축 전체를 판단합니다.

규칙
1. 아래 [근거] 에 적힌 내용만 씁니다. 없는 사실·수치를 지어내지 않습니다.
2. 카테고리마다 **한 문장**, 40~90자. 여러 건이면 **묶어서** 한 문장으로.
   서로 어긋나는 내용이면 "…인 반면 …" 처럼 **양쪽을 다 적습니다.**
3. `축:` 은 요약이 아니라 **판단**입니다. 다음 순서로 봅니다.
   ⑴ 사내 근거와 외부 근거가 **어긋나는 곳**이 있으면 그것을 먼저 적습니다.
   ⑵ 어긋남이 없으면 **무엇이 관건인지** 한 문장으로 적습니다.
   60~110자. 임원이 읽고 무엇을 결정해야 할지 알 수 있어야 합니다.
4. `변화:` 는 [지난 기록] 이 있을 때만 씁니다. 지난번과 **달라진 점**만 적습니다.
   달라진 것이 없으면 "없음" 이라고만 합니다.
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


_문장 = re.compile(r"(?<=[.!?])\s+|(?<=[다음함임])\.\s+")


def extract(hs, per=2, limit=150):
    """모델 없이 여러 조각을 잇는다 — **발췌**. 지어내지 않는다.

    조각마다 앞 문장 몇 개씩만 골라 이어 붙인다.
    요약은 아니지만 "몇 건을 묶었다" 는 것이 눈에 보인다.
    """
    조각글 = []
    for h in hs:
        본문 = re.sub(r"^\s*##?\s*\S+\s*", "", (h["body"] or "").strip())
        문장들 = [s.strip() for s in _문장.split(본문.replace("\n", " ")) if s.strip()]
        고름 = " ".join(문장들[:per])[:limit]
        if 고름:
            조각글.append("%s: %s" % (h["org"] or "-", 고름))
    return " │ ".join(조각글)


def summarize(groups, last=None):
    """카테고리별 조각 묶음 → {카테고리: 한 줄, "축": …, "변화": …}

    ★축 하나당 모델을 **한 번만** 부른다.
      카테고리마다 따로 부르면 축 5 × 카테고리 5 = 25번이 된다.
    ★축 줄은 카테고리 요약을 다시 요약한 것이 아니다.
      같은 호출에서 **원문 조각**을 보고 만든다 — 두 번 압축하면 충돌을 못 잡는다.
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
    if last:
        프롬프트 += "\n\n[지난 기록]\n" + last
    text = tools.generate(SUMMARY_SYSTEM, 프롬프트, CFG)

    if not text:
        # ★모델이 없을 때 — **원문에서 뽑아 잇는다**(발췌).
        #   전에는 첫 조각만 잘라 써서 나머지 조각이 통째로 버려졌다.
        #   "전망 2건" 인데 화면에는 한 건만 보이니 묶었다는 느낌이 안 났다.
        #   지어내지 않는다 — 원문 문장을 그대로 골라 잇기만 한다.
        out = {k: extract(hs) for k, hs in groups.items()}
        # 축 줄 — 모델이 없으니 판단을 만들 수 없다. **무엇이 있는지만** 알린다.
        있는것 = [k for k in ROW_ORDER if k in out]
        없는것 = [k for k in ROW_ORDER if k not in out]
        out["축"] = "모델이 없어 판단을 만들지 못했습니다. 근거는 %s 에 있고%s." % (
            " · ".join("%s %d건" % (k, len(groups[k])) for k in 있는것),
            (", %s 는 비어 있습니다" % " · ".join(없는것)) if 없는것 else "")
        out["_raw"] = True                        # ★요약이 아니라 원문 발췌다
        return out

    out = {}
    for 줄 in text.splitlines():
        if ":" not in 줄:
            continue
        k, v = 줄.split(":", 1)
        if k.strip() in ROW_ORDER or k.strip() in ("축", "변화"):
            out[k.strip()] = v.strip()
    return out


def compare_lanes(ax, access="전사", k=5):
    """⑥-c 를 화면에서도 볼 수 있게 — 하이브리드를 껐을 때와 켰을 때.

    ★사내 문서로 바꾼 뒤 이것이 가장 쓸모 있다.
      어휘 갈래가 실제로 도움이 되는지, 아니면 오히려 방해가 되는지 바로 보인다.
    ★모델을 부르지 않는다. 검색만 두 번 돈다(축당 약 0.06초).
    """
    벡터만 = search(ax["q"], k=k, access=access, hybrid=False, use_mmr=False)
    하이브리드 = search(ax["q"], k=k, access=access)
    a_ids = [h["id"] for h in 벡터만]
    b_ids = [h["id"] for h in 하이브리드]

    def _줄(hs, other):
        out = []
        for i, h in enumerate(hs, 1):
            was = other.index(h["id"]) + 1 if h["id"] in other else None
            out.append({"rank": i, "was": was,
                        "chip": CHIP.get(h["mtype"], h["mtype"]),
                        "tier": TIER.get(h["mtype"], ""),
                        "org": h["org"] or "-", "score": h["score"],
                        "body": (h["body"] or "").replace("\n", " ")[:110]})
        return out

    바뀐수 = sum(1 for i, x in enumerate(b_ids) if i >= len(a_ids) or a_ids[i] != x)
    새로들어온 = len([x for x in b_ids if x not in a_ids])
    return {"vec": _줄(벡터만, b_ids), "hyb": _줄(하이브리드, a_ids),
            "moved": 바뀐수, "added": 새로들어온}


def build_axis(ax, access="전사", days=14, used=None, last=None, compare=False):
    """축 하나 → 화면 한 장.

    ★해석(뉴스·리포트·기관)은 먼저 잡은 축이 가져간다 — 같은 뉴스가
      다섯 축에 반복되면 임원이 신뢰를 잃는다.
    ★사실(사내문서·공시·기업공식)은 여러 축에서 근거가 되므로 중복을 허용한다.
    """
    hits = search(ax["q"], k=16, access=access)
    if not hits:
        return {**ax, "rows": [], "lead": "이번 기간에 이 축에 걸린 근거가 없습니다.",
                "change": "", "raw": False, "count": 0, "new": 0, "cont": 0,
                "dropped": 0, "dup": 0, "gaps": ROW_ORDER, "empty": True, "move": 0}

    바닥 = hits[0]["score"] * CAT_FLOOR
    used = used if used is not None else set()
    groups, low, dropped, dup = {}, {}, 0, 0

    for h in hits:
        label = ROW_OF.get(h["mtype"], "시장")
        if h["score"] < 바닥:
            # ★버리지 않는다. 요약에 안 넣을 뿐 '관련 낮음' 으로 남긴다.
            dropped += 1
            if len(low.get(label, [])) < 3:
                low.setdefault(label, []).append(h)
            continue
        사실 = h["mtype"] in ("internal_doc", "filing", "official")
        if not 사실 and h["id"] in used:
            dup += 1
            continue
        if len(groups.get(label, [])) >= CAT_MAX:
            continue
        groups.setdefault(label, []).append(h)
        if not 사실:
            used.add(h["id"])

    if not groups:
        return {**ax, "rows": [], "lead": "이번 기간에 볼 만한 근거가 없습니다.",
                "change": "", "raw": False, "count": 0, "new": 0, "cont": 0,
                "dropped": dropped, "dup": dup, "gaps": ROW_ORDER,
                "empty": True, "move": 0}

    summary = summarize(groups, last)
    raw = summary.pop("_raw", False)

    def _srcs(hs):
        return [{"chip": CHIP.get(h["mtype"], h["mtype"]), "tier": TIER.get(h["mtype"], ""),
                 "org": h["org"] or "-", "date": (h["date"] or "")[5:], "score": h["score"],
                 "body": (h["body"] or "").replace("\n", " ")[:200]} for h in hs]

    # ★카테고리 다섯 줄을 **항상** 만든다. 없으면 없다고 말한다.
    #   조용히 빠지면 "이 축에 우리 문서가 없다" 는 사실이 가려진다.
    rows = []
    for label in ROW_ORDER:
        hs, lo = groups.get(label), low.get(label) or []
        if hs:
            rows.append({"label": label, "state": "ok", "n": len(hs), "raw": raw,
                         "text": summary.get(label) or (hs[0]["body"] or "")[:90],
                         "srcs": _srcs(hs), "low": _srcs(lo)})
        elif lo:
            rows.append({"label": label, "state": "low", "n": 0, "raw": False,
                         "text": "요약할 만큼 관련 있는 근거가 없습니다 (관련 낮음 %d건, 최고 %.2f)"
                                 % (len(lo), lo[0]["score"]),
                         "srcs": [], "low": _srcs(lo)})
        else:
            rows.append({"label": label, "state": "none", "n": 0, "raw": False,
                         "text": NONE_TEXT[label], "srcs": [], "low": []})

    쓴것 = sum(len(v) for v in groups.values())
    fresh = sum(1 for v in groups.values() for h in v if is_fresh(h["date"], days))
    비교 = compare_lanes(ax, access) if compare else None
    change = (summary.get("변화") or "").strip()
    change = "" if change in ("없음", "-", "") else change

    # ★변화가 큰 축이 위로 와야 한다. 임원이 스크롤하지 않게.
    move = (100 if change else 0) + fresh * 10 + round(hits[0]["score"] * 10)

    return {**ax, "rows": rows, "lead": summary.get("축") or "판단할 근거가 부족합니다.",
            "change": change, "raw": raw, "count": 쓴것, "new": fresh,
            "cont": 쓴것 - fresh, "dropped": dropped, "dup": dup,
            "gaps": [r["label"] for r in rows if r["state"] != "ok"],
            "compare": 비교, "empty": False, "move": move}


def is_fresh(d, days=14):
    if not d:
        return False
    try:
        from datetime import date
        y, m, dd = map(int, str(d)[:10].split("-"))
        return (date.today() - date(y, m, dd)).days <= days
    except Exception:
        return False


한축 = build_axis(AXES[0], access="제한")

print("[%d] %s   근거 %d · 제외 %d · 중복 %d" % (
    한축["no"], 한축["name"], 한축["count"], 한축["dropped"], 한축["dup"]))
print("  축 판단 : %s%s" % (한축["lead"][:76], "  ★요약 아님" if 한축["raw"] else ""))
print()
for r in 한축["rows"]:
    표시 = {"ok": "  ", "low": "▸ ", "none": "· "}[r["state"]]
    print("  %s%-4s %-5s %s" % (표시, r["label"], "%d건" % r["n"] if r["n"] else "  -",
                               r["text"][:56]))
print("\n  ▸ = 관련 낮음(요약엔 안 넣지만 버리지 않음) · · = 그 유형 자료가 없음")


# %% ═══════════════════════════════════════════════════════════════
#  ⑨-c 지난 기록과 대조 — **무엇이 달라졌나**
# ══════════════════════════════════════════════════════════════════
#  ★마켓 센싱의 핵심은 "지금 무엇이 있나" 가 아니라 "무엇이 달라졌나" 다.
#    축 요약을 매번 남겨 두고, 다음 번에 함께 넘겨 비교하게 한다.
#    같은 호출에 얹으므로 **회차가 늘어도 모델 호출은 축당 1번**이다.

conn.executescript("""
CREATE TABLE IF NOT EXISTS briefs(
  id TEXT PRIMARY KEY, made TEXT, axis INTEGER, name TEXT,
  access TEXT, lead TEXT, cats TEXT);
CREATE INDEX IF NOT EXISTS briefs_axis ON briefs(axis, made);
""")
conn.commit()


def last_record(axis_no, access):
    """이 축의 **직전 기록**. 없으면 None."""
    r = conn.execute("SELECT made, lead, cats FROM briefs WHERE axis=? AND access=?"
                     " ORDER BY made DESC LIMIT 1", (axis_no, access)).fetchone()
    if not r:
        return None
    cats = json.loads(r["cats"] or "{}")
    return "\n".join(["(%s 기록)" % r["made"][:10], "축: %s" % r["lead"]]
                     + ["%s: %s" % (k, v) for k, v in cats.items()])


def save_record(ax, access):
    """이번 결과를 남긴다. 같은 날 같은 축은 덮어쓴다."""
    made = time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute("INSERT OR REPLACE INTO briefs(id,made,axis,name,access,lead,cats)"
                 " VALUES(?,?,?,?,?,?,?)",
                 ("%s|%d|%s" % (made[:10], ax["no"], access), made, ax["no"],
                  ax["name"], access, ax["lead"],
                  json.dumps({r["label"]: r["text"] for r in ax["rows"]},
                             ensure_ascii=False)))
    conn.commit()


def build_brief(access="전사", days=14, save=True, compare=False):
    """축 전부 → 브리핑 한 장. **변화가 큰 축이 위로.**"""
    t0 = time.time()
    used = set()                                  # 해석 근거를 축끼리 나눠 갖게
    axes = []
    for a in AXES:
        ax = build_axis(a, access, days, used=used,
                        last=last_record(a["no"], access), compare=compare)
        axes.append(ax)
        if save and not ax["empty"]:
            save_record(ax, access)

    axes.sort(key=lambda x: -x["move"])
    for i, ax in enumerate(axes, 1):
        ax["rank"] = i

    tot = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    inside = conn.execute("SELECT COUNT(*) FROM docs WHERE mtype IN"
                          " ('internal_doc','mail')").fetchone()[0]
    new_docs = sum(1 for r in conn.execute("SELECT date FROM docs")
                   if is_fresh(r["date"], days))
    model = conn.execute("SELECT model FROM chunks WHERE vec IS NOT NULL LIMIT 1").fetchone()
    회차 = conn.execute("SELECT COUNT(DISTINCT substr(made,1,10)) FROM briefs"
                      " WHERE access=?", (access,)).fetchone()[0]
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "next": (datetime.date.today() + datetime.timedelta(days=7)).isoformat(),
        "period": "%s ~ %s" % ((datetime.date.today()
                                - datetime.timedelta(days=days)).isoformat(),
                               datetime.date.today().isoformat()),
        "access": access, "axes": axes, "runs": 회차,
        "changed": [a["name"] for a in axes if a["change"]],
        "no_inside": [a["name"] for a in axes
                      if not a["empty"] and "사내" in a["gaps"]],
        "empty": [a["name"] for a in axes if a["empty"]],
        "kpi": {"new": new_docs, "cont": tot - new_docs,
                "axes": len([a for a in axes if not a["empty"]]),
                "docs": tot, "inside": inside, "outside": tot - inside},
        "model": (model["model"] if model else "-"),
        "live": bool(CFG.get("EMBED_BASE_URL") and CFG.get("EMBED_MODEL")),
        "llm": bool(CFG.get("LLM_BASE_URL") and CFG.get("LLM_MODEL")),
        "ms": int((time.time() - t0) * 1000),
    }


브리핑 = build_brief(access="부서")
축표 = tools.table([{k: a[k] for k in
                   ("rank", "no", "name", "count", "dropped", "dup", "new", "move")}
                  for a in 브리핑["axes"]], cut=0)          # ★변수 탐색기

print("회차 %d · %dms" % (브리핑["runs"], 브리핑["ms"]))
print("  달라진 축      : %s" % (브리핑["changed"] or "없음 (기록이 쌓이면 뜬다)"))
print("  사내 문서 없는 축: %s" % (브리핑["no_inside"] or "없음"))
print()
tools.show(축표)
print("\n★이 셀을 한 번 더 돌려 보라. 두 번째부터 '변화' 가 잡힌다(모델이 있을 때).")


# %% ═══════════════════════════════════════════════════════════════
#  ⑨-d 화면으로 보기
# ══════════════════════════════════════════════════════════════════
#  터미널에서 —
#      python web/server.py     →  http://127.0.0.1:8700
#
#  ★서버는 이 노트북을 그대로 불러 위 함수들을 쓴다.
#    여기서 AXES·CAT_FLOOR·SUMMARY_SYSTEM 을 고치면 화면에도 그대로 반영된다.
#    로직이 두 곳에 있지 않다.

for a in 브리핑["axes"]:
    표시 = "★" if a["change"] else " "
    print("%s %d위 [%d] %-22s 근거%2d 제외%2d" % (
        표시, a["rank"], a["no"], a["name"][:22], a["count"], a["dropped"]))
    print("      %s" % a["lead"][:80])
    보임 = [r for r in a["rows"] if r["state"] == "ok" or r["label"] == "사내"]
    접힘 = [r["label"] for r in a["rows"] if r not in 보임]
    for r in 보임:
        print("        %-4s %s" % (r["label"], r["text"][:66]))
    if 접힘:
        print("        (접힘) %s 근거 없음" % " · ".join(접힘))
    print()

# %% ═══════════════════════════════════════════════════════════════
#  ⑧ run_once — 새 문서가 들어오면 이 셀 하나
# ══════════════════════════════════════════════════════════════════
#  **이미 한 일은 건너뛴다.** 새 문서·고쳐진 문서만 처리되므로
#  매일 돌려도 안전하다.

def run_once():
    """collect → raw → normalize → chunk → embed. 새 것만."""
    t0 = time.time()
    items, skipped = collect()
    saved = save_raw(items)
    docs, failed = normalize()
    chunks = chunk()
    vectors = embed_all()
    return {"new_raw": saved["new"], "changed_raw": saved["changed"],
            "new_docs": len(docs), "new_chunks": len(chunks),
            "new_vectors": vectors["made"], "failed": len(failed),
            "unreadable": len(skipped), "seconds": round(time.time() - t0, 1)}


print("run_once :", json.dumps(run_once(), ensure_ascii=False))
print("★두 번째부터는 전부 0이다. 새 문서를 문서 폴더에 넣고 다시 돌려 보라.\n")
tools.show(tools.status(conn))


# %% ═══════════════════════════════════════════════════════════════
#  reset — 규칙(정규화·조각)을 고쳤을 때만
# ══════════════════════════════════════════════════════════════════
def reset():
    global conn
    conn.close()
    DB_PATH.unlink(missing_ok=True)
    conn = tools.open_db(DB_PATH)
    print("지웠다. ① 부터 다시 돌리거나 run_once() 를 부른다.")


# reset(); print(run_once())
