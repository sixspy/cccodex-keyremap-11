"""源键分段闸（srcparts_gate）。

本模块负责“租户号 / 业务号”拆段入站、分段证据落库、齐套核证以及重映闸的单一
正确性口径。原先这里是空桩，现按下列约定落地：

证据形态（二选一，本仓选定第一种）
------------------------------------------------
``src_part_ledger`` —— 只追加（append-only）的分段事件表，列为
``ingest_id + tenant_part + biz_part + canon_old``。补段不是改写证据，而是追加
一条新的分段事件；表上装有禁止 UPDATE / DELETE 的触发器。
未采用“入站行内嵌 ``src_parts_blob`` 不可变列”的第二种形态。

全局正确性口径（二选一，部署级单选）
------------------------------------------------
``complete``（甲 / 分段齐套）
    租户段与业务段必须齐套才可出正式键；任一段缺失 → 整笔拒绝重映，正式区零
    新增，入站行保留为“待补段”。
``degrade``（乙 / 主段降级）
    允许仅凭主段 ``biz_part`` 出键，但必须在**同一事务**里写入
    ``missing_part_marks`` 缺段标记，并把该笔互证剔出“齐套”桶、落入“缺段”桶。
    落键、标记、入桶任一步失败 → 三者一并回滚。

口径写死在 ``app_settings.src_part_arch``：启动时与
``KEYREMAP_SRC_PART_ARCH`` 交叉校验，已存在的库不允许热切换口径，运行期任何
请求也不得携带口径参数。两种写法在同一个部署内不得混用。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

ARCH_COMPLETE = "complete"
ARCH_DEGRADE = "degrade"
ARCHES = (ARCH_COMPLETE, ARCH_DEGRADE)

ARCH_LABELS = {
    ARCH_COMPLETE: "甲 / 分段齐套",
    ARCH_DEGRADE: "乙 / 主段降级",
}
ARCH_CRITERIA = {
    ARCH_COMPLETE: "任一分段为空则整笔拒绝重映，正式区零增，入站留待补段",
    ARCH_DEGRADE: "允许仅 biz_part 出键，同事务写缺段标记并剔出齐套桶",
}
CANON_SEP = "::"
EVIDENCE_FORM = "src_part_ledger"

BUCKET_COMPLETE = "齐套"
BUCKET_DEGRADED = "缺段"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm_part(value) -> str | None:
    """空串 / 纯空白 / None 一律视为“缺段”，避免页面临时拼接蒙混。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def canon_from_parts(tenant_part: str | None, biz_part: str | None) -> str | None:
    """规范旧键只由服务端从两段生成；页面上传的是分段，不是拼好的键。"""
    tenant = norm_part(tenant_part)
    biz = norm_part(biz_part)
    if tenant is None or biz is None:
        return None
    return f"{tenant}{CANON_SEP}{biz}"


def parts_state(tenant_part, biz_part) -> tuple[bool, list[str]]:
    missing: list[str] = []
    if norm_part(tenant_part) is None:
        missing.append("tenant_part")
    if norm_part(biz_part) is None:
        missing.append("biz_part")
    return (not missing, missing)


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_arch(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT v FROM app_settings WHERE k='src_part_arch'").fetchone()
    if not row:
        raise RuntimeError("源键分段口径未初始化（app_settings.src_part_arch 缺失）")
    arch = row["v"]
    if arch not in ARCHES:
        raise RuntimeError(f"未知的源键分段口径：{arch}")
    return arch


def ensure_arch(conn: sqlite3.Connection, arch: str) -> str:
    """口径是部署级常量。库里已有口径时只允许一致，禁止运行期切换造成混用。"""
    if arch not in ARCHES:
        raise RuntimeError(f"未知的源键分段口径：{arch}")
    row = conn.execute("SELECT v FROM app_settings WHERE k='src_part_arch'").fetchone()
    if row is None:
        conn.execute("INSERT INTO app_settings(k,v) VALUES ('src_part_arch',?)", (arch,))
        return arch
    if row["v"] != arch:
        raise RuntimeError(
            "源键分段口径冲突：库内=%s，当前部署=%s。"
            "全局只贯彻一种口径，禁止同一库混用；如需换口径请新建库。" % (row["v"], arch)
        )
    return arch


def init_schema(conn: sqlite3.Connection) -> None:
    """分段证据、入站台、缺段标记、互证桶、争用留痕与硬约束触发器。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS app_settings(
          k TEXT PRIMARY KEY,
          v TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS src_inbound(
          ingest_id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          rev INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS src_part_ledger(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ingest_id TEXT NOT NULL,
          seq INTEGER NOT NULL,
          tenant_part TEXT,
          biz_part TEXT,
          canon_old TEXT,
          source TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE(ingest_id, seq)
        );
        CREATE INDEX IF NOT EXISTS idx_src_part_ledger_ingest
          ON src_part_ledger(ingest_id, seq);
        CREATE TABLE IF NOT EXISTS missing_part_marks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ingest_id TEXT NOT NULL UNIQUE,
          formal_id INTEGER NOT NULL,
          missing_part TEXT NOT NULL,
          canon_used TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS proof_bucket(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          formal_id INTEGER NOT NULL UNIQUE,
          ingest_id TEXT NOT NULL,
          bucket TEXT NOT NULL,
          canon_key TEXT,
          added_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS write_trace(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          route TEXT NOT NULL,
          ingest_id TEXT,
          outcome TEXT NOT NULL,
          detail TEXT,
          at TEXT NOT NULL
        );

        /* 分段证据只追加：补段必须 append 新事件，不得改写历史。 */
        CREATE TRIGGER IF NOT EXISTS trg_src_part_ledger_no_update
        BEFORE UPDATE ON src_part_ledger
        BEGIN SELECT RAISE(ABORT, 'src_part_ledger 为只追加证据，禁止 UPDATE'); END;
        CREATE TRIGGER IF NOT EXISTS trg_src_part_ledger_no_delete
        BEFORE DELETE ON src_part_ledger
        BEGIN SELECT RAISE(ABORT, 'src_part_ledger 为只追加证据，禁止 DELETE'); END;

        /* 齐套架构下缺段仍出键 = 硬失败。 */
        CREATE TRIGGER IF NOT EXISTS trg_formal_no_degraded_in_complete
        BEFORE INSERT ON formal_entities
        WHEN NEW.part_state = '缺段'
          AND (SELECT v FROM app_settings WHERE k='src_part_arch') = 'complete'
        BEGIN SELECT RAISE(ABORT, '齐套架构禁止缺段出键'); END;

        /* 齐套正式行必须能对上一条齐套的源键分段证据。 */
        CREATE TRIGGER IF NOT EXISTS trg_formal_complete_needs_parts
        BEFORE INSERT ON formal_entities
        WHEN NEW.part_state = '齐套' AND NEW.ingest_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM src_part_ledger l
            WHERE l.ingest_id = NEW.ingest_id
              AND l.id = (SELECT MAX(id) FROM src_part_ledger WHERE ingest_id = NEW.ingest_id)
              AND l.tenant_part IS NOT NULL AND TRIM(l.tenant_part) <> ''
              AND l.biz_part IS NOT NULL AND TRIM(l.biz_part) <> ''
          )
        BEGIN SELECT RAISE(ABORT, '齐套正式行缺少齐套分段证据'); END;

        /* 缺段标记只能挂缺段正式行。 */
        CREATE TRIGGER IF NOT EXISTS trg_mark_needs_degraded_formal
        BEFORE INSERT ON missing_part_marks
        WHEN NOT EXISTS (
          SELECT 1 FROM formal_entities
          WHERE id = NEW.formal_id AND part_state = '缺段'
        )
        BEGIN SELECT RAISE(ABORT, '缺段标记必须挂缺段正式行'); END;

        /* 齐套桶兜底：缺段不允许混进齐套正式计数。 */
        CREATE TRIGGER IF NOT EXISTS trg_bucket_complete_needs_parts
        BEFORE INSERT ON proof_bucket
        WHEN NEW.bucket = '齐套' AND NOT EXISTS (
          SELECT 1 FROM src_part_ledger l
          WHERE l.ingest_id = NEW.ingest_id
            AND l.id = (SELECT MAX(id) FROM src_part_ledger WHERE ingest_id = NEW.ingest_id)
            AND l.tenant_part IS NOT NULL AND TRIM(l.tenant_part) <> ''
            AND l.biz_part IS NOT NULL AND TRIM(l.biz_part) <> ''
        )
        BEGIN SELECT RAISE(ABORT, '齐套桶拒绝缺段入桶'); END;

        /* 缺段桶兜底：没有缺段标记不允许入缺段桶。 */
        CREATE TRIGGER IF NOT EXISTS trg_bucket_degraded_needs_mark
        BEFORE INSERT ON proof_bucket
        WHEN NEW.bucket = '缺段' AND NOT EXISTS (
          SELECT 1 FROM missing_part_marks WHERE ingest_id = NEW.ingest_id
        )
        BEGIN SELECT RAISE(ABORT, '缺段桶必须先写缺段标记'); END;
        """
    )


def log_trace(conn: sqlite3.Connection, route: str, ingest_id: str | None,
              outcome: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO write_trace(route,ingest_id,outcome,detail,at) VALUES (?,?,?,?,?)",
        (route, ingest_id, outcome, detail, now_iso()),
    )


def get_inbound(conn: sqlite3.Connection, ingest_id: str):
    return conn.execute(
        "SELECT * FROM src_inbound WHERE ingest_id=?", (ingest_id,)
    ).fetchone()


def latest_ledger(conn: sqlite3.Connection, ingest_id: str):
    return conn.execute(
        "SELECT * FROM src_part_ledger WHERE ingest_id=? ORDER BY seq DESC, id DESC LIMIT 1",
        (ingest_id,),
    ).fetchone()


def append_ledger(conn: sqlite3.Connection, ingest_id: str, seq: int,
                  tenant_part, biz_part, source: str, created_at: str) -> None:
    tenant = norm_part(tenant_part)
    biz = norm_part(biz_part)
    conn.execute(
        """INSERT INTO src_part_ledger
           (ingest_id,seq,tenant_part,biz_part,canon_old,source,created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (ingest_id, seq, tenant, biz, canon_from_parts(tenant, biz), source, created_at),
    )


def create_ingest(conn: sqlite3.Connection, ingest_id: str,
                  tenant_part, biz_part, created_at: str) -> None:
    tenant = norm_part(tenant_part)
    biz = norm_part(biz_part)
    complete, _ = parts_state(tenant, biz)
    status = "待重映" if complete else "待补段"
    conn.execute(
        "INSERT INTO src_inbound(ingest_id,status,rev,created_at,updated_at) VALUES (?,?,1,?,?)",
        (ingest_id, status, created_at, created_at),
    )
    append_ledger(conn, ingest_id, 1, tenant, biz, "ingest", created_at)
    log_trace(conn, "ingest", ingest_id, "ok",
              "segments=%s" % ("齐套" if complete else "缺段"))


def ingest_state(conn: sqlite3.Connection, ingest_id: str) -> dict:
    inbound = get_inbound(conn, ingest_id)
    ledger = latest_ledger(conn, ingest_id)
    tenant = ledger["tenant_part"] if ledger else None
    biz = ledger["biz_part"] if ledger else None
    complete, missing = parts_state(tenant, biz)
    return {
        "ingest_id": ingest_id,
        "status": inbound["status"],
        "rev": inbound["rev"],
        "tenant_part": tenant,
        "biz_part": biz,
        "canon_old": canon_from_parts(tenant, biz),
        "complete": complete,
        "missing": missing,
        "evidence_form": EVIDENCE_FORM,
        "ledger_seq": ledger["seq"] if ledger else None,
        "updated_at": inbound["updated_at"],
    }


def active_version(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def lookup_mapping(conn: sqlite3.Connection, version: str, key: str):
    return conn.execute(
        "SELECT * FROM mappings WHERE version=? AND old_key=? ORDER BY id LIMIT 1",
        (version, key),
    ).fetchone()


def gate_result(status: int, outcome: str, payload: dict) -> dict:
    return {"status": status, "outcome": outcome, "payload": payload}


def remap_gate(conn: sqlite3.Connection, ingest_id: str, at: str) -> dict:
    """重映闸：在已持有 BEGIN IMMEDIATE 写锁的事务内执行。

    返回值由调用方负责留痕并提交；业务性拒绝也返回结果而不抛异常，保证入站状态
    与争用/拒绝留痕能随事务落库，同时正式区“整笔零增”。
    """
    arch = get_arch(conn)
    inbound = get_inbound(conn, ingest_id)
    if inbound is None:
        return gate_result(404, "not_found", {"detail": "入站行不存在", "ingest_id": ingest_id})
    if inbound["status"] == "已重映":
        return gate_result(409, "rejected", {"detail": "该入站行已重映，禁止重复出键", "ingest_id": ingest_id})

    ledger = latest_ledger(conn, ingest_id)
    tenant = ledger["tenant_part"] if ledger else None
    biz = ledger["biz_part"] if ledger else None
    complete, missing = parts_state(tenant, biz)
    base = {
        "ingest_id": ingest_id,
        "arch": arch,
        "arch_label": ARCH_LABELS[arch],
        "tenant_part": tenant,
        "biz_part": biz,
        "canon_old": canon_from_parts(tenant, biz),
        "complete": complete,
        "missing": missing,
    }

    if arch == ARCH_COMPLETE:
        if not complete:
            conn.execute(
                "UPDATE src_inbound SET status='待补段', updated_at=? WHERE ingest_id=?",
                (at, ingest_id),
            )
            base.update({
                "detail": "缺段：整笔拒绝重映，正式区零增，入站留待补段",
                "formal_inserted": 0,
            })
            return gate_result(422, "rejected_missing", base)
        lookup_key = base["canon_old"]
        part_state = BUCKET_COMPLETE
    else:  # ARCH_DEGRADE
        if biz is None:
            conn.execute(
                "UPDATE src_inbound SET status='待补段', updated_at=? WHERE ingest_id=?",
                (at, ingest_id),
            )
            base.update({
                "detail": "主段（biz_part）缺失，无法降级出键，入站留待补段",
                "formal_inserted": 0,
            })
            return gate_result(422, "rejected_missing", base)
        lookup_key = base["canon_old"] if complete else biz
        part_state = BUCKET_COMPLETE if complete else BUCKET_DEGRADED

    version_row = active_version(conn)
    if version_row is None:
        base.update({"detail": "无生效映射版本", "formal_inserted": 0})
        return gate_result(404, "rejected", base)
    version = version_row["version"]
    mapping = lookup_mapping(conn, version, lookup_key)
    if mapping is None:
        conn.execute(
            "INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)",
            (lookup_key, "无映射", "挂起"),
        )
        conn.execute(
            "UPDATE src_inbound SET status='冲突', updated_at=? WHERE ingest_id=?",
            (at, ingest_id),
        )
        base.update({
            "detail": "无映射，已进冲突队列",
            "lookup_key": lookup_key,
            "formal_inserted": 0,
        })
        return gate_result(409, "conflict", base)

    cur = conn.execute(
        """INSERT INTO formal_entities
           (entity_key,mapped_from,version,created_at,ingest_id,part_state)
           VALUES (?,?,?,?,?,?)""",
        (mapping["new_key"], lookup_key, version, at, ingest_id, part_state),
    )
    formal_id = cur.lastrowid

    mark_id = None
    if part_state == BUCKET_DEGRADED:
        missing_part = missing[0] if missing else "tenant_part"
        mark_cur = conn.execute(
            """INSERT INTO missing_part_marks
               (ingest_id,formal_id,missing_part,canon_used,created_at)
               VALUES (?,?,?,?,?)""",
            (ingest_id, formal_id, missing_part, lookup_key, at),
        )
        mark_id = mark_cur.lastrowid

    conn.execute(
        """INSERT INTO proof_bucket(formal_id,ingest_id,bucket,canon_key,added_at)
           VALUES (?,?,?,?,?)""",
        (formal_id, ingest_id, part_state, lookup_key, at),
    )
    conn.execute(
        "UPDATE src_inbound SET status='已重映', updated_at=? WHERE ingest_id=?",
        (at, ingest_id),
    )

    base.update({
        "ok": True,
        "entity_key": mapping["new_key"],
        "mapping_version": version,
        "mapping_kind": mapping["kind"],
        "lookup_key": lookup_key,
        "part_state": part_state,
        "bucket": part_state,
        "formal_id": formal_id,
        "missing_mark_id": mark_id,
        "formal_inserted": 1,
    })
    return gate_result(200, "ok", base)


def complete_parts(conn: sqlite3.Connection, ingest_id: str,
                   tenant_part, biz_part, expected_rev: int, at: str) -> dict:
    """补段：乐观并发 + BEGIN IMMEDIATE 写锁，拒绝不落锁的 ledger 改写。"""
    inbound = get_inbound(conn, ingest_id)
    if inbound is None:
        return gate_result(404, "not_found", {"detail": "入站行不存在", "ingest_id": ingest_id})
    if inbound["status"] == "已重映":
        return gate_result(409, "rejected", {"detail": "已重映的入站行不可再补段", "ingest_id": ingest_id})
    if int(expected_rev) != int(inbound["rev"]):
        return gate_result(409, "contention", {
            "detail": "补段争用：版本已变化，请刷新后重试",
            "ingest_id": ingest_id,
            "expected_rev": int(expected_rev),
            "rev": int(inbound["rev"]),
        })

    ledger = latest_ledger(conn, ingest_id)
    old_tenant = ledger["tenant_part"] if ledger else None
    old_biz = ledger["biz_part"] if ledger else None
    new_tenant = norm_part(tenant_part)
    new_biz = norm_part(biz_part)

    if new_tenant is not None and old_tenant is not None and new_tenant != old_tenant:
        return gate_result(409, "conflict", {
            "detail": "租户段与既有分段冲突，证据只可补空不可改写",
            "ingest_id": ingest_id,
            "existing_tenant_part": old_tenant,
        })
    if new_biz is not None and old_biz is not None and new_biz != old_biz:
        return gate_result(409, "conflict", {
            "detail": "业务段与既有分段冲突，证据只可补空不可改写",
            "ingest_id": ingest_id,
            "existing_biz_part": old_biz,
        })

    merged_tenant = old_tenant if old_tenant is not None else new_tenant
    merged_biz = old_biz if old_biz is not None else new_biz
    if merged_tenant == old_tenant and merged_biz == old_biz:
        return gate_result(400, "rejected", {"detail": "补段未提供任何新分段", "ingest_id": ingest_id})

    next_rev = int(inbound["rev"]) + 1
    complete, _ = parts_state(merged_tenant, merged_biz)
    status = "待重映" if complete else "待补段"
    conn.execute(
        "UPDATE src_inbound SET status=?, rev=?, updated_at=? WHERE ingest_id=?",
        (status, next_rev, at, ingest_id),
    )
    append_ledger(conn, ingest_id, next_rev, merged_tenant, merged_biz, "complete", at)
    state = ingest_state(conn, ingest_id)
    state["expected_rev"] = int(expected_rev)
    return gate_result(200, "ok", state)


def _count(conn: sqlite3.Connection, sql: str, params=()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def verify_snapshot(conn: sqlite3.Connection, ingest_id: str) -> dict | None:
    """分段齐套只读核证：只在只读连接上执行，不写任何表。"""
    inbound = get_inbound(conn, ingest_id)
    if inbound is None:
        return None
    arch = get_arch(conn)
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM src_part_ledger WHERE ingest_id=? ORDER BY seq, id", (ingest_id,)
    ).fetchall()]
    ledger = latest_ledger(conn, ingest_id)
    tenant = ledger["tenant_part"] if ledger else None
    biz = ledger["biz_part"] if ledger else None
    complete, missing = parts_state(tenant, biz)
    canon = canon_from_parts(tenant, biz)
    lookup_key = canon if complete else (biz if biz else None)

    version_row = active_version(conn)
    mapping = None
    if version_row and lookup_key:
        row = lookup_mapping(conn, version_row["version"], lookup_key)
        if row:
            mapping = {
                "found": True,
                "version": row["version"],
                "old_key": row["old_key"],
                "new_key": row["new_key"],
                "kind": row["kind"],
            }
    if mapping is None and lookup_key:
        mapping = {"found": False, "lookup_key": lookup_key,
                   "version": version_row["version"] if version_row else None}

    formals = [dict(r) for r in conn.execute(
        "SELECT * FROM formal_entities WHERE ingest_id=? ORDER BY id", (ingest_id,)
    ).fetchall()]
    bucket = conn.execute(
        "SELECT * FROM proof_bucket WHERE ingest_id=? ORDER BY id DESC LIMIT 1", (ingest_id,)
    ).fetchone()
    marks = [dict(r) for r in conn.execute(
        "SELECT * FROM missing_part_marks WHERE ingest_id=? ORDER BY id", (ingest_id,)
    ).fetchall()]

    counts = {
        "formal_total": _count(conn, "SELECT COUNT(*) FROM formal_entities"),
        "complete_bucket": _count(conn, "SELECT COUNT(*) FROM proof_bucket WHERE bucket='齐套'"),
        "degraded_bucket": _count(conn, "SELECT COUNT(*) FROM proof_bucket WHERE bucket='缺段'"),
        "missing_marks": _count(conn, "SELECT COUNT(*) FROM missing_part_marks"),
        "unbucketed_formal": _count(
            conn,
            "SELECT COUNT(*) FROM formal_entities f "
            "LEFT JOIN proof_bucket b ON b.formal_id=f.id WHERE b.id IS NULL",
        ),
    }
    integrity = {
        # 以下三项必须恒为 0，任一非 0 即证明口径被混用。
        "degraded_in_complete_bucket": _count(
            conn,
            "SELECT COUNT(*) FROM proof_bucket b JOIN formal_entities f ON f.id=b.formal_id "
            "WHERE b.bucket='齐套' AND f.part_state<>'齐套'",
        ),
        "unmarked_degraded_formal": _count(
            conn,
            "SELECT COUNT(*) FROM formal_entities f WHERE f.part_state='缺段' "
            "AND NOT EXISTS (SELECT 1 FROM missing_part_marks m WHERE m.formal_id=f.id)",
        ),
        "complete_bucket_without_complete_ledger": _count(
            conn,
            """SELECT COUNT(*) FROM proof_bucket b WHERE b.bucket='齐套' AND NOT EXISTS (
                 SELECT 1 FROM src_part_ledger l
                 WHERE l.ingest_id=b.ingest_id
                   AND l.id=(SELECT MAX(id) FROM src_part_ledger WHERE ingest_id=b.ingest_id)
                   AND l.tenant_part IS NOT NULL AND TRIM(l.tenant_part)<>''
                   AND l.biz_part IS NOT NULL AND TRIM(l.biz_part)<>''
               )""",
        ),
    }

    return {
        "ingest_id": ingest_id,
        "arch": arch,
        "arch_label": ARCH_LABELS[arch],
        "criterion": ARCH_CRITERIA[arch],
        "evidence_form": EVIDENCE_FORM,
        "inbound": dict(inbound),
        "ledger": events,
        "current": {
            "tenant_part": tenant,
            "biz_part": biz,
            "canon_old": canon,
            "complete": complete,
            "missing": missing,
            "rev": inbound["rev"],
            "lookup_key": lookup_key,
        },
        "mapping": mapping,
        "formal": formals,
        "bucket": dict(bucket) if bucket else None,
        "marks": marks,
        "counts": counts,
        "integrity": integrity,
    }
