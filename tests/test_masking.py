def test_mask_overview_and_rule_catalog(authed):
    ov = authed.get("/api/masking/overview").json()
    assert {"pending", "masked", "audits", "strength_name"} <= set(ov.keys())
    rules = authed.get("/api/masking/rules").json()["items"]
    assert len(rules) >= 3
    assert {"手机号", "证件号"} <= {r["field"] for r in rules}


def test_strength_locked_for_the_day(authed):
    r = authed.post("/api/masking/policy", json={"strength": "full"})
    assert r.status_code == 200
    assert r.json()["strength"] == "full"
    # 同日换强度必须被拦
    r2 = authed.post("/api/masking/policy", json={"strength": "shared"})
    assert r2.status_code == 409
    assert "脱敏强度写歪了" in r2.json()["error"]
    # 同值重设幂等
    r3 = authed.post("/api/masking/policy", json={"strength": "全掩码"})
    assert r3.status_code == 200
    assert r3.json()["strength"] == "full"


def test_strength_mismatch_blocks_load(authed):
    authed.post("/api/masking/policy", json={"strength": "shared"})
    items = authed.get("/api/pipelines").json()["items"]
    pid = items[0]["id"]
    authed.post(f"/api/pipelines/{pid}/extract")
    r = authed.post(f"/api/pipelines/{pid}/load", json={"strength": "full"})
    assert r.status_code == 400
    assert "脱敏强度写歪了" in r.json()["error"]


def test_full_strength_blocks_unmasked_load(authed):
    authed.post("/api/masking/policy", json={"strength": "full"})
    items = authed.get("/api/pipelines").json()["items"]
    pid = items[0]["id"]
    authed.post(f"/api/pipelines/{pid}/extract")
    blocked = authed.post(f"/api/pipelines/{pid}/load", json={"strength": "full"})
    assert blocked.status_code == 409
    assert "未脱敏完成" in blocked.json()["error"]
    # 未脱敏时确实还没落库
    detail = authed.get(f"/api/pipelines/{pid}").json()
    assert detail["status"] == "已抽取待加载"
    assert detail["mask_status"] == "待脱敏"

    m = authed.post(
        f"/api/pipelines/{pid}/mask",
        json={"claimer": "u-admin", "strength": "full"},
    )
    assert m.status_code == 200
    assert m.json()["pipeline"]["mask_status"] == "已掩码"
    assert m.json()["audits"] >= 3
    assert len(m.json()["sample"]) >= 3

    loaded = authed.post(f"/api/pipelines/{pid}/load", json={"strength": "full"})
    assert loaded.status_code == 200
    assert loaded.json()["status"] == "已落库"


def test_shared_strength_splits_external_and_internal(authed):
    authed.post("/api/masking/policy", json={"strength": "shared"})
    items = authed.get("/api/pipelines").json()["items"]
    external = next(p for p in items if p["share_scope"] == "external")
    internal = next(p for p in items if p["share_scope"] == "internal")

    authed.post(f"/api/pipelines/{external['id']}/extract")
    blocked = authed.post(f"/api/pipelines/{external['id']}/load", json={"strength": "shared"})
    assert blocked.status_code == 409
    assert "未脱敏完成" in blocked.json()["error"]

    authed.post(f"/api/pipelines/{internal['id']}/extract")
    audits_before = authed.get("/api/masking/overview").json()["audits"]
    ok = authed.post(f"/api/pipelines/{internal['id']}/load", json={"strength": "shared"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "已落库"
    assert ok.json()["mask_status"] == "明文落库"
    ov = authed.get("/api/masking/overview").json()
    assert ov["audits"] == audits_before + 1


def test_contention_leaves_single_winner(authed):
    authed.post("/api/masking/policy", json={"strength": "full"})
    items = authed.get("/api/pipelines").json()["items"]
    pid = items[0]["id"]
    authed.post(f"/api/pipelines/{pid}/extract")
    first = authed.post(f"/api/pipelines/{pid}/mask", json={"claimer": "u-a", "strength": "full"})
    second = authed.post(f"/api/pipelines/{pid}/mask", json={"claimer": "u-b", "strength": "full"})
    assert first.status_code == 200
    assert second.status_code == 409
    detail = authed.get(f"/api/pipelines/{pid}/mask").json()
    claims = detail["claims"]
    assert len([c for c in claims if c["status"] == "won"]) == 1
    assert len([c for c in claims if c["status"] == "lost"]) == 1
    assert claims[0]["claimer"] == "u-a"


def test_empty_rule_name_rejected(authed):
    r = authed.post(
        "/api/masking/rules",
        json={"name": "   ", "field": "手机号", "mask_type": "phone"},
    )
    assert r.status_code == 400
    assert "规则名" in r.json()["error"]
    r2 = authed.put("/api/masking/rules/1", json={"name": ""})
    assert r2.status_code == 400


def test_rule_locked_while_extraction_open(authed):
    items = authed.get("/api/pipelines").json()["items"]
    pid = items[0]["id"]
    started = authed.post(f"/api/pipelines/{pid}/extract/start")
    assert started.status_code == 200
    assert started.json()["status"] == "抽取中"
    blocked = authed.post(
        "/api/masking/rules",
        json={"name": "新规则", "field": "邮箱", "mask_type": "email"},
    )
    assert blocked.status_code == 409
    assert "抽取流程尚未收束" in blocked.json()["error"]
    assert authed.get("/api/masking/overview").json()["extraction_open"] is True
    finished = authed.post(f"/api/pipelines/{pid}/extract/finish")
    assert finished.json()["status"] == "已抽取待加载"
    ok = authed.post(
        "/api/masking/rules",
        json={"name": "新规则", "field": "邮箱", "mask_type": "email"},
    )
    assert ok.status_code == 200


def test_sample_has_before_after_and_mask(authed):
    items = authed.get("/api/pipelines").json()["items"]
    pid = items[0]["id"]
    sample = authed.get(f"/api/pipelines/{pid}/mask/sample").json()
    assert sample["count"] >= 3
    for row in sample["items"]:
        assert row["before"]
        assert row["after"]
        assert "mask_type" in row
    phone_rows = [r for r in sample["items"] if r["mask_type"] == "phone"]
    assert phone_rows
    for row in phone_rows:
        assert row["before"] != row["after"]
        assert "*" in row["after"]
        assert row["after"][-4:] == row["before"][-4:]


def test_page_shows_masking_panels(client):
    auto = client
    auto.post("/api/login", json={"username": "admin", "password": "123456"})
    page = auto.get("/app/").text
    for text in ["待脱敏", "已掩码", "审计条数", "脱敏强度", "对照抽样", "脱敏争抢记录"]:
        assert text in page
