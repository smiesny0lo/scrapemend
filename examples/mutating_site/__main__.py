"""Run the demo site with command-line flags.

python -m examples.mutating_site --layout 2 --chaos flaky,ratelimit
"""

import argparse

import uvicorn

from .app import LAYOUTS, create_app
from .chaos import Mode


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m examples.mutating_site",
        description="Run the mutating demo site. Unset flags fall back to env vars.",
    )
    parser.add_argument("--layout", type=int, choices=LAYOUTS, help="page layout (default: 1)")
    parser.add_argument(
        "--chaos",
        metavar="MODES",
        help=f"comma-separated failure modes: {', '.join(Mode)}; or off (default)",
    )
    parser.add_argument(
        "--failure-rate",
        type=float,
        metavar="RATE",
        help="share of requests that fail in flaky mode, 0-1 (default: 0.2)",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    try:
        app = create_app(args.layout, args.chaos, args.failure_rate)
    except ValueError as e:
        parser.error(str(e))
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
