from __future__ import annotations

from pathlib import Path

SOURCE_ROOTS = ("yara_app", "app_shell", "scripts")
ROOT_FILES = (
    ".env.example",
    "BUILD_INFO.json",
    "README.md",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "requirements-desktop.txt",
)
TEXT_SUFFIXES = {".bat", ".css", ".html", ".js", ".json", ".md", ".ps1", ".py", ".toml", ".txt", ".vbs"}
MOJIBAKE_MARKERS = (
    "Рќ",
    "Рџ",
    "Р—",
    "РІС",
    "СЃ",
    "С‰",
    "вЂ",
    "В«",
    "В»",
    "Р",
    "РЅР°",
    "РґР°",
)


def iter_source_text_files(root: Path):
    for dirname in SOURCE_ROOTS:
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in path.parts):
                continue
            if path.suffix.lower() in TEXT_SUFFIXES:
                yield path
    for name in ROOT_FILES:
        path = root / name
        if path.exists():
            yield path


def test_source_text_has_no_mojibake() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in iter_source_text_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        markers = [marker for marker in MOJIBAKE_MARKERS if marker in text]
        if markers:
            rel = path.relative_to(root)
            offenders.append(f"{rel}: {', '.join(markers)}")

    assert offenders == []
