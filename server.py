from __future__ import annotations

import os
from pathlib import Path

from run_server import main


if __name__ == "__main__":
    repo_root = Path(__file__).resolve().parent
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(repo_root))
    main()
