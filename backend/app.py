
import hashlib, os, secrets, sqlite3
from contextlib import contextmanager
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

import srcparts_gate as gate

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def connect_ro():
    """只读连接：分段齐套核证接口专用，写操作会被 SQLite 拒绝。"""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def readonly():
    """读路径统一用 auth 的只读连接，且必定关闭。"""
    conn = connect_ro()
    try:
        yield conn
    finally:
        conn.close()

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

CONFIGURED_ARCH = os.environ.get("KEYREMAP_SRC_PART_ARCH", gate.ARCH_COMPLETE)

def init_db():
    conn = connect()
    try:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        """)
        # 先补列，再装触发器：触发器体引用 part_state / ingest_id，必须在列存在后创建。
        gate.ensure_column(conn, "formal_entities", "ingest_id", "TEXT")
        gate.ensure_column(conn, "formal_entities", "part_state", "TEXT NOT NULL DEFAULT '齐套'")
        gate.init_schema(conn)
        # 部署级单选：口径只能初始化一次，之后任何不一致都直接拒绝启动。
        gate.ensure_arch(conn, CONFIGURED_ARCH)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
                ("m-2026.11","T1::OLD-B3","NEW-300","一对一"),
                ("m-2026.11","OLD-B3","NEW-400","一对一"),
            ])
            # 种子正式行同样带分段证据与互证桶，保证核证不出现“未入桶”噪声。
            gate.create_ingest(conn, "ing-seed", "T0", "OLD-A1", "2026-11-10T09:00:00")
            conn.execute("UPDATE src_inbound SET status='已重映' WHERE ingest_id='ing-seed'")
            cur = conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at,ingest_id,part_state) VALUES (?,?,?,?,?,?)",
                               ("NEW-100","T0::OLD-A1","m-2026.11","2026-11-10T09:00:00","ing-seed","齐套"))
            conn.execute("INSERT INTO proof_bucket(formal_id,ingest_id,bucket,canon_key,added_at) VALUES (?,?,?,?,?)",
                         (cur.lastrowid, "ing-seed", "齐套", "T0::OLD-A1", "2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))
        conn.commit()
    finally:
        conn.close()

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with readonly() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
        arch = gate.get_arch(c)
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n,
            "db": str(DB_PATH), "src_part_arch": arch,
            "src_part_arch_label": gate.ARCH_LABELS[arch],
            "src_part_evidence": gate.EVIDENCE_FORM}

@app.post("/api/login")
def login(body: LoginIn):
    with readonly() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with readonly() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with readonly() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class IngestIn(BaseModel):
    tenant_part: str | None = None
    biz_part: str | None = None
    ingest_id: str | None = None

class CompleteIn(BaseModel):
    tenant_part: str | None = None
    biz_part: str | None = None
    expected_rev: int

def _run_gate(route: str, fn) -> dict:
    """所有重映/补段写入共用：BEGIN IMMEDIATE 串行化 + 单一留痕 + 原子提交。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = fn(conn)
        gate.log_trace(conn, route, result["payload"].get("ingest_id"),
                       result["outcome"], result["payload"].get("detail", ""))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _reject(result: dict):
    if result["status"] != 200:
        raise HTTPException(result["status"], result["payload"])

@app.post("/api/src-parts/ingest")
def src_parts_ingest(body: IngestIn, authorization: str | None = Header(default=None)):
    """入站装载台：租户段与业务段分开上传，服务端生成 canon_old 并落证据。"""
    require_user(authorization)
    ingest_id = (body.ingest_id or "").strip() or "ing-" + secrets.token_hex(6)
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if gate.get_inbound(conn, ingest_id) is not None:
            raise HTTPException(409, "ingest_id 已存在，禁止覆盖入站证据")
        gate.create_ingest(conn, ingest_id, body.tenant_part, body.biz_part, now)
        state = gate.ingest_state(conn, ingest_id)
        conn.commit()
        return state
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

@app.get("/api/src-parts/ingest/{ingest_id}")
def src_parts_verify(ingest_id: str, authorization: str | None = Header(default=None)):
    """分段齐套核证，只读：使用 mode=ro 连接，路径与参数均不携带口径。"""
    require_user(authorization)
    conn = connect_ro()
    try:
        snapshot = gate.verify_snapshot(conn, ingest_id)
    finally:
        conn.close()
    if snapshot is None:
        raise HTTPException(404, "入站行不存在")
    return snapshot

@app.get("/api/src-parts/inbound")
def src_parts_inbound(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with readonly() as c:
        items = []
        for row in c.execute("SELECT ingest_id FROM src_inbound ORDER BY created_at DESC, rowid DESC").fetchall():
            items.append(gate.ingest_state(c, row["ingest_id"]))
    return {"items": items}

@app.post("/api/src-parts/complete")
def src_parts_complete(ingest_id: str, body: CompleteIn,
                       authorization: str | None = Header(default=None)):
    """补段写入：与重映共用写锁，乐观 rev 冲突可复现。"""
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    result = _run_gate("complete", lambda conn: gate.complete_parts(
        conn, ingest_id, body.tenant_part, body.biz_part, body.expected_rev, now))
    _reject(result)
    return result["payload"]

@app.post("/api/src-parts/remap")
def src_parts_remap(ingest_id: str, authorization: str | None = Header(default=None)):
    """重映闸：严格按部署选定的单一正确性口径出键。"""
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    result = _run_gate("remap", lambda conn: gate.remap_gate(conn, ingest_id, now))
    _reject(result)
    return result["payload"]

@app.get("/api/src-parts/summary")
def src_parts_summary(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with readonly() as c:
        arch = gate.get_arch(c)
        counts = {
            "formal_total": c.execute("SELECT COUNT(*) FROM formal_entities").fetchone()[0],
            "complete_bucket": c.execute("SELECT COUNT(*) FROM proof_bucket WHERE bucket='齐套'").fetchone()[0],
            "degraded_bucket": c.execute("SELECT COUNT(*) FROM proof_bucket WHERE bucket='缺段'").fetchone()[0],
            "missing_marks": c.execute("SELECT COUNT(*) FROM missing_part_marks").fetchone()[0],
            "inbound_waiting_parts": c.execute("SELECT COUNT(*) FROM src_inbound WHERE status='待补段'").fetchone()[0],
        }
        integrity = {
            "degraded_in_complete_bucket": c.execute(
                "SELECT COUNT(*) FROM proof_bucket b JOIN formal_entities f ON f.id=b.formal_id "
                "WHERE b.bucket='齐套' AND f.part_state<>'齐套'").fetchone()[0],
            "unmarked_degraded_formal": c.execute(
                "SELECT COUNT(*) FROM formal_entities f WHERE f.part_state='缺段' "
                "AND NOT EXISTS (SELECT 1 FROM missing_part_marks m WHERE m.formal_id=f.id)").fetchone()[0],
        }
        marks = [dict(r) for r in c.execute("SELECT * FROM missing_part_marks ORDER BY id DESC").fetchall()]
        trace = [dict(r) for r in c.execute("SELECT * FROM write_trace ORDER BY id DESC LIMIT 20").fetchall()]
    return {"arch": arch, "arch_label": gate.ARCH_LABELS[arch],
            "criterion": gate.ARCH_CRITERIA[arch],
            "evidence_form": gate.EVIDENCE_FORM,
            "counts": counts, "integrity": integrity, "marks": marks, "trace": trace}

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with readonly() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with readonly() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
