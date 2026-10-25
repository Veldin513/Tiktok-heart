"""Validation for profile directory names shared by GUI and worker."""
from pathlib import Path


def profile_directory(base: Path, key: str) -> Path:
    if (not key or key in {'.', '..'} or key != key.strip() or key.endswith(('.', ' '))
            or any(c in key for c in '/\\:<>"|?*\x00') or any(ord(c) < 32 for c in key)
            or key.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
                                            *(f'COM{i}' for i in range(1, 10)),
                                            *(f'LPT{i}' for i in range(1, 10))}):
        raise ValueError('Недопустимое имя папки профиля.')
    root = (base / 'profiles').resolve()
    target = (root / key).resolve()
    if target.parent != root:
        raise ValueError('Папка профиля должна находиться непосредственно в profiles.')
    return target
