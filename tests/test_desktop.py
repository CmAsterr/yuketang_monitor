"""Desktop lifecycle checks: temporary paths and simulated clocks/connections only."""

from __future__ import annotations
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from yktmon.lifecycle import AppLifecycle, workspace_identity
from yktmon.web import create_app
from yktmon import desktop


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now

    def advance(self, app, seconds):
        while seconds:
            step = min(seconds, 10)
            self.now += step
            seconds -= step
            app.tick()


def lifecycle(tmp_path, **kwargs):
    clock = Clock()
    app = AppLifecycle(tmp_path / "config", tmp_path / "data", clock=clock, **kwargs)
    app.ready = True
    return app, clock


def test_desktop_exits_only_after_final_page_and_full_grace(tmp_path):
    stopped = []
    app, clock = lifecycle(tmp_path, desktop=True, on_exit=lambda: stopped.append(True))
    first = app.connect("first")
    second = app.connect("second")
    clock.advance(app, 600)
    assert not stopped
    app.disconnect(first)
    clock.advance(app, 600)
    assert not stopped
    app.disconnect(second)
    clock.advance(app, 179)
    assert not stopped
    clock.advance(app, 1)
    assert stopped == [True]
    clock.advance(app, 60)
    assert stopped == [True]


def test_reload_old_disconnect_cannot_remove_new_connection(tmp_path):
    app, clock = lifecycle(tmp_path, desktop=True)
    old = app.connect("page")
    new = app.connect("page")
    app.disconnect(old)
    clock.advance(app, 300)
    assert not app.exit_requested and app.status()["page_count"] == 1
    app.disconnect(new)
    clock.advance(app, 100)
    app.connect("reopened")
    clock.advance(app, 300)
    assert not app.exit_requested


def test_persistent_mode_never_exits_for_missing_pages(tmp_path):
    app, clock = lifecycle(tmp_path, desktop=False)
    clock.advance(app, 10000)
    assert not app.exit_requested
    token = app.connect("page")
    app.disconnect(token)
    clock.advance(app, 10000)
    assert not app.exit_requested and app.status()["shutdown_in_seconds"] is None


def test_browser_failure_and_launch_reservation(tmp_path):
    app, clock = lifecycle(tmp_path, desktop=True)
    clock.advance(app, 170)
    assert app.reserve_open()
    clock.advance(app, 179)
    assert not app.exit_requested
    clock.advance(app, 1)
    assert app.exit_requested
    assert not app.reserve_open()
    with pytest.raises(RuntimeError):
        app.connect("too-late")


def test_sleep_wake_gets_new_grace_window(tmp_path):
    app, clock = lifecycle(tmp_path, desktop=True)
    clock.advance(app, 170)
    clock.now += 7200
    app.tick()
    assert not app.exit_requested and app.status()["shutdown_in_seconds"] == 180
    clock.advance(app, 179)
    assert not app.exit_requested
    clock.advance(app, 1)
    assert app.exit_requested


def test_exit_and_presence_endpoints_require_same_origin(tmp_path):
    stopped = []
    life = AppLifecycle(
        tmp_path / "config",
        tmp_path / "data",
        desktop=True,
        on_exit=lambda: stopped.append(True),
    )
    app = create_app(
        tmp_path / "config", tmp_path / "data", autostart=False, lifecycle=life
    )
    with TestClient(app, base_url="http://localhost") as c:
        state = c.get("/api/app/status").json()
        assert state["mode"] == "desktop"
        assert (
            c.post("/api/app/exit", json={"instance_id": life.instance}).status_code
            == 403
        )
        assert (
            c.post(
                "/api/app/exit",
                headers={"X-Yktmon": "local", "Origin": "https://evil.example"},
                json={"instance_id": life.instance},
            ).status_code
            == 403
        )
        assert (
            c.post(
                "/api/app/exit",
                headers={"X-Yktmon": "local"},
                json={"instance_id": "stale"},
            ).status_code
            == 409
        )
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(
                "ws://localhost/api/app/presence?page_id=abcdefghijklmnop",
                headers={"Origin": "https://evil.example"},
            ):
                pass
        with c.websocket_connect(
            "ws://localhost/api/app/presence?page_id=abcdefghijklmnop",
            headers={"Origin": "http://localhost"},
        ) as ws:
            assert ws.receive_json()["page_count"] == 1
            ws.send_text("heartbeat")
            assert ws.receive_json()["mode"] == "desktop"
        response = c.post(
            "/api/app/exit",
            headers={"X-Yktmon": "local"},
            json={"instance_id": life.instance},
        )
        assert response.status_code == 200 and stopped == [True]
        assert c.get("/api/app/status").json()["shutting_down"]


def mock_launcher_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        desktop.httpx,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
    )


def test_launcher_reuses_persistent_service_without_changing_mode(
    tmp_path, monkeypatch
):
    config = tmp_path / "c"
    data = tmp_path / "data"
    calls = []
    opens = []
    state = {
        "service": "yktmon",
        "lifecycle_version": 1,
        "workspace_id": workspace_identity(config, data),
        "ready": True,
        "mode": "persistent",
    }

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json=state)

    mock_launcher_client(monkeypatch, handler)

    def spawn(*args, **kwargs):
        raise AssertionError("Must not spawn when service exists")

    result = desktop.launch(
        config, data, opener=lambda url: opens.append(url), spawn=spawn
    )
    assert not result["started"] and result["mode"] == "persistent"
    assert opens == ["http://127.0.0.1:8787/#courses"]
    assert calls == [("GET", "/api/app/status"), ("POST", "/api/app/open")]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, text="wrong app"),
        httpx.Response(200, json={"service": "unrelated"}),
        httpx.Response(
            200,
            json={"service": "yktmon", "lifecycle_version": 1, "workspace_id": "other"},
        ),
    ],
)
def test_launcher_does_not_spawn_over_unknown_or_other_workspace(
    tmp_path, monkeypatch, response
):
    mock_launcher_client(monkeypatch, lambda request: response)

    def spawn(*args, **kwargs):
        raise AssertionError("Must not replace another service")

    with pytest.raises(desktop.LaunchError):
        desktop.launch(
            tmp_path / "c", tmp_path / "data", spawn=spawn, opener=lambda url: False
        )


def test_launcher_start_then_healthcheck_then_browser(tmp_path, monkeypatch):
    config = tmp_path / "c"
    data = tmp_path / "data"
    spawned = []
    opened = []

    def handler(request):
        if not spawned:
            raise httpx.ConnectError("not running", request=request)
        return httpx.Response(
            200,
            json={
                "service": "yktmon",
                "lifecycle_version": 1,
                "workspace_id": workspace_identity(config, data),
                "ready": True,
                "mode": "desktop",
            },
        )

    mock_launcher_client(monkeypatch, handler)

    class Child:
        def poll(self):
            return None

    def spawn(*args, **kwargs):
        spawned.append((args, kwargs))
        return Child()

    result = desktop.launch(
        config, data, spawn=spawn, opener=lambda url: opened.append(url)
    )
    assert result["started"] and len(spawned) == len(opened) == 1
    assert spawned[0][0][:2] == (config.resolve(), data.resolve())


def test_launcher_failure_never_opens_browser(tmp_path, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("not running", request=request)

    mock_launcher_client(monkeypatch, handler)

    class Child:
        def poll(self):
            return 1

    with pytest.raises(desktop.LaunchError, match="启动失败"):
        desktop.launch(
            tmp_path / "c",
            tmp_path / "data",
            spawn=lambda *a, **kw: Child(),
            opener=lambda url: pytest.fail("Should not open"),
        )


def test_workspace_identity_uses_explicit_paths(tmp_path):
    assert workspace_identity(tmp_path / "c", tmp_path / "a") != workspace_identity(
        tmp_path / "c", tmp_path / "b"
    )
    assert workspace_identity(tmp_path / "./c", tmp_path / "a") == workspace_identity(
        tmp_path / "c", tmp_path / "a"
    )


@pytest.mark.parametrize("failure_type", [httpx.ConnectError, httpx.ConnectTimeout])
def test_probe_handles_windows_closed_port_variants(tmp_path, failure_type):
    def handler(request):
        raise failure_type("closed port", request=request)

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as c:
        assert desktop.probe(c, "any") is None


def test_first_browser_startup_has_minimum_opening_window(tmp_path):
    app, clock = lifecycle(tmp_path, desktop=True, grace_seconds=4)
    clock.advance(app, 10)
    assert not app.exit_requested
    token = app.connect("page")
    app.disconnect(token)
    clock.advance(app, 4)
    assert app.exit_requested
