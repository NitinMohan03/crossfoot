"""`python -m crossfoot.mock_processor` -- what `just proc-api` runs."""

import argparse
from pathlib import Path

import uvicorn

from crossfoot.mock_processor.app import create_app
from crossfoot.mock_processor.settings import ApiSettings


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the mock payment processor API.")
    parser.add_argument("--data", type=Path, default=None, help="dataset dir (default data/full)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()

    settings = ApiSettings.from_env(data_dir=args.data)
    app = create_app(settings)
    print(f"serving {settings.data_dir} on http://{args.host}:{args.port} (token {settings.token})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
