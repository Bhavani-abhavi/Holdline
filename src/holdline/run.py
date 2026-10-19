"""Start the web server: `python -m holdline.run` or the `holdline` script."""

from __future__ import annotations

import os

import uvicorn

from holdline.main import create_app


def main() -> int:
    """Serve on HOLDLINE_HOST:HOLDLINE_PORT (default 127.0.0.1:8000)."""
    host = os.getenv("HOLDLINE_HOST", "127.0.0.1")
    port = int(os.getenv("HOLDLINE_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
