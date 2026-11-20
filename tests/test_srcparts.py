"""源键分段闸夹具（本地 SQLite）。

覆盖四条硬约束：
1. 分段证据必须落库，且只追加不可改写；
2. 全局只贯彻一种正确性口径（complete / degrade），不得混用；
3. 齐套架构缺段拒写、降级架构缺段出键必带标记并剔出齐套桶；
4. 补段与重映共用写锁，争用必须可复现且留痕。
"""

import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as appmod
import srcparts_gate as gate


def configure(tmp_path, monkeypatch, arch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(appmod, "CONFIGURED_ARCH", arch)
    appmod.init_db()
    return TestClient(appmod.app)


def login(c):
    token = c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + token}


def db_count(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


@pytest.fixture()
def complete_client(tmp_path, monkeypatch):
    return configure(tmp_path, monkeypatch, gate.ARCH_COMPLETE)


@pytest.fixture()
def degrade_client(tmp_path, monkeypatch):
    return configure(tmp_path, monkeypatch, gate.ARCH_DEGRADE)


def test_ingest_splits_parts_and_persists_ledger(complete_client, tmp_path):
    c, h = complete_client, login(complete_client)
    out = c.post("/api/src-parts/ingest", headers=h,
                 json={"ingest_id": "ing-1", "tenant_part": "T1", "biz_part": "OLD-B3"}).json()
    assert out["canon_old"] == "T1::OLD-B3" and out["complete"] is True

    conn = sqlite3.connect(tmp_path / "t.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM src_part_ledger WHERE ingest_id='ing-1'").fetchone()
    conn.close()
    assert (row["tenant_part"], row["biz_part"], row["canon_old"]) == ("T1", "OLD-B3", "T1::OLD-B3")

    # 证据只追加：投影页/接口都无法临时拼接，直接改证据被数据库拒绝
    conn = sqlite3.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE src_part_ledger SET tenant_part='T9' WHERE ingest_id='ing-1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM src_part_ledger WHERE ingest_id='ing-1'")
    conn.close()


def test_complete_arch_rejects_missing_tenant_with_zero_formal(complete_client, tmp_path):
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-miss", "tenant_part": "  ", "biz_part": "OLD-B3"})
    before = c.get("/api/formal", headers=h).json()["items"]

    r = c.post("/api/src-parts/remap?ingest_id=ing-miss", headers=h)
    assert r.status_code == 422
    assert r.json()["detail"]["formal_inserted"] == 0

    assert c.get("/api/formal", headers=h).json()["items"] == before
    snap = c.get("/api/src-parts/ingest/ing-miss", headers=h).json()
    assert snap["current"]["missing"] == ["tenant_part"]
    assert snap["inbound"]["status"] == "待补段"
    assert snap["marks"] == [] and snap["formal"] == []
    assert snap["integrity"]["degraded_in_complete_bucket"] == 0
    assert db_count(tmp_path / "t.db",
                    "SELECT COUNT(*) FROM write_trace WHERE outcome='rejected_missing'") == 1


def test_complete_arch_complete_parts_then_remap(complete_client, tmp_path):
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-late", "tenant_part": "", "biz_part": "OLD-B3"})
    out = c.post("/api/src-parts/complete?ingest_id=ing-late", headers=h,
                 json={"tenant_part": "T1", "expected_rev": 1}).json()
    assert out["rev"] == 2 and out["complete"] is True

    # 补段是新增事件，不是改写旧证据
    conn = sqlite3.connect(tmp_path / "t.db")
    rows = conn.execute("SELECT seq,tenant_part,biz_part FROM src_part_ledger "
                        "WHERE ingest_id='ing-late' ORDER BY seq").fetchall()
    conn.close()
    assert [r[0] for r in rows] == [1, 2]
    assert rows[1][1:] == ("T1", "OLD-B3")

    r = c.post("/api/src-parts/remap?ingest_id=ing-late", headers=h)
    assert r.status_code == 200 and r.json()["part_state"] == "齐套"
    assert r.json()["entity_key"] == "NEW-300"


def test_complete_arch_db_trigger_blocks_degraded_key(complete_client, tmp_path):
    """即便绕过接口直写库，齐套架构也不允许缺段出键。"""
    h = login(complete_client)
    complete_client.post("/api/src-parts/ingest", headers=h,
                         json={"ingest_id": "ing-t", "tenant_part": None, "biz_part": "OLD-B3"})
    conn = sqlite3.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("""INSERT INTO formal_entities
            (entity_key,mapped_from,version,created_at,ingest_id,part_state)
            VALUES ('NEW-400','OLD-B3','m-2026.11','2026-11-20','ing-t','缺段')""")
    conn.close()
    assert db_count(tmp_path / "t.db", "SELECT COUNT(*) FROM formal_entities WHERE ingest_id='ing-t'") == 0


def test_degrade_arch_writes_key_with_queryable_mark(degrade_client, tmp_path):
    c, h = degrade_client, login(degrade_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-d", "tenant_part": None, "biz_part": "OLD-B3"})
    r = c.post("/api/src-parts/remap?ingest_id=ing-d", headers=h)
    assert r.status_code == 200
    payload = r.json()
    assert payload["entity_key"] == "NEW-400" and payload["part_state"] == "缺段"

    snap = c.get("/api/src-parts/ingest/ing-d", headers=h).json()
    assert snap["arch"] == gate.ARCH_DEGRADE
    assert snap["marks"][0]["missing_part"] == "tenant_part"
    assert snap["marks"][0]["missing_part"] is not None
    assert snap["bucket"]["bucket"] == "缺段"
    # 齐套桶里只有种子行；本笔缺段没有混进齐套计数
    assert snap["counts"]["complete_bucket"] == 1
    assert snap["counts"]["degraded_bucket"] == 1
    assert snap["counts"]["unbucketed_formal"] == 0

    summary = c.get("/api/src-parts/summary", headers=h).json()
    assert summary["counts"]["missing_marks"] == 1
    assert summary["integrity"]["unmarked_degraded_formal"] == 0
    assert summary["integrity"]["degraded_in_complete_bucket"] == 0


def test_degrade_arch_requires_biz_part(degrade_client):
    c, h = degrade_client, login(degrade_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-none", "tenant_part": None, "biz_part": None})
    r = c.post("/api/src-parts/remap?ingest_id=ing-none", headers=h)
    assert r.status_code == 422
    assert c.get("/api/src-parts/ingest/ing-none", headers=h).json()["formal"] == []


def test_degrade_arch_rolls_back_key_and_mark_together(degrade_client, tmp_path, monkeypatch):
    c, h = degrade_client, login(degrade_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-rb", "tenant_part": None, "biz_part": "OLD-B3"})

    def boom(*a, **kw):
        raise sqlite3.OperationalError("模拟入桶失败")

    original = gate.log_trace
    monkeypatch.setattr(gate, "log_trace", boom)
    try:
        with pytest.raises(sqlite3.OperationalError):
            c.post("/api/src-parts/remap?ingest_id=ing-rb", headers=h)
    finally:
        monkeypatch.setattr(gate, "log_trace", original)

    path = tmp_path / "t.db"
    assert db_count(path, "SELECT COUNT(*) FROM formal_entities WHERE ingest_id='ing-rb'") == 0
    assert db_count(path, "SELECT COUNT(*) FROM missing_part_marks WHERE ingest_id='ing-rb'") == 0
    assert db_count(path, "SELECT COUNT(*) FROM proof_bucket WHERE ingest_id='ing-rb'") == 0
    assert db_count(path, "SELECT status FROM src_inbound WHERE ingest_id='ing-rb'") == "待补段"


def test_marks_and_buckets_cannot_be_forged(complete_client, tmp_path):
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-forge", "tenant_part": "T1", "biz_part": "OLD-B3"})
    # 缺段标记不能挂到齐套行上
    assert c.post("/api/src-parts/remap?ingest_id=ing-forge", headers=h).json()["part_state"] == "齐套"
    conn = sqlite3.connect(tmp_path / "t.db")
    formal_id = conn.execute("SELECT id FROM formal_entities WHERE ingest_id='ing-forge'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO missing_part_marks(ingest_id,formal_id,missing_part,canon_used,created_at) "
                     "VALUES ('ing-forge',?,'tenant_part','x','now')", (formal_id,))
    # 无标记不得进缺段桶
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO proof_bucket(formal_id,ingest_id,bucket,canon_key,added_at) "
                     "VALUES (?, 'ing-forge','缺段','x','now')", (formal_id,))
    conn.close()


def test_arch_is_deployment_single_choice(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(appmod, "CONFIGURED_ARCH", gate.ARCH_COMPLETE)
    appmod.init_db()
    monkeypatch.setattr(appmod, "CONFIGURED_ARCH", gate.ARCH_DEGRADE)
    with pytest.raises(RuntimeError):
        appmod.init_db()
    # 库里口径未被悄悄改写
    assert db_count(tmp_path / "t.db", "SELECT v FROM app_settings WHERE k='src_part_arch'") == "complete"


def test_verify_is_read_only(complete_client, tmp_path):
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-ro", "tenant_part": "T1", "biz_part": "OLD-B3"})
    conn = appmod.connect_ro()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO write_trace(route,outcome,at) VALUES ('x','y','z')")
        snap = gate.verify_snapshot(conn, "ing-ro")
    finally:
        conn.close()
    assert snap["current"]["complete"] is True
    assert snap["mapping"]["new_key"] == "NEW-300"


def test_complete_parts_contention_is_reproducible(complete_client, tmp_path):
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-race", "tenant_part": "", "biz_part": "OLD-B3"})

    # 1) 库级写锁：第二个写事务不得绕过锁去改 ledger
    holder = sqlite3.connect(tmp_path / "t.db", timeout=0)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    blocked = sqlite3.connect(tmp_path / "t.db", timeout=0)
    with pytest.raises(sqlite3.OperationalError):
        blocked.execute("BEGIN IMMEDIATE")
    blocked.close()
    holder.rollback()
    holder.close()

    # 2) 版本号争用：两个并发补段只能有一个改动 rev
    results = []

    def worker(value):
        results.append(c.post("/api/src-parts/complete?ingest_id=ing-race", headers=h,
                              json={"tenant_part": value, "expected_rev": 1}))

    threads = [threading.Thread(target=worker, args=("T1",)),
               threading.Thread(target=worker, args=("T1",))]
    for t in threads: t.start()
    for t in threads: t.join()
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["detail"].startswith("补段争用")
    assert db_count(tmp_path / "t.db",
                    "SELECT COUNT(*) FROM src_part_ledger WHERE ingest_id='ing-race'") == 2
    assert db_count(tmp_path / "t.db",
                    "SELECT COUNT(*) FROM write_trace WHERE ingest_id='ing-race' AND outcome='contention'") == 1


def test_no_double_write_between_remap_and_complete(complete_client, tmp_path):
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-dbl", "tenant_part": "", "biz_part": "OLD-B3"})
    # 先补段再重映：只能有一笔正式键
    assert c.post("/api/src-parts/complete?ingest_id=ing-dbl", headers=h,
                  json={"tenant_part": "T1", "expected_rev": 1}).status_code == 200
    assert c.post("/api/src-parts/remap?ingest_id=ing-dbl", headers=h).status_code == 200
    assert c.post("/api/src-parts/remap?ingest_id=ing-dbl", headers=h).status_code == 409
    # 已重映后补段被拒，不能借补段再触发一笔写入
    assert c.post("/api/src-parts/complete?ingest_id=ing-dbl", headers=h,
                  json={"tenant_part": "T2", "expected_rev": 2}).status_code == 409
    assert db_count(tmp_path / "t.db",
                    "SELECT COUNT(*) FROM formal_entities WHERE ingest_id='ing-dbl'") == 1


def test_arch_query_param_is_ignored(complete_client):
    """口径不可由请求携带：带 arch=degrade 也仍按部署口径拒写缺段。"""
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-p", "tenant_part": None, "biz_part": "OLD-B3"})
    r = c.post("/api/src-parts/remap?ingest_id=ing-p&arch=degrade", headers=h)
    assert r.status_code == 422
    assert c.get("/api/src-parts/summary", headers=h).json()["arch"] == gate.ARCH_COMPLETE


def test_remap_and_complete_race_has_single_winner_and_trace(complete_client, tmp_path):
    """重映与补段抢同一入站行：只允许一路成功，另一路必留争用/拒绝痕迹。"""
    c, h = complete_client, login(complete_client)
    c.post("/api/src-parts/ingest", headers=h,
           json={"ingest_id": "ing-duel", "tenant_part": "", "biz_part": "OLD-B3"})
    out = []

    def remap():
        out.append(("remap", c.post("/api/src-parts/remap?ingest_id=ing-duel", headers=h)))

    def complete():
        out.append(("complete", c.post("/api/src-parts/complete?ingest_id=ing-duel", headers=h,
                                       json={"tenant_part": "T1", "expected_rev": 1})))

    ts = [threading.Thread(target=remap), threading.Thread(target=complete)]
    for t in ts: t.start()
    for t in ts: t.join()
    path = tmp_path / "t.db"
    results = dict(out)
    codes = sorted(r.status_code for r in results.values())
    # 三种合法先后：补段先 → 重映成功；重映先（缺段拒写）→ 补段成功；重映先（已出键）→ 补段 409。
    assert codes in ([200, 200], [200, 422], [200, 409]), codes
    # 硬约束：抢写不得造成两笔正式键，也不得两路都写出正式行
    formal = db_count(path, "SELECT COUNT(*) FROM formal_entities WHERE ingest_id='ing-duel'")
    assert formal == (1 if results["remap"].status_code == 200 else 0)
    assert db_count(path, "SELECT COUNT(*) FROM proof_bucket WHERE ingest_id='ing-duel'") == formal
    # 每一路都必须留痕（入站一笔 + 重映一笔 + 补段一笔），失败的一路不得静默
    assert db_count(path, "SELECT COUNT(*) FROM write_trace WHERE ingest_id='ing-duel'") == 3
    failures = sum(1 for r in results.values() if r.status_code != 200)
    assert db_count(path, "SELECT COUNT(*) FROM write_trace WHERE ingest_id='ing-duel' "
                          "AND route IN ('remap','complete') AND outcome<>'ok'") == failures
