"""Compatibility entrypoint for the single manual collection worker.

The old continuous market collector is retired. This process idles without
subscribing to Deriv until a user-created collection job is available.
"""
import asyncio

from scripts.even_odd_manual_collection_worker import run_worker


def main() -> None:
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        print("Even/Odd manual collection worker stopped.")


if __name__ == "__main__":
    main()
