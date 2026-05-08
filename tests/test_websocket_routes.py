from __future__ import annotations


def _rule_paths(app) -> set[str]:
    return {rule.rule for rule in app.url_map.iter_rules()}


def test_create_app_registers_websocket_hub_extension(app):
    assert "websocket_hub" in app.extensions


def test_create_app_registers_snapshot_websocket_routes(app):
    rules = _rule_paths(app)

    assert "/ws/snapshots" in rules
    assert "/ws/cameras/<camera_id>/snapshots" in rules
