"""CLI entry: `pyclaego-llm-router`."""

import argparse

import uvicorn

from pyclaego.config import get_config
from pyclaego.llm_router import create_app, load_router_config


def main() -> None:
    parser = argparse.ArgumentParser(description="PyClaego LLM Router proxy")
    parser.add_argument("--host", type=str, help="bind host (overrides config)")
    parser.add_argument(
        "--port", "-p", type=int, help="bind port (overrides config)"
    )
    args = parser.parse_args()

    # Loading config eagerly surfaces validation errors before uvicorn starts.
    get_config()
    cfg = load_router_config()
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port

    print("=" * 60)
    print("PyClaego LLM Router")
    print("=" * 60)
    print(f"Listening on http://{host}:{port}")
    print(f"Upstreams: {len(cfg.upstreams)}")
    for up in cfg.upstreams:
        print(f"  - {up.id} [{up.protocol}] {up.base_url}")
        for m in up.models:
            print(f"      {m.alias}  ->  {m.upstream_model}")
    print("=" * 60)

    app = create_app(cfg)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
