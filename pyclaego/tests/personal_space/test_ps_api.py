"""REST API tests for /api/v2/* (Phase 10 / 4.3).

Uses a freshly-built FastAPI app with only the ps_api router mounted, so we
avoid triggering the global bridge-client startup."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pyclaego.personal_space import PersonalSpaceManager, WidgetClassRegistry


@pytest.fixture()
def isolated_ps_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Per-test PS root + reset PSManager singleton + isolate registry user_root."""
    PersonalSpaceManager.reset_instance()
    WidgetClassRegistry.reset_instance()

    # Point the manager at tmp_path through env so _get_psm picks it up.
    # We bypass the env path and just construct the singleton with the path.
    PersonalSpaceManager.instance(root_path=tmp_path / "personal_spaces")
    # Use the builtin registry only (don't read user dotfiles).
    WidgetClassRegistry.instance(user_root=tmp_path / "no_user_classes")
    yield tmp_path

    PersonalSpaceManager.reset_instance()
    WidgetClassRegistry.reset_instance()


@pytest.fixture()
def client(isolated_ps_root: Path) -> TestClient:
    from pyclaego.web.ps_api import router as ps_router
    app = FastAPI()
    app.include_router(ps_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# WidgetClass listing
# ---------------------------------------------------------------------------


class TestWidgetClasses:
    def test_list_includes_builtin(self, client: TestClient):
        resp = client.get("/api/v2/widget_classes")
        assert resp.status_code == 200
        data = resp.json()
        ids = [c["class_id"] for c in data["widget_classes"]]
        assert "chat" in ids
        assert "notes" in ids


# ---------------------------------------------------------------------------
# PS create / list / get
# ---------------------------------------------------------------------------


class TestPSCRUD:
    def test_create_and_get_ps(self, client: TestClient):
        r = client.post("/api/v2/personal_spaces/alice", json={"title": "Alice"})
        assert r.status_code == 200
        body = r.json()
        assert body["ps_id"] == "alice"
        # bootstrap creates the default chat widget
        ids = [w["widget_id"] for w in body["widgets"]]
        assert "w_chat_default" in ids

        r2 = client.get("/api/v2/personal_spaces/alice")
        assert r2.status_code == 200
        assert r2.json()["manifest"].get("title") == "Alice"

    def test_list_after_creation(self, client: TestClient):
        client.post("/api/v2/personal_spaces/bob", json={})
        r = client.get("/api/v2/personal_spaces")
        assert "bob" in r.json()["personal_spaces"]

    def test_get_unknown_ps_404(self, client: TestClient):
        assert client.get("/api/v2/personal_spaces/nobody").status_code == 404

    def test_invalid_ps_id_400(self, client: TestClient):
        r = client.post("/api/v2/personal_spaces/.bad", json={})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Widget create / get / patch / delete
# ---------------------------------------------------------------------------


class TestWidgetCRUD:
    def test_create_widget_and_fetch(self, client: TestClient):
        client.post("/api/v2/personal_spaces/alice", json={})
        r = client.post(
            "/api/v2/personal_spaces/alice/widgets",
            json={"widget_id": "w_notes_1", "widget_class": "notes", "title": "My notes"},
        )
        assert r.status_code == 200
        assert r.json()["widget_id"] == "w_notes_1"

        # Widget appears in PS summary
        ps = client.get("/api/v2/personal_spaces/alice").json()
        assert any(w["widget_id"] == "w_notes_1" for w in ps["widgets"])

        info = client.get("/api/v2/personal_spaces/alice/widgets/w_notes_1").json()
        assert info["widget_class"]["class_id"] == "notes"
        assert "store" in info["resolved_config"]

    def test_create_widget_unknown_class_400(self, client: TestClient):
        client.post("/api/v2/personal_spaces/alice", json={})
        r = client.post(
            "/api/v2/personal_spaces/alice/widgets",
            json={"widget_id": "x", "widget_class": "no_such_class"},
        )
        assert r.status_code == 400

    def test_create_widget_duplicate_409(self, client: TestClient):
        client.post("/api/v2/personal_spaces/alice", json={})
        body = {"widget_id": "dup", "widget_class": "chat"}
        assert client.post("/api/v2/personal_spaces/alice/widgets", json=body).status_code == 200
        assert client.post("/api/v2/personal_spaces/alice/widgets", json=body).status_code == 409

    def test_patch_widget_config(self, client: TestClient, isolated_ps_root: Path):
        client.post("/api/v2/personal_spaces/alice", json={})
        client.post(
            "/api/v2/personal_spaces/alice/widgets",
            json={"widget_id": "w_c", "widget_class": "chat"},
        )
        r = client.patch(
            "/api/v2/personal_spaces/alice/widgets/w_c/config",
            json={"config": {"foo": {"bar": 42}}},
        )
        assert r.status_code == 200
        # config persisted to disk
        cfg_path = (
            isolated_ps_root
            / "personal_spaces" / "alice" / "widgets" / "w_c" / "widget.config.json"
        )
        on_disk = json.loads(cfg_path.read_text())
        assert on_disk == {"foo": {"bar": 42}}

    def test_delete_widget(self, client: TestClient, isolated_ps_root: Path):
        client.post("/api/v2/personal_spaces/alice", json={})
        client.post(
            "/api/v2/personal_spaces/alice/widgets",
            json={"widget_id": "w_gone", "widget_class": "chat"},
        )
        r = client.delete("/api/v2/personal_spaces/alice/widgets/w_gone")
        assert r.status_code == 200
        assert not (
            isolated_ps_root / "personal_spaces" / "alice" / "widgets" / "w_gone"
        ).exists()
        assert client.get("/api/v2/personal_spaces/alice/widgets/w_gone").status_code == 404

    def test_get_unknown_widget_404(self, client: TestClient):
        client.post("/api/v2/personal_spaces/alice", json={})
        assert (
            client.get("/api/v2/personal_spaces/alice/widgets/missing").status_code == 404
        )


# ---------------------------------------------------------------------------
# Highlight (no hook → empty dict)
# ---------------------------------------------------------------------------


class TestHighlight:
    def test_chat_widget_highlight_empty(self, client: TestClient):
        client.post("/api/v2/personal_spaces/alice", json={})
        r = client.get(
            "/api/v2/personal_spaces/alice/widgets/w_chat_default/highlight"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ps_id"] == "alice"
        assert body["highlight"] == {}
