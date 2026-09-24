"""Retro game requests: create, dedupe, permissions, admin actions, auto-fulfil on scan."""
import pytest


@pytest.fixture
def req_app(tmp_path, monkeypatch):
    import settings as settings_mod
    from app import app
    from db import db, init_db
    import roms
    import rom_requests

    monkeypatch.setattr(settings_mod, "CONFIG_FILE", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(settings_mod, "_cached_settings", None)
    root = tmp_path / "roms"
    (root / "gba").mkdir(parents=True)
    (root / "gba" / "Advance Wars (USA).gba").write_bytes(b"x")
    monkeypatch.setattr(roms, "ROMS_ROOT", str(root))
    monkeypatch.setattr(roms, "STATES_DIR", str(tmp_path / "states"))
    sent = []
    monkeypatch.setattr(rom_requests, "notify", lambda m: sent.append(m))

    init_db(app)
    with app.app_context():
        db.drop_all()
        db.create_all()
        roms.scan_roms()
    return app, root, sent


def _login(app, user, password):
    c = app.test_client()
    c.post("/login", data={"user": user, "password": password})
    return c


def _users(app):
    from auth import create_or_update_user
    with app.app_context():
        create_or_update_user("admin", "Adm1n!pass", admin_access=True, shop_access=True)
        create_or_update_user("ana", "An4!passxx", shop_access=True)
        create_or_update_user("rui", "Ru1!passxx", shop_access=True)


def test_normalize_title():
    from rom_requests import normalize_title
    assert normalize_title("Golden Sun (USA, Europe) [!]") == "golden sun"
    assert normalize_title("Mario & Luigi: Superstar Saga") == "mario and luigi superstar saga"


def test_create_dedupe_and_already_owned(req_app):
    app, _, sent = req_app
    c = app.test_client()  # no admin yet -> auth off
    r = c.post("/api/roms/requests", json={"title": "Golden Sun", "platform": "gba"})
    assert r.status_code == 201 and sent and "Golden Sun" in sent[-1]
    dup = c.post("/api/roms/requests", json={"title": "golden sun (europe)", "platform": "gba"})
    assert dup.get_json()["duplicate"] is True
    owned = c.post("/api/roms/requests", json={"title": "Advance Wars", "platform": "gba"})
    assert owned.status_code == 409
    assert c.post("/api/roms/requests", json={"title": "", "platform": "gba"}).status_code == 400
    assert c.post("/api/roms/requests", json={"title": "X", "platform": "nope"}).status_code == 400
    assert len(c.get("/api/roms/requests").get_json()["requests"]) == 1


def test_permissions(req_app):
    app, _, _ = req_app
    _users(app)
    ana, rui, admin = _login(app, "ana", "An4!passxx"), _login(app, "rui", "Ru1!passxx"), _login(app, "admin", "Adm1n!pass")
    rid = ana.post("/api/roms/requests", json={"title": "Golden Sun", "platform": "gba"}).get_json()["request"]["id"]
    rui.post("/api/roms/requests", json={"title": "Metroid Fusion", "platform": "gba"})

    assert [r["title"] for r in ana.get("/api/roms/requests").get_json()["requests"]] == ["Golden Sun"]
    assert len(admin.get("/api/roms/requests").get_json()["requests"]) == 2
    assert ana.patch(f"/api/roms/requests/{rid}", json={"status": "approved"}).status_code == 403
    assert rui.delete(f"/api/roms/requests/{rid}").status_code == 403

    r = admin.patch(f"/api/roms/requests/{rid}", json={"status": "approved", "admin_note": "Buying it"})
    assert r.get_json()["request"]["status"] == "approved"
    assert ana.delete(f"/api/roms/requests/{rid}").status_code == 403  # no longer pending
    assert admin.delete(f"/api/roms/requests/{rid}").status_code == 200
    assert ana.get("/roms/requests").status_code == 200


def test_scan_fulfils_matching_request(req_app):
    app, root, sent = req_app
    import roms
    from rom_requests import RomRequest
    c = app.test_client()
    c.post("/api/roms/requests", json={"title": "Golden Sun", "platform": "gba"})
    c.post("/api/roms/requests", json={"title": "Golden Sun", "platform": "gb"})  # other platform
    (root / "gba" / "Golden Sun (Europe) [!].gba").write_bytes(b"y")
    with app.app_context():
        roms.scan_roms()
        by_platform = {r.platform: r for r in RomRequest.query.all()}
        assert by_platform["gba"].status == "fulfilled" and by_platform["gba"].rom_id
        assert by_platform["gb"].status == "pending"
    assert any("fulfilled" in m for m in sent)
