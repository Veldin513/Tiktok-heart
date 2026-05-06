from __future__ import annotations

from yara_app.config import get_cli_profile
import yara_app.config as config


def test_get_cli_profile_ignores_test_paths_and_options() -> None:
    assert get_cli_profile(["pytest", "tests/test_worker.py"]) is None
    assert get_cli_profile(["worker", "--self-test"]) is None


def test_get_cli_profile_accepts_plain_profile_name() -> None:
    assert get_cli_profile(["worker", "test_profile"]) == "test_profile"


def test_base_dir_can_be_overridden_for_test_isolation() -> None:
    assert config.BASE_DIR.name.startswith("yara_pytest_")
