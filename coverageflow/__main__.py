"""python -m coverageflow entry point (owner: stream A2, delegates to cli.py)."""

from . cli import main

if __name__ == "__main__":
    raise SystemExit(main())
