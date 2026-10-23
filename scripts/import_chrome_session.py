from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from yara_app.project_adapter import ProjectAdapter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Import Chrome session using the same checks as the desktop app.')
    parser.add_argument('--chrome-profile', default='Default')
    parser.add_argument('--bot-profile', default='default')
    args = parser.parse_args(argv)
    try:
        result = ProjectAdapter(PROJECT_ROOT).import_google_chrome_profile(
            chrome_profile_id=args.chrome_profile, bot_profile_key=args.bot_profile, copy_mode='full_profile',
        )
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Profile data imported: {result['destination']}")
    if result.get('backup'):
        print(f"Previous profile backup: {result['backup']}")
    print('The worker will verify the session before clearing any login retry pause.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
