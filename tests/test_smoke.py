

import pytest
from fastapi.testclient import TestClient
from app import app, init_db

@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path/"t.db"); init_db(); yield

def test_remap_and_conflict():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "实体键桥"
    h = {"Authorization":"Bearer "+c.post("/api/login", json={"username":"admin","password":"123456"}).json()["token"]}
    assert c.post("/api/src-parts/ingest", headers=h,
                  json={"ingest_id":"s1","tenant_part":"T1","biz_part":"OLD-B3"}).json()["complete"] is True
    # 齐套键带租户段：T1::OLD-B3 → NEW-300，证明租户段参与出键
    assert c.post("/api/src-parts/remap?ingest_id=s1", headers=h).json()["entity_key"] == "NEW-300"
    assert c.post("/api/src-parts/ingest", headers=h,
                  json={"ingest_id":"s2","tenant_part":"T1","biz_part":"OLD-ZZ"}).json()["complete"] is True
    assert c.post("/api/src-parts/remap?ingest_id=s2", headers=h).status_code == 409
    assert any(x["old_key"]=="T1::OLD-ZZ" for x in c.get("/api/conflicts", headers=h).json()["items"])
