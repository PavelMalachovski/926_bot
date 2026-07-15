"""Tests for SQLite database resilience."""

from app.services.smc.db import Database


class TestDatabaseOpen:
    def test_creates_missing_parent_directories(self, tmp_path):
        path = tmp_path / "nested" / "dirs" / "smc.db"
        db = Database(str(path))
        db.kv_set("probe", 1)
        assert path.exists()
        assert Database(str(path)).kv_get("probe") == 1

    def test_falls_back_when_path_unusable(self, tmp_path, monkeypatch):
        # A file where a directory is expected makes the path unusable.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.chdir(tmp_path)  # fallback file lands in tmp, not the repo
        db = Database(str(blocker / "sub" / "smc.db"))
        assert db.path == Database.FALLBACK_PATH
        db.kv_set("probe", "ok")  # still functional
        assert db.kv_get("probe") == "ok"
