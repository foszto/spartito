"""`python -m orchestrator`: the `orch` command line (what the background service runs)."""

from .cli import app

if __name__ == "__main__":
    app(prog_name="orch")
