"""Runs only while there is queued work; no scheduled polling."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from yara_app.project_adapter import ProjectAdapter  # noqa: E402
from yara_app.redaction import RedactingFormatter  # noqa: E402
from yara_app.worker_queue import WorkerQueue  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-dir', type=Path, default=ROOT)
    args = parser.parse_args()
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter('%(asctime)s [%(levelname)s] %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    WorkerQueue(ProjectAdapter(args.base_dir)).run()


if __name__ == '__main__':
    main()
