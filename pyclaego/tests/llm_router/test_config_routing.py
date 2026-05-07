"""Tests for config + routing."""

from __future__ import annotations

import pytest

from pyclaego.llm_router.config import (
    RouterConfigError,
    parse_router_config,
)
from pyclaego.llm_router.routing import RouteTable


def _base_raw():
    return {
        "server": {"host": "127.0.0.1", "port": 18790},
        "storage": {
            "call_dump_dir": "/tmp/r/calls",
            "stats_db_path": "/tmp/r/stats.sqlite",
            "dump_enabled": True,
            "mask_keys": ["api_key", "authorization"],
        },
        "upstreams": [
            {
                "id": "u1",
                "protocol": "openai",
                "base_url": "https://api.example/v1",
                "api_key": "sk-test",
                "headers": {},
                "models": [
                    {"alias": "alpha", "upstream_model": "real/alpha"},
                    {"alias": "beta", "upstream_model": "real/beta"},
                ],
            }
        ],
    }


def test_parse_router_config_minimal_ok():
    cfg = parse_router_config(_base_raw())
    assert cfg.server.port == 18790
    assert len(cfg.upstreams) == 1
    assert cfg.upstreams[0].models[0].alias == "alpha"
    assert "authorization" in cfg.storage.mask_keys


def test_parse_router_config_invalid_protocol():
    raw = _base_raw()
    raw["upstreams"][0]["protocol"] = "bogus"
    with pytest.raises(RouterConfigError):
        parse_router_config(raw)


def test_parse_router_config_duplicate_alias_within_upstream():
    raw = _base_raw()
    raw["upstreams"][0]["models"].append(
        {"alias": "alpha", "upstream_model": "x"}
    )
    with pytest.raises(RouterConfigError):
        parse_router_config(raw)


def test_parse_router_config_duplicate_alias_across_upstreams_same_protocol():
    raw = _base_raw()
    raw["upstreams"].append(
        {
            "id": "u2",
            "protocol": "openai",
            "base_url": "https://api2.example",
            "api_key": "",
            "headers": {},
            "models": [{"alias": "alpha", "upstream_model": "z"}],
        }
    )
    with pytest.raises(RouterConfigError):
        parse_router_config(raw)


def test_parse_router_config_duplicate_alias_across_protocols_ok():
    raw = _base_raw()
    raw["upstreams"].append(
        {
            "id": "u2",
            "protocol": "anthropic",
            "base_url": "https://api2.example",
            "api_key": "",
            "headers": {},
            "models": [{"alias": "alpha", "upstream_model": "z"}],
        }
    )
    cfg = parse_router_config(raw)
    table = RouteTable(cfg)
    assert table.resolve("openai", "alpha").upstream.id == "u1"
    assert table.resolve("anthropic", "alpha").upstream.id == "u2"


def test_route_table_unknown_alias():
    cfg = parse_router_config(_base_raw())
    table = RouteTable(cfg)
    assert table.resolve("openai", "nope") is None
    assert table.resolve("anthropic", "alpha") is None


def test_parse_router_config_ollama_protocol_ok():
    raw = _base_raw()
    raw["upstreams"].append(
        {
            "id": "ollama_local",
            "protocol": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "api_key": "",
            "headers": {},
            "models": [{"alias": "loc_llama", "upstream_model": "llama3.1:8b"}],
        }
    )
    cfg = parse_router_config(raw)
    table = RouteTable(cfg)
    route = table.resolve("ollama", "loc_llama")
    assert route is not None
    assert route.upstream.id == "ollama_local"
    assert route.upstream_model == "llama3.1:8b"
