"""Entry point for `python -m agent_history`.

The detached trigger re-launches itself this way, which works identically on
every platform and does not depend on the console script being on PATH.
"""
from .cli import main

raise SystemExit(main())
