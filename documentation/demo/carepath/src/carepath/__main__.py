"""Entry point: seed the database and serve the API.

Run it with `python -m carepath --seed`.
"""

import argparse

import uvicorn

from .app import create_app
from .seed import run as seed


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CarePath service.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--seed", action="store_true", help="Insert the demonstration cohort.")
    arguments = parser.parse_args()

    application = create_app()
    if arguments.seed:
        for label, identifier in seed().items():
            print(f"{label}: {identifier}")
    uvicorn.run(application, host=arguments.host, port=arguments.port, log_config=None)


if __name__ == "__main__":
    main()
