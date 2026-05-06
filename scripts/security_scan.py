from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    preview: str


TEXT_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".css",
    ".env",
    ".example",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".lock",
    ".md",
    ".ps1",
    ".py",
    ".rs",
    ".toml",
    ".txt",
    ".vbs",
    ".xml",
    ".yaml",
    ".yml",
}

SENSITIVE_TRACKED_PATTERNS = [
    re.compile(r"^control/(?!\.gitkeep$)(?!.*\.example\.json$).+\.json$", re.IGNORECASE),
    re.compile(r"^profiles/(?!\.gitkeep$)", re.IGNORECASE),
    re.compile(r"^logs/(?!\.gitkeep$)", re.IGNORECASE),
    re.compile(r"^backups/(?!\.gitkeep$)", re.IGNORECASE),
    re.compile(r"^release/", re.IGNORECASE),
    re.compile(r"^src-tauri/target/", re.IGNORECASE),
    re.compile(r"(^|/)node_modules/", re.IGNORECASE),
]

CONTENT_RULES = [
    ("telegram_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("windows_user_path", re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/:\s]+")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----")),
]

ALLOWLIST = [
    (".env.example", "TG_TOKEN="),
    ("control/telegram_bot_v2.example.json", "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE"),
]


def run_git(args: list[str]) -> list[str]:
    output = subprocess.check_output(["git", *args], cwd=ROOT)
    return [item.decode("utf-8", "ignore") for item in output.split(b"\0") if item]


def normalize(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if path.name in {".gitignore", ".gitattributes", "LICENSE"}:
        return True
    return False


def is_allowed(path: str, line: str) -> bool:
    return any(path == allowed_path and allowed_text in line for allowed_path, allowed_text in ALLOWLIST)


def redact(line: str) -> str:
    line = re.sub(r"\b(\d{8,12}:)[A-Za-z0-9_-]{12,}\b", r"\1***REDACTED***", line)
    line = re.sub(r"\b([A-Za-z0-9._%+-]{2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b", r"\1***\2", line)
    line = re.sub(r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)[^\\/:\s]+", r"\1***", line)
    return line.strip()[:180]


def tracked_paths() -> list[str]:
    return run_git(["ls-files", "-z"])


def untracked_not_ignored_paths() -> list[str]:
    return run_git(["ls-files", "--others", "--exclude-standard", "-z"])


def scan_path_list(paths: list[str], *, label: str) -> list[Finding]:
    findings: list[Finding] = []
    for raw_path in paths:
        path = normalize(raw_path)
        for pattern in SENSITIVE_TRACKED_PATTERNS:
            if label == "tracked" and pattern.search(path):
                findings.append(Finding(path, 0, "sensitive_tracked_path", "runtime/private path is tracked"))
                break

        full_path = ROOT / raw_path
        if not full_path.exists() or full_path.is_dir() or not is_probably_text(full_path):
            continue
        try:
            text = full_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if is_allowed(path, line):
                continue
            for rule_name, rule in CONTENT_RULES:
                if rule.search(line):
                    findings.append(Finding(path, line_number, rule_name, redact(line)))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan tracked and non-ignored files for private data.")
    parser.add_argument("--tracked-only", action="store_true", help="Scan only tracked files.")
    args = parser.parse_args()

    findings = scan_path_list(tracked_paths(), label="tracked")
    if not args.tracked_only:
        findings.extend(scan_path_list(untracked_not_ignored_paths(), label="untracked"))

    if not findings:
        print("Security scan passed: no direct private-data hits found.")
        return 0

    print("Security scan failed. Review these files before publishing:")
    for finding in findings:
        location = finding.path if finding.line <= 0 else f"{finding.path}:{finding.line}"
        print(f"- {location} [{finding.rule}] {finding.preview}")
    return 1


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
