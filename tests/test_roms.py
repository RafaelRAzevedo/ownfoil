"""Retro ROM library: scanning, file serving, per-user save states."""
import pytest


@pytest.fixture
def roms_app(tmp_path, monkeypatch):
    import settings as settings_mod
    from app import app
    from db import db, init_db
    import roms

    monkeypatch.setattr(settings_mod, "CONFIG_FILE", str(tmp_path / "settings.yaml"))
    monkeypatch.setattr(settings_mod, "_cached_settings", None)

    root = tmp_path / "roms"
    (root / "gba" / "sub").mkdir(parents=True)
    (root / "gba" / "Advance Wars.gba").write_bytes(b"x" * 1024)
    (root / "gba" / "sub" / "Golden Sun.gba").write_bytes(b"y" * 10)
    (root / "gba" / "Golden Sun.sav").write_bytes(b"s")          # wrong ext, ignored
    (root / "genesis").mkdir()
    (root / "genesis" / "Sonic.md").write_bytes(b"z")             # alias -> megadrive
    (root / "unknown").mkdir()
    (root / "unknown" / "thing.bin").write_bytes(b"q")            # unknown platform, ignored

    monkeypatch.setattr(roms, "ROMS_ROOT", str(root))
    monkeypatch.setattr(roms, "STATES_DIR", str(tmp_path / "states"))

    init_db(app)
    with app.app_context():
        db.drop_all()
        db.create_all()
    return app, root


def test_scan_finds_platform_roms(roms_app):
    app, root = roms_app
    import roms
    with app.app_context():
        added, removed, total = roms.scan_roms()
        assert (added, removed, total) == (3, 0, 3)
        names = {(r.platform, r.name) for r in roms.Rom.query.all()}
        assert names == {("gba", "Advance Wars"), ("gba", "Golden Sun"), ("megadrive", "Sonic")}

        (root / "genesis" / "Sonic.md").unlink()
        assert roms.scan_roms() == (0, 1, 2)


def test_pages_and_file_download(roms_app):
    app, _ = roms_app
    import roms
    with app.app_context():
        roms.scan_roms()
        rom_id = roms.Rom.query.filter_by(name="Advance Wars").one().id
    client = app.test_client()
    page = client.get("/roms")
    assert page.status_code == 200 and b"Advance Wars" in page.data and b"Game Boy Advance" in page.data
    play = client.get(f"/roms/play/{rom_id}")
    assert play.status_code == 200 and b'EJS_core = "gba"' in play.data
    f = client.get(f"/api/roms/{rom_id}/file?download=1")
    assert f.status_code == 200 and len(f.data) == 1024
    assert "attachment" in f.headers["Content-Disposition"]
    assert client.get("/api/roms/9999/file").status_code == 404


def test_state_roundtrip(roms_app):
    app, _ = roms_app
    import roms
    with app.app_context():
        roms.scan_roms()
        rom_id = roms.Rom.query.first().id
    client = app.test_client()
    url = f"/api/roms/{rom_id}/state"
    assert client.get(url).status_code == 404
    assert client.put(url, data=b"\x01\x02state").status_code == 200
    assert client.get(url).data == b"\x01\x02state"
    assert b"EJS_loadStateURL" in client.get(f"/roms/play/{rom_id}").data
    assert client.delete(url).status_code == 200
    assert client.get(url).status_code == 404
    assert client.put(url, data=b"").status_code == 400
