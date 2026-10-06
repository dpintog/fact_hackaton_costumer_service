"""Day 1 CLI: uses only the Python standard library."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from campaigns.prepare import main

if __name__ == "__main__":
    main()
