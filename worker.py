r"""开发版授权 Worker。

运行一次：`.venv\Scripts\python.exe worker.py --once`
持续运行：`.venv\Scripts\python.exe worker.py`
"""

from __future__ import annotations

import argparse
import time

from quiz_app.config import Settings
from quiz_app.db import Database
from quiz_app.dce import build_dce_client
from quiz_app.services import QuizService


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    db = Database(settings.database_path, url=settings.database_url or None)
    db.initialize(seed_mock_dce=settings.dce_mode == "mock")
    service = QuizService(db, build_dce_client(settings, db))
    if args.once:
        print("processed" if service.process_one_grant_job("cli-worker") else "idle")
        return
    while True:
        if not service.process_one_grant_job("cli-worker"):
            time.sleep(2)


if __name__ == "__main__":
    main()
