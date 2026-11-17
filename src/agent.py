"""Entrypoint for the LiveKit CLI, the Dockerfile and `uv run src/agent.py <mode>`.

Modes: console (local, no phone), dev, start, connect. The agent itself lives in
the shipcheck package.
"""

from shipcheck.agent import main, server  # noqa: F401  (server is discovered by tooling)

if __name__ == "__main__":
    main()
