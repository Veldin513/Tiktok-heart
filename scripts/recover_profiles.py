"""Local recovery UI. Never accepts a phrase on the command line or logs it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    from yara_app.profile_keys import ProfileProtection, ProtectionError
    window = tk.Tk()
    window.title('TikTok Heart — восстановление профилей')
    window.geometry('640x430')
    window.minsize(580, 420)
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill='both', expand=True)
    root, recovery, phrase = tk.StringVar(value=str(ROOT)), tk.StringVar(), tk.StringVar()
    ttk.Label(frame, text='Восстановление доступа к профилям', font=('Segoe UI', 14, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='Потребуются ваш файл восстановления и секретная фраза.\nПрограмма работает без интернета.').pack(anchor='w', pady=(6, 14))

    def chooser(label, variable, file=False):
        ttk.Label(frame, text=label).pack(anchor='w')
        row = ttk.Frame(frame)
        row.pack(fill='x', pady=(3, 8))
        ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True)
        def select():
            selected = filedialog.askopenfilename(filetypes=[('Файл восстановления', '*.heart-recovery')]) if file else filedialog.askdirectory()
            if selected:
                variable.set(selected)
        ttk.Button(row, text='Выбрать', command=select).pack(side='left', padx=(8, 0))
    chooser('Папка TikTok Heart', root)
    chooser('Файл восстановления', recovery, file=True)
    ttk.Label(frame, text='Секретная фраза').pack(anchor='w')
    entry = ttk.Entry(frame, textvariable=phrase, show='•')
    entry.pack(fill='x', pady=(3, 12))

    def restore():
        value = phrase.get()
        phrase.set('')
        try:
            folder = Path(root.get()).resolve()
            if not (folder / 'control').is_dir() or not (folder / 'profiles').is_dir():
                raise ProtectionError('Выберите папку приложения с папками control и profiles.')
            path = Path(recovery.get())
            if path.stat().st_size > 65536:
                raise ProtectionError('Файл восстановления слишком большой.')
            package = json.loads(path.read_text(encoding='utf-8-sig'))
            # Runtime may not initialize while its key is missing. The recovery
            # operation therefore does not construct the adapter or a browser.
            ProfileProtection(folder).restore(package, value)
            messagebox.showinfo('Готово', 'Прежний ключ восстановлен. Теперь откройте TikTok Heart.\nЗашифрованные профили сохранены.')
        except ProtectionError as exc:
            messagebox.showerror('Не получилось восстановить', str(exc))
        except Exception:
            messagebox.showerror('Не получилось восстановить', 'Проверьте файл и папку приложения. Данные не изменены.')
        finally:
            value = ''
    ttk.Button(frame, text='Восстановить доступ', command=restore).pack(anchor='e')
    window.mainloop()


if __name__ == '__main__':
    main()
