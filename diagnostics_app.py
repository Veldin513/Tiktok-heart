from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from project_adapter import ProjectAdapter


# ──────────────────────────────────────────────────────────────────────────────
# Palette
# ──────────────────────────────────────────────────────────────────────────────
C = {
    'bg':         '#ffffff',   # window background — kept white to avoid grey gutters
    'panel':      '#0f172a',   # header background
    'surface':    '#ffffff',   # card/panel background
    'surface2':   '#f8fafc',   # secondary surface
    'surface3':   '#fdfefe',   # raised surface
    'border':     '#e2e8f0',   # card borders
    'border2':    '#cbd5e1',   # stronger border
    'muted':      '#94a3b8',   # secondary text
    'text_head':  '#f0f6ff',   # header text
    'text_main':  '#0f172a',   # main dark text
    'text_sub':   '#475569',   # secondary dark text
    'accent':     '#6366f1',   # indigo
    'accent2':    '#0ea5e9',   # sky
    'accent_bg':  '#eef2ff',   # indigo tint bg
    'ok':         '#16a34a',   # green — slightly richer
    'ok_bg':      '#dcfce7',   # green tint
    'danger':     '#dc2626',   # red
    'danger_bg':  '#fee2e2',   # red tint
    'warning':    '#d97706',   # amber — richer
    'warn_bg':    '#fef3c7',   # amber tint
    'chip_idle':  '#334155',   # dark chip — lighter so text visible
    'tab_sel':    '#6366f1',   # selected tab indicator
    'msg_pill':   '#6366f1',   # message pill bg
    'selection':  '#dbeafe',   # active selection
    'selection2': '#e2e8f0',   # inactive selection
    'scroll':     '#cbd5e1',   # scrollbar thumb
    'scroll_act': '#94a3b8',   # scrollbar hover
    'sidebar':    '#1e293b',   # services sidebar bg
    'sidebar_sel':'#334155',   # selected sidebar item
    'sidebar_txt':'#cbd5e1',   # sidebar text
    'sidebar_act':'#6366f1',   # active accent in sidebar
}

FONT_MAIN  = ('Segoe UI', 10)
FONT_BOLD  = ('Segoe UI', 10, 'bold')
FONT_SMALL = ('Segoe UI', 9)
FONT_MONO  = ('Consolas', 10)
FONT_TITLE = ('Segoe UI', 22, 'bold')
FONT_HERO  = ('Segoe UI', 16, 'bold')
FONT_CARD  = ('Segoe UI', 18, 'bold')
UI_VERSION = 'final ui v10'


def classify_log_line(line: str) -> str | None:
    """Return a semantic tag name for a log line.

    The UI uses these tags for foreground coloring in the logs viewer. The
    matcher is deliberately token-based so common words do not produce false
    positives, while standard logger output like ``[INFO]`` / ``[WARNING]`` /
    ``[ERROR]`` is recognized reliably.
    """
    low = line.lower()
    spaced = f' {low} '
    if ('[error]' in low or '[fatal]' in low or ' traceback' in spaced or
            ' exception' in spaced or 'ошибка' in low):
        return 'error'
    if '[warning]' in low or ' предупрежд' in low:
        return 'warning'
    if '✅' in line or '[success]' in low or 'успех' in low:
        return 'success'
    if '[debug]' in low:
        return 'debug'
    if '[info]' in low:
        return 'info'
    return None


def resolve_initial_tab_index(saved_index: object, tab_count: int, *, restore_last_tab: bool = False) -> int:
    """Return the tab index to open on startup.

    By default the app opens the Overview tab. Restoring the last tab is
    opt-in because the dashboard is the safest landing page after restart.
    """
    if tab_count <= 0 or not restore_last_tab:
        return 0
    try:
        index = int(saved_index or 0)
    except Exception:
        index = 0
    return max(0, min(index, tab_count - 1))


LOG_FILTER_OPTIONS = ('all', 'errors', 'warnings', 'success', 'important')


def normalize_log_filter_mode(value: object) -> str:
    """Map UI aliases and unexpected values to a supported log filter mode."""
    raw = str(value or 'all').strip().lower()
    aliases = {
        'all': 'all',
        'all logs': 'all',
        'errors': 'errors',
        'error': 'errors',
        'warnings': 'warnings',
        'warning': 'warnings',
        'success': 'success',
        'successes': 'success',
        'ok': 'success',
        'important': 'important',
        'important only': 'important',
        'critical': 'important',
    }
    mode = aliases.get(raw, raw)
    return mode if mode in LOG_FILTER_OPTIONS else 'all'


def resolve_overview_layout(width: int, height: int) -> dict[str, bool]:
    """Return responsive layout flags for the Overview dashboard.

    The Overview should keep all helper cards visible on a normal 1260×800
    window. Collapsing is reserved for genuinely low-height layouts after the
    header and toolbars have already consumed part of the window.
    """
    safe_width = max(0, int(width or 0))
    safe_height = max(0, int(height or 0))
    return {
        'stack_side_cards': safe_width < 1120,
        'collapse_recommendations': safe_height < 500,
        'collapse_activity': safe_height < 440,
        'compact_metrics': safe_height < 600,
    }


LOG_OPTIONS = [
    ('worker_stdout', 'Worker stdout'),
    ('log', 'Worker app log'),
    ('auth_debug', 'Auth debug'),
    ('launcher_log', 'Launcher'),
    ('telegram_log', 'Telegram bot'),
]


class UnifiedScrolledText(tk.Text):
    """Text widget with unified ttk scrollbars and proxied geometry methods."""

    def __init__(self, master=None, *, horizontal: bool | None = None, **kw):
        self.frame = ttk.Frame(master, style='Surface.TFrame')
        self.vbar = ttk.Scrollbar(self.frame, orient=tk.VERTICAL, style='App.Vertical.TScrollbar')
        self.vbar.pack(side=tk.RIGHT, fill=tk.Y)
        wrap = kw.get('wrap', tk.CHAR)
        if horizontal is None:
            horizontal = wrap in (tk.NONE, 'none')
        self.hbar = None
        if horizontal:
            self.hbar = ttk.Scrollbar(self.frame, orient=tk.HORIZONTAL, style='App.Horizontal.TScrollbar')
            self.hbar.pack(side=tk.BOTTOM, fill=tk.X)
            kw.update({'xscrollcommand': self.hbar.set})

        kw.update({'yscrollcommand': self.vbar.set})
        super().__init__(self.frame, **kw)
        self.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vbar.configure(command=self.yview)
        if self.hbar is not None:
            self.hbar.configure(command=self.xview)

        text_meths = vars(tk.Text).keys()
        methods = vars(tk.Pack).keys() | vars(tk.Grid).keys() | vars(tk.Place).keys()
        methods = methods.difference(text_meths)
        for m in methods:
            if m and m[0] != '_' and m not in {'config', 'configure'}:
                setattr(self, m, getattr(self.frame, m))

    def __str__(self):
        return str(self.frame)


class DiagnosticsApp(ttk.Frame):
    REFRESH_INTERVAL_MS = 4000
    REFRESH_INTERVAL_IDLE_MS = 9000
    REFRESH_TIMEOUT_MS = 18000
    ACTIVE_ANIMATION_MS = 70
    IDLE_ANIMATION_MS = 260
    IDLE_THRESHOLD_SEC = 120

    def __init__(
        self,
        master: tk.Tk,
        base_dir: Path | str | None = None,
        startup_errors: list[str] | None = None,
        startup_warnings: list[str] | None = None,
        on_minimize_to_tray=None,
        on_exit_application=None,
    ) -> None:
        super().__init__(master, padding=0)
        self.master = master
        self.adapter = ProjectAdapter(base_dir)
        self.adapter.ensure_runtime_files()
        self.startup_errors  = list(startup_errors  or [])
        self.startup_warnings = list(startup_warnings or [])
        self.on_minimize_to_tray = on_minimize_to_tray
        self.on_exit_application = on_exit_application
        # keep for callers that reference self.colors
        self.colors = C

        # ── state vars ────────────────────────────────────────────────────────
        self.status_var          = tk.StringVar(value='Инициализация…')
        self.last_refresh_var    = tk.StringVar(value='—')
        self.auto_refresh_var    = tk.BooleanVar(value=True)
        self.log_autoscroll_var  = tk.BooleanVar(value=True)
        self.selected_log_var    = tk.StringVar(value='worker_stdout')
        self.log_combo_var       = tk.StringVar(value='Worker stdout')
        self.log_filter_var      = tk.StringVar(value='all')
        self.log_search_var      = tk.StringVar(value='')
        self.log_line_limit_var  = tk.IntVar(value=160)
        self.hero_subtitle_var   = tk.StringVar(value='Инициализация…')
        self.message_details_var = tk.StringVar(value='—')
        self.targets_meta_var    = tk.StringVar(value='Список адресатов готовится…')
        self.health_score_var    = tk.StringVar(value='0')
        self.health_summary_var  = tk.StringVar(value='—')
        self.last_success_var    = tk.StringVar(value='—')
        self.last_error_var      = tk.StringVar(value='—')
        self.current_time_var    = tk.StringVar(value='—')
        self.refresh_eta_var     = tk.StringVar(value='Обновление: вручную')
        self.active_tab_var      = tk.StringVar(value='Раздел: Обзор')
        self.message_editor_meta_var = tk.StringVar(value='Строк: 0  ·  Символов: 0')
        self.message_editor_state_var = tk.StringVar(value='Состояние: сохранено')
        self.message_backup_var  = tk.StringVar(value='Backup: —')
        self.compact_mode_var    = tk.BooleanVar(value=True)
        self.profile_hint_var    = tk.StringVar(value='Загружаю профили…')
        self.files_hint_var      = tk.StringVar(value='Загружаю список файлов…')
        self.logs_hint_var       = tk.StringVar(value='')
        self._log_quick_filter_buttons: dict[str, tk.Button] = {}
        self.log_chip_vars = {
            'errors': tk.StringVar(value='Ошибок: 0'),
            'warnings': tk.StringVar(value='Предупреждений: 0'),
            'successes': tk.StringVar(value='Успехов: 0'),
            'last_error': tk.StringVar(value='Последняя ошибка: —'),
        }
        self.signal_chip_vars = {
            'critical': tk.StringVar(value='critical: 0'),
            'warning': tk.StringVar(value='warning: 0'),
            'info': tk.StringVar(value='info: 0'),
            'ok': tk.StringVar(value='ok: 0'),
        }

        # ── internal state ────────────────────────────────────────────────────
        self._tab_widgets: dict[str, tk.Widget] = {}
        self._refresh_after_id: str | None = None
        self._refresh_timeout_id: str | None = None
        self._refresh_in_flight = False
        self._refresh_generation = 0
        self._last_refresh_started_monotonic = 0.0
        self._last_user_activity_monotonic = time.monotonic()
        self._action_history: list[str] = []
        self._action_buttons: list[ttk.Button] = []
        self._action_busy    = False
        self._last_diag: dict[str, object] = {}
        self._message_dirty  = False
        self._file_notes_by_path: dict[str, str] = {}
        self.target_search_var = tk.StringVar(value='')
        self._target_sort_column = 'idx'
        self._target_sort_desc = False
        self._profile_target_rows: list[dict[str, object]] = []
        self._ui_state_path = self.adapter.base_dir / 'control' / 'ui_state.json'
        self._build_info = self._load_build_info()
        self._pulse_phase = 0
        self._anim_clock = 0.0
        self._metric_bar_targets: dict[object, int] = {}
        self._text_widgets: list[tk.Text] = []
        self._treeviews: list[ttk.Treeview] = []
        self._toast_windows: list[tk.Toplevel] = []
        self._animated_bg_targets: dict[tk.Widget, str] = {}
        self._overview_layout_state: dict[str, bool] = {}
        self._saved_status_base = C['ok']
        self._load_ui_state()
        self.compact_mode_var.set(True)

        self.master.title('TikTok Heart Bot — Control Center')
        self._bind_activity_watchers()
        self.master.geometry('1260x800')
        self.master.minsize(1080, 700)
        self._configure_style()
        self.configure(style='Root.TFrame')
        self.pack(fill=tk.BOTH, expand=True)
        self._build_ui()
        self._collect_theme_targets()
        self._apply_unified_widget_theme()
        self._bind_shortcuts()
        self._apply_density_mode()
        self._restore_selected_tab()
        self._update_tab_context()
        self._tick_clock()
        self._animate_ui()
        self._log_action('Приложение запущено')
        self.refresh_all(initial=True)
        self._schedule_refresh()

    # ══════════════════════════════════════════════════════════════════════════
    # Style
    # ══════════════════════════════════════════════════════════════════════════
    def _configure_style(self) -> None:
        self.master.configure(bg=C['bg'])
        s = ttk.Style(self.master)
        try:
            s.theme_use('clam')
        except tk.TclError:
            pass

        s.configure('Root.TFrame',       background=C['bg'])
        s.configure('Surface.TFrame',    background=C['surface'])
        s.configure('TFrame',            background=C['surface'])
        s.configure('Surface.TLabelframe',
                    background=C['surface'], foreground=C['text_main'])
        s.configure('Surface.TLabelframe.Label',
                    background=C['surface'], foreground=C['text_main'],
                    font=FONT_BOLD)
        # Spinbox — white background
        s.configure('TSpinbox', fieldbackground=C['surface'],
                    background=C['surface'], foreground=C['text_main'],
                    arrowcolor=C['text_sub'])
        # Checkbutton — no grey bg
        s.configure('TCheckbutton', background=C['surface'],
                    foreground=C['text_main'])
        s.map('TCheckbutton', background=[('active', C['surface'])])
        # Entry / Combobox — keep white even in readonly state
        s.configure('TEntry', fieldbackground=C['surface'], background=C['surface'],
                    foreground=C['text_main'], insertcolor=C['text_main'])
        s.map('TEntry', fieldbackground=[('readonly', C['surface']), ('disabled', C['surface2'])])
        s.configure('TCombobox', fieldbackground=C['surface'],
                    background=C['surface'], foreground=C['text_main'],
                    arrowcolor=C['text_sub'], selectbackground=C['surface'],
                    selectforeground=C['text_main'])
        s.map('TCombobox',
              fieldbackground=[('readonly', C['surface']), ('disabled', C['surface2'])],
              background=[('readonly', C['surface']), ('disabled', C['surface2'])],
              foreground=[('readonly', C['text_main']), ('disabled', C['muted'])],
              selectbackground=[('readonly', C['surface'])],
              selectforeground=[('readonly', C['text_main'])],
              arrowcolor=[('readonly', C['text_sub'])])
        # Scrollbars — unified across tabs/text/tree views
        for style_name in ('TScrollbar', 'App.Vertical.TScrollbar', 'App.Horizontal.TScrollbar'):
            s.configure(style_name, background=C['scroll'], troughcolor=C['surface2'],
                        arrowcolor=C['text_sub'], bordercolor=C['border'],
                        lightcolor=C['scroll'], darkcolor=C['scroll'],
                        relief='flat', gripcount=0, arrowsize=11)
            s.map(style_name, background=[('active', C['scroll_act']), ('pressed', C['scroll_act'])])
        # Separator
        s.configure('TSeparator', background=C['border'])

        s.configure('Head.TLabel',   background=C['surface'], foreground=C['text_main'], font=FONT_BOLD)
        s.configure('Sub.TLabel',    background=C['surface'], foreground=C['text_sub'],  font=FONT_SMALL)
        s.configure('Hint.TLabel',   background=C['surface'], foreground=C['muted'],     font=FONT_SMALL)
        s.configure('BigVal.TLabel', background=C['surface'], foreground=C['text_main'], font=FONT_CARD)

        s.configure('Primary.TButton', font=FONT_BOLD, padding=(12, 7),
                    background=C['accent'], foreground='white',
                    relief='flat', borderwidth=0)
        s.map('Primary.TButton',
              background=[('active', '#4f46e5'), ('pressed', '#4338ca')],
              foreground=[('active', 'white'), ('pressed', 'white')])
        # Regular ttk buttons — white bg, clean border
        s.configure('TButton', font=FONT_SMALL, padding=(8, 5),
                    background=C['surface'], foreground=C['text_main'],
                    relief='flat', borderwidth=1, bordercolor=C['border2'])
        s.map('TButton',
              background=[('active', C['accent_bg']), ('pressed', C['accent_bg'])],
              foreground=[('active', C['accent']),    ('pressed', C['accent'])],
              bordercolor=[('active', C['accent']),   ('pressed', C['accent'])],
              relief=[('pressed', 'flat')])

        # tab styling
        s.configure('TNotebook', background=C['bg'], borderwidth=0, tabmargins=[0,0,0,0])
        s.configure('TNotebook.Tab', background=C['surface2'],
                    foreground=C['text_sub'],
                    padding=(18, 9), font=FONT_BOLD)
        s.map('TNotebook.Tab',
              background=[('selected', C['surface'])],
              foreground=[('selected', C['accent'])],
              expand=[('selected', [0, 0, 0, 1])])
        s.configure('Inner.TNotebook', background=C['bg'], borderwidth=0, tabmargins=[0, 0, 0, 0])
        s.configure('Inner.TNotebook.Tab', background=C['surface2'], foreground=C['text_sub'],
                    padding=(8, 5), font=('Segoe UI', 9, 'bold'))
        s.map('Inner.TNotebook.Tab',
              background=[('selected', C['surface'])],
              foreground=[('selected', C['accent'])])

        s.configure('Treeview', rowheight=26, font=FONT_SMALL,
                    background=C['surface'], fieldbackground=C['surface'],
                    foreground=C['text_main'], borderwidth=0, relief='flat')
        s.configure('Treeview.Heading', font=FONT_BOLD, background='#f3f6fb',
                    foreground=C['text_sub'], relief='flat', borderwidth=0,
                    padding=(8, 6))
        s.map('Treeview', background=[('selected', C['selection'])],
              foreground=[('selected', C['text_main'])])
        s.map('Treeview.Heading', background=[('active', '#e8eef7')])

        # Progressbar styles — must define layout explicitly for Python 3.14
        for pb_name, pb_color in (
            ('TProgressbar',       C['accent']),
            ('Green.TProgressbar', C['ok']),
            ('Amber.TProgressbar', C['warning']),
            ('Red.TProgressbar',   C['danger']),
        ):
            s.configure(pb_name, troughcolor=C['border'],
                        background=pb_color, bordercolor=C['border'],
                        thickness=8)
            # Register Horizontal layout so Python 3.14 can find it
            horiz = f'Horizontal.{pb_name}'
            try:
                s.layout(horiz, [
                    ('Horizontal.Progressbar.trough', {
                        'children': [('Horizontal.Progressbar.pbar',
                                      {'side': 'left', 'sticky': 'ns'})],
                        'sticky': 'nswe'})])
                s.configure(horiz, troughcolor=C['border'],
                            background=pb_color, bordercolor=C['border'],
                            thickness=8)
            except Exception:
                pass



    def _blend_color(self, start: str, end: str, factor: float) -> str:
        start = start.lstrip('#')
        end = end.lstrip('#')
        if len(start) != 6 or len(end) != 6:
            return '#' + end
        factor = max(0.0, min(1.0, factor))
        parts = []
        for i in (0, 2, 4):
            s = int(start[i:i+2], 16)
            e = int(end[i:i+2], 16)
            parts.append(int(s + (e - s) * factor))
        return '#' + ''.join(f'{v:02x}' for v in parts)

    def _style_scrollbar_widget(self, widget: tk.Scrollbar) -> None:
        try:
            widget.configure(
                bg=C['scroll'], activebackground=C['scroll_act'],
                troughcolor=C['bg'], relief=tk.FLAT, bd=0,
                highlightthickness=0, width=12, elementborderwidth=0,
                borderwidth=0, arrowcolor=C['text_sub'],
            )
        except Exception:
            try:
                widget.configure(
                    bg=C['scroll'], activebackground=C['scroll_act'],
                    troughcolor=C['bg'], relief=tk.FLAT, bd=0,
                    highlightthickness=0, width=12,
                )
            except Exception:
                pass

    def _style_text_widget(self, widget: tk.Text) -> None:
        try:
            widget.configure(
                bg=C['surface3'], fg=C['text_main'],
                insertbackground=C['accent'], insertwidth=1,
                selectbackground='#cfe3ff',
                selectforeground=C['text_main'],
                inactiveselectbackground=C['surface3'],
                relief=tk.FLAT, bd=0, highlightthickness=1,
                highlightbackground=C['border'], highlightcolor=C['accent'],
                padx=10, pady=8, spacing1=1, spacing3=1,
                exportselection=False, undo=False,
            )
        except Exception:
            pass
        for child in widget.winfo_children():
            if isinstance(child, tk.Scrollbar):
                self._style_scrollbar_widget(child)

    def _collect_theme_targets(self) -> None:
        self._text_widgets = []
        self._treeviews = []
        def walk(widget):
            try:
                if isinstance(widget, tk.Text):
                    self._text_widgets.append(widget)
                elif isinstance(widget, ttk.Treeview):
                    self._treeviews.append(widget)
                elif isinstance(widget, tk.Scrollbar):
                    self._style_scrollbar_widget(widget)
            except Exception:
                pass
            for child in widget.winfo_children():
                walk(child)
        walk(self)

    def _apply_unified_widget_theme(self) -> None:
        for widget in self._text_widgets:
            self._style_text_widget(widget)
        for tree in self._treeviews:
            try:
                tree.configure(style='Treeview')
                tree.tag_configure('row_even', background=C['surface3'])
                tree.tag_configure('row_odd', background=C['surface2'])
            except Exception:
                pass
        self._restyle_treeviews()

    def _restyle_treeviews(self) -> None:
        for tree in list(getattr(self, '_treeviews', [])):
            try:
                rows = list(tree.get_children(''))
            except Exception:
                continue
            for index, iid in enumerate(rows):
                try:
                    current = list(tree.item(iid, 'tags'))
                except Exception:
                    current = []
                current = [tag for tag in current if tag not in {'row_even', 'row_odd'}]
                current.append('row_even' if index % 2 == 0 else 'row_odd')
                try:
                    tree.item(iid, tags=tuple(current))
                except Exception:
                    pass

    def _load_build_info(self) -> dict[str, object]:
        path = self.adapter.base_dir / 'BUILD_INFO.json'
        try:
            return dict(json.loads(path.read_text(encoding='utf-8')))
        except Exception:
            return {}

    def _load_ui_state(self) -> None:
        try:
            payload = json.loads(self._ui_state_path.read_text(encoding='utf-8'))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        try:
            self.auto_refresh_var.set(bool(payload.get('auto_refresh', self.auto_refresh_var.get())))
            self.selected_log_var.set(str(payload.get('selected_log', self.selected_log_var.get())))
            self.log_combo_var.set(dict(LOG_OPTIONS).get(self.selected_log_var.get(), 'Worker stdout'))
            self.log_filter_var.set(normalize_log_filter_mode(payload.get('log_filter', self.log_filter_var.get())))
            self.log_search_var.set(str(payload.get('log_search', self.log_search_var.get())))
            self.log_line_limit_var.set(int(payload.get('log_line_limit', self.log_line_limit_var.get())))
            self.target_search_var.set(str(payload.get('target_search', self.target_search_var.get())))
            self.compact_mode_var.set(True)
            self._restore_last_tab = bool(payload.get('restore_last_tab', False))
            self._saved_tab_index = int(payload.get('selected_tab', 0) or 0)
        except Exception:
            self.compact_mode_var.set(True)
            self._restore_last_tab = False
            self._saved_tab_index = 0

    def _save_ui_state(self) -> None:
        try:
            self._ui_state_path.parent.mkdir(parents=True, exist_ok=True)
            tab_index = 0
            if getattr(self, 'tabs', None) is not None:
                try:
                    tab_index = int(self.tabs.index(self.tabs.select()))
                except Exception:
                    tab_index = 0
            payload = {
                'auto_refresh': bool(self.auto_refresh_var.get()),
                'selected_log': self.selected_log_var.get(),
                'log_filter': self.log_filter_var.get(),
                'log_search': self.log_search_var.get(),
                'log_line_limit': int(self.log_line_limit_var.get() or 160),
                'target_search': self.target_search_var.get(),
                'compact_mode': True,
                'restore_last_tab': False,
                'selected_tab': tab_index,
            }
            self._ui_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _restore_selected_tab(self) -> None:
        index = resolve_initial_tab_index(
            getattr(self, '_saved_tab_index', 0),
            len(self.tabs.tabs()),
            restore_last_tab=bool(getattr(self, '_restore_last_tab', False)),
        )
        try:
            self.tabs.select(index)
        except Exception:
            pass

    def _toggle_compact_mode(self) -> None:
        self.compact_mode_var.set(True)
        self._apply_density_mode()
        self._save_ui_state()

    def _apply_density_mode(self) -> None:
        self.compact_mode_var.set(True)
        style = ttk.Style(self.master)
        style.configure('Treeview', rowheight=24)
        style.configure('TNotebook.Tab', padding=(16, 7))
        style.configure('TButton', padding=(7, 4))
        style.configure('Primary.TButton', padding=(10, 6))
        self.set_status('Вид: компактный режим') if getattr(self, 'status_var', None) else None

    def _build_ui(self) -> None:
        self.configure(style='Root.TFrame')
        self._build_menu()
        self._build_header()
        self._build_quick_bar()
        self._build_tabs()
        self._build_status_bar()


    # ══════════════════════════════════════════════════════════════════════════
    # Menu / shortcuts / status tickers
    # ══════════════════════════════════════════════════════════════════════════
    def _build_menu(self) -> None:
        menubar = tk.Menu(self.master)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label='Обновить	F5', command=self.refresh_all)
        file_menu.add_command(label='Копировать содержимое раздела	Ctrl+Shift+C', command=self.copy_current_tab)
        file_menu.add_separator()
        file_menu.add_command(label='Открыть проект', command=lambda: self.open_path(self.adapter.base_dir))
        file_menu.add_command(label='Открыть логи профиля', command=lambda: self.open_path(self.adapter.get_active_profile_logs_dir()))
        file_menu.add_command(label='Открыть общие логи', command=lambda: self.open_path(self.adapter.get_common_logs_dir()))
        file_menu.add_command(label='Открыть backups', command=self.open_backups_folder)
        file_menu.add_separator()
        file_menu.add_command(label='Экспорт диагностики в JSON	Ctrl+E', command=self.export_diagnostics_json)
        file_menu.add_command(label='Экспорт диагностики в TXT', command=self.export_diagnostics_text)
        if self.on_exit_application:
            file_menu.add_separator()
            file_menu.add_command(label='Закрыть приложение	Ctrl+Shift+Q', command=self.request_exit_application)
        menubar.add_cascade(label='Файл', menu=file_menu)

        actions_menu = tk.Menu(menubar, tearoff=False)
        actions_menu.add_command(label='Запустить всё	Ctrl+R', command=self.start_all)
        actions_menu.add_command(label='Остановить всё	Ctrl+.', command=self.stop_all)
        actions_menu.add_command(label='Перезапустить всё	Ctrl+Shift+R', command=self.restart_all)
        actions_menu.add_separator()
        actions_menu.add_command(label='Запустить worker', command=self.start_worker)
        actions_menu.add_command(label='Остановить worker', command=self.stop_worker)
        actions_menu.add_command(label='Перезапустить worker', command=self.restart_worker)
        actions_menu.add_separator()
        actions_menu.add_command(label='Запустить Telegram', command=self.start_telegram_bot)
        actions_menu.add_command(label='Остановить Telegram', command=self.stop_telegram_bot)
        actions_menu.add_command(label='Перезапустить Telegram', command=self.restart_telegram_bot)
        menubar.add_cascade(label='Действия', menu=actions_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        for idx, title in enumerate(('Обзор', 'Сервисы', 'Сообщения', 'Профили', 'Логи', 'Диагностика'), start=1):
            view_menu.add_command(label=f'{title}	Ctrl+{idx}', command=lambda i=idx - 1: self._select_tab_index(i))
        view_menu.add_separator()
        view_menu.add_command(label='Компактная плотность	Ctrl+-', command=self._toggle_compact_mode)
        view_menu.add_command(label='Фокус: поиск по логам	Ctrl+L', command=self._focus_log_search)
        menubar.add_cascade(label='Вид', menu=view_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label='Горячие клавиши', command=self._show_shortcuts_dialog)
        help_menu.add_command(label='О приложении', command=self._show_about_dialog)
        menubar.add_cascade(label='Помощь', menu=help_menu)

        self.master.configure(menu=menubar)
        self._menubar = menubar

    def _bind_shortcuts(self) -> None:
        bindings = {
            '<F5>': lambda _e: self.refresh_all() or 'break',
            '<Control-Shift-C>': lambda _e: self.copy_current_tab() or 'break',
            '<Control-e>': lambda _e: self.export_diagnostics_json() or 'break',
            '<Control-l>': lambda _e: self._focus_log_search() or 'break',
            '<Control-r>': lambda _e: self.start_all() or 'break',
            '<Control-R>': lambda _e: self.restart_all() or 'break',
            '<Control-period>': lambda _e: self.stop_all() or 'break',
            '<Control-minus>': lambda _e: (self._toggle_compact_mode(), 'break')[-1],
        }
        if self.on_exit_application:
            bindings['<Control-Shift-q>'] = lambda _e: self.request_exit_application() or 'break'
            bindings['<Control-Shift-Q>'] = lambda _e: self.request_exit_application() or 'break'
        for seq, handler in bindings.items():
            self.master.bind_all(seq, handler)
        for idx in range(1, 7):
            self.master.bind_all(f'<Control-Key-{idx}>', lambda _e, i=idx - 1: self._select_tab_index(i) or 'break')

    def _select_tab_index(self, index: int) -> None:
        try:
            self.tabs.select(index)
            self._update_tab_context()
            self._save_ui_state()
        except Exception:
            pass

    def _focus_log_search(self) -> None:
        try:
            self.tabs.select(self.logs_tab)
            self._update_tab_context()
            self.log_search_entry.focus_set()
            self.log_search_entry.selection_range(0, tk.END)
        except Exception:
            pass

    def _show_shortcuts_dialog(self) -> None:
        lines = [
            'F5 — обновить данные',
            'Ctrl+1..6 — переключение вкладок',
            'Ctrl+Shift+C — копировать содержимое текущего раздела',
            'Ctrl+E — экспорт диагностики в JSON',
            'Ctrl+L — перейти к поиску по логам',
            'Ctrl+R — запустить все сервисы',
            'Ctrl+Shift+R — перезапустить все сервисы',
            'Ctrl+. — остановить все сервисы',
            'Ctrl+- — применить компактную плотность',
            *((['Ctrl+Shift+Q — закрыть приложение без сворачивания в трей']) if self.on_exit_application else []),
        ]
        messagebox.showinfo('Горячие клавиши', '\n'.join(lines))

    def _show_about_dialog(self) -> None:
        build_name = self._build_info.get('build') or 'unknown'
        notes = list(self._build_info.get('notes') or [])
        details = [
            'TikTok Heart Bot — Control Center',
            '',
            f'Папка проекта: {self.adapter.base_dir}',
            f'Версия UI: {UI_VERSION}',
            f'Сборка проекта: {build_name}',
            '',
            'Финальные улучшения интерфейса:',
            '• упрощённая верхняя панель без перегруза кнопками',
            '• явное переключение LIVE / DRY RUN отдельной кнопкой',
            '• улучшенный обзор с заполнением текущего прогона и событий',
            '• всплывающие уведомления и заметная пульсация статуса',
            '• компактный режим по умолчанию и более спокойная визуальная палитра',
        ]
        if notes:
            details.extend(['', 'Заметки сборки:'])
            details.extend([f'• {item}' for item in notes[:4]])
        messagebox.showinfo('О приложении', '\n'.join(details))

    def _on_tab_changed(self, _event=None) -> None:
        self._update_tab_context()

    def _update_tab_context(self) -> None:
        try:
            tab_id = self.tabs.select()
            text = self.tabs.tab(tab_id, 'text')
            for token in ('📊', '⚙️', '💬', '👤', '📋', '🔍'):
                text = text.replace(token, '')
            self.active_tab_var.set(f'Раздел: {text.strip()}')
            self._save_ui_state()
        except Exception:
            self.active_tab_var.set('Раздел: —')

    def _tick_clock(self) -> None:
        now = datetime.now()
        self.current_time_var.set(now.strftime('%d.%m.%Y %H:%M:%S'))
        if self.auto_refresh_var.get() and self._refresh_after_id:
            try:
                remaining_ms = max(0, int(self.tk.call('after', 'info', self._refresh_after_id)[0]))
            except Exception:
                remaining_ms = self.REFRESH_INTERVAL_MS
            self.refresh_eta_var.set(f'Автообновление через {max(1, remaining_ms // 1000)}с')
        elif self.auto_refresh_var.get():
            self.refresh_eta_var.set('Автообновление активно')
        else:
            self.refresh_eta_var.set('Обновление: вручную')
        self.after(1000, self._tick_clock)

    def _make_pill(self, parent: tk.Widget, textvariable: tk.StringVar, bg: str, fg: str = 'white') -> tk.Label:
        return tk.Label(parent, textvariable=textvariable, bg=bg, fg=fg,
                        padx=10, pady=4, font=('Segoe UI', 9, 'bold'))

    def _bind_activity_watchers(self) -> None:
        for sequence in ('<Any-KeyPress>', '<Any-ButtonPress>', '<MouseWheel>', '<FocusIn>'):
            try:
                self.master.bind_all(sequence, self._mark_user_activity, add='+')
            except Exception:
                pass

    def _mark_user_activity(self, _event=None) -> None:
        self._last_user_activity_monotonic = time.monotonic()

    def _is_ui_idle(self) -> bool:
        return (time.monotonic() - self._last_user_activity_monotonic) >= self.IDLE_THRESHOLD_SEC

    def _current_refresh_interval_ms(self) -> int:
        return self.REFRESH_INTERVAL_IDLE_MS if self._is_ui_idle() else self.REFRESH_INTERVAL_MS

    def _current_animation_interval_ms(self) -> int:
        try:
            if not bool(self.master.winfo_viewable()):
                return max(900, self.IDLE_ANIMATION_MS)
        except Exception:
            pass
        return self.IDLE_ANIMATION_MS if self._is_ui_idle() else self.ACTIVE_ANIMATION_MS

    def _cancel_refresh_timeout(self) -> None:
        if self._refresh_timeout_id:
            try:
                self.after_cancel(self._refresh_timeout_id)
            except Exception:
                pass
            self._refresh_timeout_id = None

    def _schedule_refresh_timeout(self, generation: int) -> None:
        self._cancel_refresh_timeout()
        self._refresh_timeout_id = self.after(
            self.REFRESH_TIMEOUT_MS,
            lambda g=generation: self._on_refresh_timeout(g),
        )

    def _on_refresh_timeout(self, generation: int) -> None:
        self._refresh_timeout_id = None
        if generation != self._refresh_generation or not self._refresh_in_flight:
            return
        self._refresh_in_flight = False
        msg = 'Обновление зависло дольше 18 сек. Следующий цикл разблокирован.'
        self.set_status(msg)
        self._log_action(msg)

    def _toolbar_button(self, parent: tk.Widget, text: str, command, *, width: int) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=C['surface'],
            fg=C['text_main'],
            activebackground=C['accent_bg'],
            activeforeground=C['accent'],
            relief='flat',
            bd=0,
            highlightthickness=0,
            padx=6,
            pady=4,
            cursor='hand2',
            anchor='center',
            font=FONT_SMALL,
        )

    def _toolbar_action(self, parent: tk.Widget, icon: str, text: str, command, *, width: int) -> tk.Frame:
        visual_width = max(width, len(text) + 4)
        frame = tk.Frame(
            parent,
            bg=C['surface'],
            highlightthickness=1,
            highlightbackground=C['border'],
            bd=0,
            cursor='hand2',
            height=30,
            width=max(120, visual_width * 9 + 28),
        )
        frame.pack_propagate(False)
        frame.grid_propagate(False)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        icon_label = tk.Label(
            frame,
            text=icon,
            bg=C['surface'],
            fg=C['text_main'],
            font=('Segoe UI Emoji', 12),
            cursor='hand2',
            width=2,
            anchor='center',
        )
        icon_label.grid(row=0, column=0, sticky='nsw', padx=(8, 6))

        text_label = tk.Label(
            frame,
            text=text,
            bg=C['surface'],
            fg=C['text_main'],
            font=FONT_SMALL,
            cursor='hand2',
            anchor='w',
        )
        text_label.grid(row=0, column=1, sticky='ew', padx=(0, 10))

        def _run(_event=None):
            command()
            return 'break'

        def _paint(bg: str, fg: str = C['text_main'], border: str | None = None) -> None:
            frame.configure(bg=bg, highlightbackground=border or C['border'])
            text_label.configure(bg=bg, fg=fg)
            icon_label.configure(bg=bg, fg=fg)

        def _enter(_event=None):
            _paint(C['surface3'], C['accent'], C['accent'])

        def _leave(_event=None):
            _paint(C['surface'], C['text_main'], C['border'])

        for widget in (frame, icon_label, text_label):
            widget.bind('<Button-1>', _run)
            widget.bind('<Enter>', _enter)
            widget.bind('<Leave>', _leave)

        return frame

    def _toolbar_dropdown(self, parent: tk.Widget, textvariable: tk.StringVar,
                          values: list[str], command=None, *, width: int = 18) -> tk.Frame:
        outer = tk.Frame(
            parent,
            bg=C['surface'],
            highlightthickness=1,
            highlightbackground=C['border2'],
            bd=0,
            cursor='hand2',
            height=30,
            width=max(120, width * 8 + 24),
        )
        outer.pack_propagate(False)
        outer.grid_propagate(False)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        value_label = tk.Label(
            outer,
            textvariable=textvariable,
            bg=C['surface3'],
            fg=C['text_main'],
            font=FONT_SMALL,
            anchor='w',
            padx=10,
            cursor='hand2',
        )
        value_label.grid(row=0, column=0, sticky='nsew')

        sep = tk.Frame(outer, bg=C['border'], width=1)
        sep.grid(row=0, column=1, sticky='ns')

        arrow = tk.Label(
            outer,
            text='▾',
            bg=C['surface2'],
            fg=C['text_sub'],
            font=('Segoe UI Symbol', 10, 'bold'),
            width=3,
            cursor='hand2',
        )
        arrow.grid(row=0, column=2, sticky='nsew')

        menu = tk.Menu(outer, tearoff=False)
        for value in values:
            def _choose(v=value):
                textvariable.set(v)
                if command is not None:
                    command(v)
            menu.add_command(label=value, command=_choose)

        def _post(_event=None):
            menu.tk_popup(outer.winfo_rootx(), outer.winfo_rooty() + outer.winfo_height())
            return 'break'

        def _paint(bg: str, border: str, fg: str, arrow_bg: str) -> None:
            outer.configure(bg=bg, highlightbackground=border)
            value_label.configure(bg=bg, fg=fg)
            sep.configure(bg=border)
            arrow.configure(bg=arrow_bg, fg=fg)

        def _enter(_event=None):
            _paint('#ffffff', C['accent'], C['accent'], C['accent_bg'])

        def _leave(_event=None):
            _paint(C['surface3'], C['border2'], C['text_main'], C['surface2'])

        for widget in (outer, value_label, sep, arrow):
            widget.bind('<Button-1>', _post)
            widget.bind('<Enter>', _enter)
            widget.bind('<Leave>', _leave)

        return outer

    def _quick_filter_button(self, parent: tk.Widget, text: str, mode: str) -> tk.Button:
        button = tk.Button(
            parent,
            text=text,
            command=lambda m=mode: self.set_log_filter_mode(m),
            bg=C['surface'],
            fg=C['text_sub'],
            activebackground=C['accent_bg'],
            activeforeground=C['accent'],
            relief='flat',
            bd=0,
            highlightthickness=1,
            highlightbackground=C['border'],
            highlightcolor=C['accent'],
            padx=8,
            pady=3,
            cursor='hand2',
            font=('Segoe UI', 8, 'bold'),
        )
        self._log_quick_filter_buttons[mode] = button
        return button

    def _refresh_log_quick_filters(self) -> None:
        active = normalize_log_filter_mode(self.log_filter_var.get())
        palette = {
            'all': (C['surface'], C['text_sub']),
            'errors': (C['danger_bg'], C['danger']),
            'warnings': (C['warn_bg'], C['warning']),
            'success': (C['ok_bg'], C['ok']),
            'important': (C['accent_bg'], C['accent']),
        }
        for mode, button in self._log_quick_filter_buttons.items():
            is_active = mode == active
            bg, fg = palette.get(mode, (C['surface'], C['text_sub']))
            try:
                button.configure(
                    bg=bg if is_active else C['surface'],
                    fg=fg if is_active else C['text_sub'],
                    highlightbackground=fg if is_active else C['border'],
                    activebackground=bg if is_active else C['accent_bg'],
                    activeforeground=fg if is_active else C['accent'],
                )
            except Exception:
                pass

    def _shade_color(self, color: str, amount: float) -> str:
        color = color.lstrip('#')
        if len(color) != 6:
            return '#' + color
        parts = [int(color[i:i+2], 16) for i in (0, 2, 4)]
        adjusted = []
        for value in parts:
            if amount >= 0:
                value = int(value + (255 - value) * amount)
            else:
                value = int(value * (1 + amount))
            adjusted.append(max(0, min(255, value)))
        return '#' + ''.join(f'{v:02x}' for v in adjusted)

    def _queue_widget_bg(self, widget: tk.Widget | None, color: str) -> None:
        if widget is None:
            return
        self._animated_bg_targets[widget] = color

    def _animate_widget_bg(self, widget: tk.Widget, target: str, speed: float = 0.16) -> None:
        try:
            current = str(widget.cget('bg') or target)
        except Exception:
            current = target
        if not isinstance(current, str) or not current.startswith('#'):
            current = target
        next_color = self._blend_color(current, target, speed)
        try:
            widget.configure(bg=next_color)
        except Exception:
            return

    def _make_tree_with_scrollbar(self, parent: tk.Widget, *, columns, show='headings', xscroll: bool = False) -> tuple[tk.Frame, ttk.Treeview]:
        wrap = tk.Frame(parent, bg=C['surface'])
        tree = ttk.Treeview(wrap, columns=columns, show=show, style='Treeview')
        ysb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, style='App.Vertical.TScrollbar', command=tree.yview)
        tree.configure(yscrollcommand=ysb.set)
        if xscroll:
            xsb = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, style='App.Horizontal.TScrollbar', command=tree.xview)
            tree.configure(xscrollcommand=xsb.set)
            xsb.pack(side=tk.BOTTOM, fill=tk.X)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        return wrap, tree

    def _make_scrollable_panel(self, parent: tk.Widget, *, bg: str) -> tuple[tk.Frame, tk.Canvas, tk.Frame]:
        outer = tk.Frame(parent, bg=bg)
        canvas = tk.Canvas(outer, bg=bg, bd=0, highlightthickness=0)
        ysb = ttk.Scrollbar(outer, orient=tk.VERTICAL, style='App.Vertical.TScrollbar', command=canvas.yview)
        canvas.configure(yscrollcommand=ysb.set)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        body = tk.Frame(canvas, bg=bg)
        body_window = canvas.create_window((0, 0), window=body, anchor='nw')

        def _sync_scrollregion(_event=None):
            try:
                canvas.configure(scrollregion=canvas.bbox('all'))
            except Exception:
                pass

        def _sync_body_width(_event=None):
            try:
                canvas.itemconfigure(body_window, width=max(1, canvas.winfo_width()))
            except Exception:
                pass

        def _wheel(event):
            delta = getattr(event, 'delta', 0)
            if not delta and getattr(event, 'num', None) in (4, 5):
                delta = 120 if event.num == 4 else -120
            if delta:
                canvas.yview_scroll(int(-delta / 120), 'units')
                return 'break'
            return None

        body.bind('<Configure>', _sync_scrollregion)
        canvas.bind('<Configure>', _sync_body_width)
        for widget in (outer, canvas, body):
            widget.bind('<MouseWheel>', _wheel, add='+')
            widget.bind('<Button-4>', _wheel, add='+')
            widget.bind('<Button-5>', _wheel, add='+')

        return outer, canvas, body

    # ══════════════════════════════════════════════════════════════════════════
    # Header
    # ══════════════════════════════════════════════════════════════════════════
    def _build_header(self) -> None:
        hdr = tk.Frame(self, bg=C['panel'], height=86)
        hdr.pack(fill=tk.X, padx=0, pady=0)
        hdr.pack_propagate(False)

        tk.Frame(hdr, bg=C['accent'], height=3).place(relx=0, rely=1.0, relwidth=1, anchor='sw')

        title_box = tk.Frame(hdr, bg=C['panel'])
        title_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=18, pady=10)
        tk.Label(title_box, text='🫶  TikTok Heart Bot',
                 bg=C['panel'], fg=C['text_head'], font=FONT_TITLE).pack(anchor=tk.W)
        tk.Label(title_box, textvariable=self.hero_subtitle_var,
                 bg=C['panel'], fg='#cbd5e1', font=FONT_SMALL).pack(anchor=tk.W, pady=(3, 0))
        build_badge = tk.Label(title_box,
                               text=f"Build: {self._build_info.get('build') or 'unknown'}  ·  UI: {UI_VERSION}",
                               bg=C['panel'], fg='#93c5fd', font=('Segoe UI', 8, 'bold'))
        build_badge.pack(anchor=tk.W, pady=(3, 0))

        chip_box = tk.Frame(hdr, bg=C['panel'])
        chip_box.pack(side=tk.RIGHT, padx=18, pady=14)
        self.hero_worker_chip = self._chip(chip_box, 'Worker')
        self.hero_tg_chip = self._chip(chip_box, 'Telegram')
        self.hero_profile_chip = self._chip(chip_box, 'Профиль')
        self.hero_health_chip = self._chip(chip_box, 'Система')
        for chip in (self.hero_health_chip, self.hero_profile_chip, self.hero_tg_chip, self.hero_worker_chip):
            chip.pack(side=tk.RIGHT, padx=(6, 0))

    def _chip(self, parent: tk.Widget, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=C['chip_idle'], fg='white',
                        padx=12, pady=5, font=('Segoe UI', 9, 'bold'))

    def _chip_update(self, chip: tk.Label, text: str, kind: str) -> None:
        colors = {
            'ok':      C['ok'],
            'warning': C['warning'],
            'danger':  C['danger'],
            'info':    C['accent2'],
            'muted':   C['chip_idle'],
        }
        base = colors.get(kind, C['chip_idle'])
        chip.configure(text=text)
        self._queue_widget_bg(chip, base)

    # ══════════════════════════════════════════════════════════════════════════
    # Quick-action bar (replaces the old bloated toolbar)
    # ══════════════════════════════════════════════════════════════════════════
    def _build_quick_bar(self) -> None:
        bar = tk.Frame(self, bg=C['surface'],
                       highlightthickness=1, highlightbackground=C['border2'])
        bar.pack(fill=tk.X, padx=0, pady=0)

        row = tk.Frame(bar, bg=C['surface'])
        row.pack(fill=tk.X, padx=14, pady=(10, 10))

        left = tk.Frame(row, bg=C['surface'])
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(left, text='Командный центр', bg=C['surface'], fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w')
        actions = tk.Frame(left, bg=C['surface'])
        actions.pack(anchor='w', pady=(6, 0))
        self._qbtn(actions, '▶ Запустить всё', self.start_all, C['ok']).pack(side=tk.LEFT, padx=(0, 6))
        self._qbtn(actions, '■ Остановить', self.stop_all, C['danger']).pack(side=tk.LEFT, padx=(0, 6))
        self._qbtn(actions, '⏸ Пауза', self.toggle_pause, C['warning']).pack(side=tk.LEFT, padx=(0, 6))
        self._qbtn(actions, '↻ Обновить', self.refresh_all, C['accent']).pack(side=tk.LEFT, padx=(0, 10))
        self._menu_btn(actions, 'Worker', [
            ('Запустить worker', self.start_worker),
            ('Остановить worker', self.stop_worker),
            ('Перезапустить worker', self.restart_worker),
            ('Сбросить runtime-флаги', self.reset_runtime_flags),
        ]).pack(side=tk.LEFT, padx=(0, 6))
        self._menu_btn(actions, 'Telegram', [
            ('Запустить Telegram', self.start_telegram_bot),
            ('Остановить Telegram', self.stop_telegram_bot),
            ('Перезапустить Telegram', self.restart_telegram_bot),
            ('Очистить Telegram lock', self.clear_telegram_lock),
        ]).pack(side=tk.LEFT, padx=(0, 6))
        self._menu_btn(actions, 'Открыть', [
            ('Открыть проект', lambda: self.open_path(self.adapter.base_dir)),
            ('Логи профиля', lambda: self.open_path(self.adapter.get_active_profile_logs_dir())),
            ('Общие логи', lambda: self.open_path(self.adapter.get_common_logs_dir())),
            ('Backups', self.open_backups_folder),
        ]).pack(side=tk.LEFT, padx=(0, 6))
        self._menu_btn(actions, 'Окно', [
            ('Горячие клавиши', self._show_shortcuts_dialog),
            ('О приложении', self._show_about_dialog),
            *(([ 'Свернуть в трей', self.on_minimize_to_tray],) if self.on_minimize_to_tray else []),
            *(([ 'Закрыть приложение', self.request_exit_application],) if self.on_exit_application else []),
        ]).pack(side=tk.LEFT)

        options = tk.Frame(left, bg=C['surface'])
        options.pack(anchor='w', pady=(8, 0))
        ttk.Checkbutton(options, text='Автообновление',
                        variable=self.auto_refresh_var,
                        command=self._toggle_auto_refresh).pack(side=tk.LEFT, padx=(0, 12))
        tk.Label(options, text='Компактная плотность включена по умолчанию.', bg=C['surface'], fg=C['muted'], font=FONT_SMALL).pack(side=tk.LEFT)

        right = tk.Frame(row, bg=C['surface'])
        right.pack(side=tk.RIGHT, padx=(12, 0))
        mode_box = tk.Frame(right, bg=C['surface'])
        mode_box.pack(anchor='e')
        tk.Label(mode_box, text='Режим прогона', bg=C['surface'], fg=C['muted'], font=FONT_SMALL).pack(anchor='e')
        mode_row = tk.Frame(mode_box, bg=C['surface'])
        mode_row.pack(anchor='e', pady=(4, 0))
        if self.on_exit_application:
            self._qbtn(mode_row, '⏻ Выход', self.request_exit_application, C['danger']).pack(side=tk.LEFT, padx=(0, 8))
        self.mode_toggle_btn = self._qbtn(mode_row, 'Режим: LIVE', self.toggle_dry_run_mode, C['accent'])
        self.mode_toggle_btn.pack(side=tk.LEFT)

        util = tk.Frame(right, bg=C['surface'])
        util.pack(anchor='e', pady=(10, 0))
        if self.on_minimize_to_tray:
            self._qbtn(util, '↙ В трей', self.on_minimize_to_tray, '#475569').pack(side=tk.RIGHT, padx=(6, 0))
        self._qbtn(util, '📋 Копировать раздел', self.copy_current_tab, '#475569').pack(side=tk.RIGHT)

    def _menu_btn(self, parent, text: str, items) -> tk.Menubutton:
        btn = tk.Menubutton(parent, text=f'{text} ▾',
                            bg=C['surface2'], fg=C['text_main'],
                            activebackground=C['accent_bg'],
                            activeforeground=C['accent'],
                            relief='flat', bd=1, highlightthickness=1,
                            highlightbackground=C['border'],
                            font=FONT_BOLD, padx=10, pady=5, cursor='hand2')
        menu = tk.Menu(btn, tearoff=False)
        for label, command in items:
            menu.add_command(label=label, command=command)
        btn.configure(menu=menu)
        return btn

    def request_exit_application(self) -> None:
        if not self.on_exit_application:
            return
        self.set_status('Завершение приложения…')
        try:
            self._log_action('Приложение закрыто напрямую из интерфейса')
        except Exception:
            pass
        self.after(10, self.on_exit_application)

    def _toggle_compact_mode_quick(self) -> None:
        self.compact_mode_var.set(True)
        self._toggle_compact_mode()

    def _qbtn(self, parent, text: str, cmd, color: str) -> tk.Button:
        btn = tk.Button(parent, text=text, command=cmd,
                        bg=color, fg='white', activebackground=color,
                        activeforeground='white', relief='flat',
                        font=FONT_BOLD, padx=10, pady=5, cursor='hand2',
                        bd=0, highlightthickness=0)
        normal = color
        hover = self._shade_color(color, 0.12)
        pressed = self._shade_color(color, -0.08)
        btn.configure(activebackground=pressed)
        btn.bind('<Enter>', lambda _e, b=btn, c=hover: b.configure(bg=c))
        btn.bind('<Leave>', lambda _e, b=btn, c=normal: b.configure(bg=c))
        btn.bind('<ButtonPress-1>', lambda _e, b=btn, c=pressed: b.configure(bg=c))
        btn.bind('<ButtonRelease-1>', lambda _e, b=btn, c=hover: b.configure(bg=c))
        self._action_buttons.append(btn)
        return btn

    def _build_tabs(self) -> None:
        outer = tk.Frame(self, bg=C['surface'],
                         highlightthickness=1, highlightbackground=C['border2'])
        outer.pack(fill=tk.BOTH, expand=True, padx=0, pady=(2, 0))

        self.tabs = ttk.Notebook(outer)
        self.tabs.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)
        self.tabs.bind('<<NotebookTabChanged>>', self._on_tab_changed)

        page_style = 'Root.TFrame'
        self.overview_tab     = ttk.Frame(self.tabs, padding=(10, 8), style=page_style)
        self.services_tab     = ttk.Frame(self.tabs, padding=(10, 8), style=page_style)
        self.messages_tab     = ttk.Frame(self.tabs, padding=(10, 8), style=page_style)
        self.profiles_tab     = ttk.Frame(self.tabs, padding=(10, 8), style=page_style)
        self.logs_tab         = ttk.Frame(self.tabs, padding=(10, 8), style=page_style)
        self.diagnostics_tab  = ttk.Frame(self.tabs, padding=(10, 8), style=page_style)

        self.tabs.add(self.overview_tab,    text='  📊  Обзор  ')
        self.tabs.add(self.services_tab,    text='  ⚙️  Сервисы  ')
        self.tabs.add(self.messages_tab,    text='  💬  Сообщения  ')
        self.tabs.add(self.profiles_tab,    text='  👤  Профили  ')
        self.tabs.add(self.logs_tab,        text='  📋  Логи  ')
        self.tabs.add(self.diagnostics_tab, text='  🔍  Диагностика  ')

        self._build_overview_tab()
        self._build_services_tab()
        self._build_messages_tab()
        self._build_profiles_tab()
        self._build_logs_tab()
        self._build_diagnostics_tab()   # also contains files + raw sub-tabs

        self._install_copy_support()

        self._tab_widgets = {
            str(self.overview_tab):    self.summary_text,
            str(self.services_tab):    self.service_notes_text,
            str(self.messages_tab):    self.messages_text,
            str(self.profiles_tab):    self.profile_detail_text,
            str(self.logs_tab):        self.logs_text,
            str(self.diagnostics_tab): self.recommendations_text,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # Status bar
    # ══════════════════════════════════════════════════════════════════════════
    def _build_status_bar(self) -> None:
        bar = tk.Frame(self, bg=C['panel'], height=34)
        bar.pack(fill=tk.X, padx=0, pady=0)
        bar.pack_propagate(False)
        self._status_dot = tk.Label(bar, text='●', bg=C['panel'],
                                    fg=C['ok'], font=('Segoe UI', 10, 'bold'))
        self._status_dot.pack(side=tk.LEFT, padx=(14, 4))
        tk.Label(bar, textvariable=self.status_var,
                 bg=C['panel'], fg=C['text_head'],
                 font=FONT_SMALL).pack(side=tk.LEFT)

        right = tk.Frame(bar, bg=C['panel'])
        right.pack(side=tk.RIGHT, padx=14)
        tk.Label(right, textvariable=self.current_time_var,
                 bg=C['panel'], fg=C['text_head'],
                 font=('Segoe UI', 8, 'bold')).pack(side=tk.RIGHT, padx=(12, 0))
        tk.Label(right, textvariable=self.refresh_eta_var,
                 bg=C['panel'], fg=C['muted'],
                 font=('Segoe UI', 8)).pack(side=tk.RIGHT, padx=(12, 0))
        tk.Label(right, textvariable=self.last_refresh_var,
                 bg=C['panel'], fg=C['muted'],
                 font=('Segoe UI', 8)).pack(side=tk.RIGHT, padx=(12, 0))
        tk.Label(right, textvariable=self.active_tab_var,
                 bg=C['panel'], fg=C['muted'],
                 font=('Segoe UI', 8)).pack(side=tk.RIGHT, padx=(12, 0))

    def _build_overview_tab(self) -> None:
        self.overview_tab.columnconfigure(0, weight=1)
        self.overview_tab.rowconfigure(0, weight=1)

        overview_body = tk.Frame(self.overview_tab, bg=C['bg'])
        overview_body.pack(fill=tk.BOTH, expand=True)
        overview_body.columnconfigure(0, weight=1)
        overview_body.rowconfigure(1, weight=1)
        self._overview_body = overview_body

        row1 = tk.Frame(overview_body, bg=C['bg'])
        row1.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        row1.columnconfigure(1, weight=1)

        gauge_card = tk.Frame(
            row1, bg=C['surface'], width=212, height=188,
            highlightthickness=1, highlightbackground=C['border'])
        gauge_card.grid(row=0, column=0, sticky='nsw', padx=(0, 8))
        gauge_card.grid_propagate(False)
        tk.Label(gauge_card, text='Здоровье системы', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w', padx=12, pady=(8, 0))
        self.health_canvas = tk.Canvas(gauge_card, width=184, height=108,
                                       bg=C['surface'], highlightthickness=0)
        self.health_canvas.pack(padx=4, pady=(0, 0))
        self.breakdown_frame = tk.Frame(gauge_card, bg=C['surface'])
        self.breakdown_frame.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.breakdown_frame.columnconfigure(1, weight=1)
        self._overview_gauge_card = gauge_card

        cards_col = tk.Frame(row1, bg=C['bg'])
        cards_col.grid(row=0, column=1, sticky='nsew')
        cards_col.columnconfigure(0, weight=1, uniform='overview_cards')
        cards_col.columnconfigure(1, weight=1, uniform='overview_cards')
        cards_col.rowconfigure(0, weight=1, uniform='overview_cards')
        cards_col.rowconfigure(1, weight=1, uniform='overview_cards')
        self._overview_cards_col = cards_col

        self.metric_cards = {
            'worker':   self._metric_card(cards_col, 0, 0, 'Worker', '⚙️', height=92),
            'telegram': self._metric_card(cards_col, 0, 1, 'Telegram', '🤖', height=92),
            'profile':  self._metric_card(cards_col, 1, 0, 'Активный профиль', '👤', height=92),
            'messages': self._metric_card(cards_col, 1, 1, 'Пул сообщений', '💬', height=92),
        }

        lower = tk.Frame(overview_body, bg=C['bg'])
        lower.grid(row=1, column=0, sticky='nsew')
        lower.columnconfigure(0, weight=12, uniform='overview_lower')
        lower.columnconfigure(1, weight=9, uniform='overview_lower')
        lower.rowconfigure(0, weight=1)
        self._overview_lower = lower

        def _card(parent, title):
            f = tk.Frame(parent, bg=C['surface'],
                         highlightthickness=1, highlightbackground=C['border'])
            tk.Label(f, text=title, bg=C['surface'], fg=C['text_sub'],
                     font=FONT_BOLD).pack(anchor='w', padx=10, pady=(7, 3))
            tk.Frame(f, bg=C['border'], height=1).pack(fill=tk.X)
            return f

        def _compact_text(parent, *, height: int):
            widget = tk.Text(
                parent,
                wrap=tk.WORD,
                font=FONT_MONO,
                relief=tk.FLAT,
                bg=C['surface'],
                fg=C['text_main'],
                insertbackground=C['text_main'],
                bd=0,
                padx=8,
                pady=6,
                highlightthickness=0,
                height=height,
            )
            return widget

        lp = tk.Frame(lower, bg=C['bg'])
        lp.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        lp.columnconfigure(0, weight=1)
        lp.rowconfigure(0, weight=1)

        sc = _card(lp, 'Состояние control center')
        sc.grid(row=0, column=0, sticky='nsew')
        tb = tk.Frame(sc, bg=C['surface'])
        tb.pack(fill=tk.BOTH, expand=True)
        tb_wrap, self.state_tree = self._make_tree_with_scrollbar(tb, columns=('metric', 'status', 'details'))
        tb_wrap.pack(fill=tk.BOTH, expand=True)
        for col, title, w in (('metric', 'Параметр', 146), ('status', 'Статус', 108), ('details', 'Подробности', 410)):
            self.state_tree.heading(col, text=title)
            self.state_tree.column(col, width=w, anchor=tk.W)

        rp = tk.Frame(lower, bg=C['bg'])
        rp.grid(row=0, column=1, sticky='nsew')
        rp.columnconfigure(0, weight=1, uniform='overview_side')
        rp.columnconfigure(1, weight=1, uniform='overview_side')
        rp.rowconfigure(0, weight=11)
        rp.rowconfigure(1, weight=10)
        rp.rowconfigure(2, weight=10)
        self._overview_side_panel = rp

        runf = _card(rp, 'Текущий прогон')
        runf.grid(row=0, column=0, columnspan=2, sticky='nsew')
        activityf = _card(rp, 'Последние события')
        activityf.grid(row=1, column=0, sticky='nsew', pady=(8, 0), padx=(0, 4))
        recf = _card(rp, 'Что стоит сделать')
        recf.grid(row=1, column=1, sticky='nsew', pady=(8, 0), padx=(4, 0))
        self._overview_run_card = runf
        self._overview_activity_card = activityf
        self._overview_rec_card = recf

        self.current_run_text = _compact_text(runf, height=6)
        self.current_run_text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.activity_text = _compact_text(activityf, height=7)
        self.activity_text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.overview_rec_text = _compact_text(recf, height=7)
        self.overview_rec_text.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.summary_text = UnifiedScrolledText(
            overview_body, height=0, relief=tk.FLAT, bg=C['surface'], bd=0)

        overview_body.bind('<Configure>', self._on_overview_resize, add='+')
        self.after_idle(lambda: self._apply_overview_layout(overview_body.winfo_width(), overview_body.winfo_height()))

    def _on_overview_resize(self, event: tk.Event) -> None:
        self._apply_overview_layout(getattr(event, 'width', 0), getattr(event, 'height', 0))

    def _apply_overview_layout(self, width: int, height: int) -> None:
        layout = resolve_overview_layout(width, height)
        if layout == getattr(self, '_overview_layout_state', {}):
            return
        self._overview_layout_state = dict(layout)

        gauge_width = 222 if width >= 1380 else 212
        gauge_height = 176 if layout['compact_metrics'] else 188
        gauge_canvas_h = 100 if layout['compact_metrics'] else 108
        try:
            self._overview_gauge_card.configure(width=gauge_width, height=gauge_height)
            self.health_canvas.configure(width=gauge_width - 28, height=gauge_canvas_h)
        except Exception:
            pass

        metric_height = 84 if layout['compact_metrics'] else 92
        for card in getattr(self, 'metric_cards', {}).values():
            try:
                card['value'].master.configure(height=metric_height)
            except Exception:
                pass

        rp = getattr(self, '_overview_side_panel', None)
        runf = getattr(self, '_overview_run_card', None)
        activityf = getattr(self, '_overview_activity_card', None)
        recf = getattr(self, '_overview_rec_card', None)
        if rp is None or runf is None or activityf is None or recf is None:
            return

        runf.grid_forget()
        activityf.grid_forget()
        recf.grid_forget()

        if layout['collapse_activity']:
            runf.grid(row=0, column=0, columnspan=2, rowspan=2, sticky='nsew')
            rp.rowconfigure(0, weight=1)
            rp.rowconfigure(1, weight=0)
            rp.rowconfigure(2, weight=0)
        elif layout['stack_side_cards']:
            runf.grid(row=0, column=0, columnspan=2, sticky='nsew')
            activityf.grid(row=1, column=0, columnspan=2, sticky='nsew', pady=(8, 0))
            if not layout['collapse_recommendations']:
                recf.grid(row=2, column=0, columnspan=2, sticky='nsew', pady=(8, 0))
                rp.rowconfigure(2, weight=8)
            else:
                rp.rowconfigure(2, weight=0)
            rp.rowconfigure(0, weight=11)
            rp.rowconfigure(1, weight=9)
        else:
            runf.grid(row=0, column=0, columnspan=2, sticky='nsew')
            if layout['collapse_recommendations']:
                activityf.grid(row=1, column=0, columnspan=2, sticky='nsew', pady=(8, 0))
            else:
                activityf.grid(row=1, column=0, sticky='nsew', pady=(8, 0), padx=(0, 4))
                recf.grid(row=1, column=1, sticky='nsew', pady=(8, 0), padx=(4, 0))
            rp.rowconfigure(0, weight=11)
            rp.rowconfigure(1, weight=10)
            rp.rowconfigure(2, weight=0)

        try:
            self.current_run_text.configure(height=5 if layout['compact_metrics'] else 6)
            self.activity_text.configure(height=5 if layout['compact_metrics'] else 7)
            self.overview_rec_text.configure(height=5 if layout['compact_metrics'] else 7)
        except Exception:
            pass
    def _metric_card(self, parent, row, col, title, icon='', *, height: int = 132):
        pr = 8 if col == 0 else 0
        pb = 8 if row == 0 else 0
        card = tk.Frame(parent, bg=C['surface'],
                        highlightthickness=1, highlightbackground=C['border'],
                        height=height)
        card.grid(row=row, column=col, sticky='nsew', padx=(0, pr), pady=(0, pb), ipadx=0, ipady=0)
        card.grid_propagate(False)
        stripe_colors = ['#6366f1', '#0ea5e9', '#16a34a', '#d97706']
        tk.Frame(card, bg=stripe_colors[(row * 2 + col) % 4], height=3).pack(fill=tk.X)
        hdr = tk.Frame(card, bg=C['surface'])
        hdr.pack(fill=tk.X, padx=10, pady=(7, 0))
        tk.Label(hdr, text=icon, bg=C['surface'], font=('Segoe UI', 12)).pack(side=tk.LEFT)
        tk.Label(hdr, text=title, bg=C['surface'], fg=C['text_sub'],
                 font=FONT_BOLD).pack(side=tk.LEFT, padx=5)
        value = tk.Label(card, text='—', bg=C['surface'], fg=C['text_main'], font=FONT_CARD)
        value.pack(anchor=tk.W, padx=10, pady=(2, 0))
        sub = tk.Label(card, text='—', bg=C['surface'], fg=C['text_sub'],
                       font=FONT_SMALL, justify=tk.LEFT, wraplength=240)
        sub.pack(anchor=tk.W, padx=10, pady=(1, 3))
        bar = ttk.Progressbar(card, orient='horizontal', mode='determinate', maximum=100)
        bar.pack(fill=tk.X, padx=10, pady=(0, 6))
        return {'value': value, 'sub': sub, 'bar': bar}

    # ══════════════════════════════════════════════════════════════════════════
    # Services tab
    # ══════════════════════════════════════════════════════════════════════════
    def _build_services_tab(self) -> None:
        self._services_selected = tk.StringVar(value='worker')
        self._sidebar_status_lbls: dict[str, tk.Label] = {}
        self._sidebar_btns: dict[str, tk.Frame] = {}
        self._svc_panels: dict[str, tk.Frame] = {}

        # root grid: sidebar | detail
        main = tk.Frame(self.services_tab, bg=C['bg'])
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # ── Left sidebar ──────────────────────────────────────────────────────
        sidebar = tk.Frame(main, bg=C['sidebar'], width=210)
        sidebar.grid(row=0, column=0, sticky='nsew')
        sidebar.pack_propagate(False)

        tk.Label(sidebar, text='СЕРВИСЫ', bg=C['sidebar'],
                 fg='#475569', font=('Segoe UI', 8, 'bold'),
                 padx=16, pady=10).pack(anchor='w', pady=(4, 0))

        def _sidebar_item(key: str, icon: str, label: str, sub: str) -> tk.Frame:
            item = tk.Frame(sidebar, bg=C['sidebar'], cursor='hand2')
            item.pack(fill=tk.X)
            # left accent bar (hidden by default)
            accent = tk.Frame(item, bg=C['sidebar'], width=3)
            accent.pack(side=tk.LEFT, fill=tk.Y)
            icon_lbl = tk.Label(item, text=icon, bg=C['sidebar'],
                                fg='white', font=('Segoe UI', 16),
                                padx=12, pady=12)
            icon_lbl.pack(side=tk.LEFT)
            txt = tk.Frame(item, bg=C['sidebar'])
            txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Label(txt, text=label, bg=C['sidebar'], fg=C['sidebar_txt'],
                     font=('Segoe UI', 10, 'bold'), anchor='w').pack(anchor='w')
            tk.Label(txt, text=sub, bg=C['sidebar'], fg='#475569',
                     font=('Segoe UI', 8), anchor='w').pack(anchor='w')
            dot = tk.Label(item, text='●', bg=C['sidebar'],
                           fg='#475569', font=('Segoe UI', 10), padx=10)
            dot.pack(side=tk.RIGHT)
            self._sidebar_status_lbls[key] = dot
            item._accent = accent  # type: ignore[attr-defined]

            def _click(_e=None, k=key):
                self._services_selected.set(k)
                self._refresh_services_view()

            for w in (item, accent, icon_lbl, txt) + tuple(txt.winfo_children()) + (dot,):
                try:
                    w.bind('<Button-1>', _click)
                except Exception:
                    pass
            self._sidebar_btns[key] = item
            return item

        _sidebar_item('worker',   '⚙️', 'Worker',   'TikTok runtime')
        _sidebar_item('telegram', '🤖', 'Telegram', 'Control bot')

        tk.Frame(sidebar, bg=C['sidebar']).pack(fill=tk.BOTH, expand=True)

        # global actions at bottom of sidebar
        glob = tk.Frame(sidebar, bg=C['sidebar'])
        glob.pack(fill=tk.X)
        tk.Frame(glob, bg='#334155', height=1).pack(fill=tk.X, pady=(0, 10))
        tk.Label(glob, text='ВСЕ СЕРВИСЫ', bg=C['sidebar'],
                 fg='#475569', font=('Segoe UI', 8, 'bold'),
                 padx=16).pack(anchor='w', pady=(0, 6))

        def _gbtn(text, cmd, col):
            b = tk.Button(glob, text=text, command=cmd,
                          bg=col, fg='white', activebackground=col,
                          relief='flat', font=('Segoe UI', 9, 'bold'),
                          padx=8, pady=6, cursor='hand2',
                          bd=0, highlightthickness=0)
            self._action_buttons.append(b)
            b.pack(fill=tk.X, padx=12, pady=2)

        _gbtn('▶  Запустить всё',  self.start_all,   C['ok'])
        _gbtn('■  Остановить всё', self.stop_all,    C['danger'])
        _gbtn('↻  Перезапустить',  self.restart_all, C['accent'])

        # ── Right area ────────────────────────────────────────────────────────
        right_col = tk.Frame(main, bg=C['bg'])
        right_col.grid(row=0, column=1, sticky='nsew')
        right_col.columnconfigure(0, weight=1)
        right_col.rowconfigure(0, weight=1)

        # detail panels container
        self._svc_right_top = tk.Frame(right_col, bg=C['bg'])
        self._svc_right_top.grid(row=0, column=0, sticky='nsew',
                                  padx=10, pady=(8, 4))
        self._svc_right_top.columnconfigure(0, weight=1)
        self._svc_right_top.rowconfigure(0, weight=1)

        self.worker_card   = self._build_svc_detail('worker',
            '⚙️  Worker · TikTok runtime', [
                ('▶  Запустить',       self.start_worker,        C['ok']),
                ('■  Остановить',      self.stop_worker,         C['danger']),
                ('↻  Перезапустить',   self.restart_worker,      C['accent']),
                ('⚑  Сбросить флаги',  self.reset_runtime_flags, '#64748b'),
                ('⏸  Пауза',           self.toggle_pause,        C['warning']),
            ])
        self.telegram_card = self._build_svc_detail('telegram',
            '🤖  Telegram bot', [
                ('▶  Запустить',       self.start_telegram_bot,   C['ok']),
                ('■  Остановить',      self.stop_telegram_bot,    C['danger']),
                ('↻  Перезапустить',   self.restart_telegram_bot, C['accent']),
                ('🔒  Очистить lock',  self.clear_telegram_lock,  '#64748b'),
            ])

        # shared notes at bottom
        notes_wrap = tk.Frame(right_col, bg=C['surface'],
                              highlightthickness=1, highlightbackground=C['border'])
        notes_wrap.grid(row=1, column=0, sticky='ew', padx=10, pady=(0, 8))
        hdr_nw = tk.Frame(notes_wrap, bg=C['surface'])
        hdr_nw.pack(fill=tk.X, padx=10, pady=(7, 3))
        tk.Label(hdr_nw, text='📋  Справка и сигналы', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(side=tk.LEFT)
        tk.Frame(notes_wrap, bg=C['border'], height=1).pack(fill=tk.X)
        self.service_notes_text = UnifiedScrolledText(
            notes_wrap, wrap=tk.WORD, font=FONT_MONO, relief=tk.FLAT,
            height=7, bg=C['surface'], bd=0)
        self.service_notes_text.pack(fill=tk.BOTH, padx=4, pady=4)

        self._refresh_services_view()

    def _build_svc_detail(self, key: str, title: str,
                           buttons: list) -> dict:
        """Build a detail+control panel for one service."""
        outer = tk.Frame(self._svc_right_top, bg=C['bg'])
        outer.grid(row=0, column=0, sticky='nsew')
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        outer.grid_remove()

        # status card
        card = tk.Frame(outer, bg=C['surface'],
                        highlightthickness=1, highlightbackground=C['border'])
        card.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        stripe = tk.Frame(card, bg=C['accent'], width=5)
        stripe.pack(side=tk.LEFT, fill=tk.Y)
        info = tk.Frame(card, bg=C['surface'])
        info.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=16, pady=14)
        tk.Label(info, text=title, bg=C['surface'], fg=C['text_sub'],
                 font=FONT_BOLD).pack(anchor='w')
        status_lbl = tk.Label(info, text='—', bg=C['surface'],
                              fg=C['text_main'], font=FONT_HERO)
        status_lbl.pack(anchor='w', pady=(6, 0))
        meta = tk.Frame(info, bg=C['surface'])
        meta.pack(anchor='w', pady=(6, 0))
        pid_lbl = tk.Label(meta, text='PID: —', bg=C['surface'],
                           fg=C['text_sub'], font=FONT_SMALL)
        pid_lbl.pack(side=tk.LEFT, padx=(0, 20))
        started_lbl = tk.Label(meta, text='Запущен: —', bg=C['surface'],
                               fg=C['text_sub'], font=FONT_SMALL)
        started_lbl.pack(side=tk.LEFT)
        cmd_lbl = tk.Label(info, text='', bg=C['surface'], fg=C['muted'],
                           font=('Consolas', 8), justify=tk.LEFT, wraplength=800)
        cmd_lbl.pack(anchor='w', pady=(4, 0))

        # controls card
        ctl_card = tk.Frame(outer, bg=C['surface'],
                            highlightthickness=1, highlightbackground=C['border'])
        ctl_card.grid(row=1, column=0, sticky='nsew')
        ctl_hdr = tk.Frame(ctl_card, bg=C['surface'])
        ctl_hdr.pack(fill=tk.X, padx=14, pady=(12, 6))
        tk.Label(ctl_hdr, text='🎛  Управление', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w')
        tk.Frame(ctl_card, bg=C['border'], height=1).pack(fill=tk.X)

        btn_grid = tk.Frame(ctl_card, bg=C['surface'])
        btn_grid.pack(fill=tk.X, padx=14, pady=14)
        for i, (label, cmd, color) in enumerate(buttons):
            b = tk.Button(btn_grid, text=label, command=cmd,
                          bg=color, fg='white', activebackground=color,
                          relief='flat', font=FONT_BOLD,
                          padx=14, pady=9, cursor='hand2',
                          bd=0, highlightthickness=0)
            b.grid(row=i // 4, column=i % 4, sticky='ew',
                   padx=(0, 8 if i % 4 < 3 else 0), pady=(0, 8))
            self._action_buttons.append(b)
        for col in range(4):
            btn_grid.columnconfigure(col, weight=1)

        self._svc_panels[key] = outer
        return {'status': status_lbl, 'pid': pid_lbl, 'started': started_lbl,
                'cmd': cmd_lbl, 'note': cmd_lbl, 'stripe': stripe}

    def _refresh_services_view(self) -> None:
        sel = self._services_selected.get()
        for key, panel in self._svc_panels.items():
            if key == sel:
                panel.grid()
            else:
                panel.grid_remove()
        for key, frame in self._sidebar_btns.items():
            is_sel = key == sel
            bg = C['sidebar_sel'] if is_sel else C['sidebar']
            stripe_col = C['sidebar_act'] if is_sel else C['sidebar']
            frame.configure(bg=bg)
            accent_bar = getattr(frame, '_accent', None)
            if accent_bar:
                try:
                    accent_bar.configure(bg=stripe_col)
                except Exception:
                    pass
            for child in frame.winfo_children():
                if child is accent_bar:
                    continue
                try:
                    child.configure(bg=bg)
                    for gc in child.winfo_children():
                        try:
                            gc.configure(bg=bg)
                        except Exception:
                            pass
                except Exception:
                    pass

    # ══════════════════════════════════════════════════════════════════════════
    # Messages tab
    # ══════════════════════════════════════════════════════════════════════════
    def _build_messages_tab(self) -> None:
        # toolbar
        toolbar = tk.Frame(self.messages_tab, bg=C['surface'],
                           highlightthickness=1, highlightbackground=C['border'])
        toolbar.pack(fill=tk.X, pady=(0, 8), ipady=7)

        def _tbtn(text, cmd, color=None):
            if color:
                b = tk.Button(toolbar, text=text, command=cmd,
                              bg=color, fg='white', activebackground=color,
                              relief='flat', font=FONT_BOLD,
                              padx=10, pady=5, cursor='hand2',
                              bd=0, highlightthickness=0)
            else:
                b = ttk.Button(toolbar, text=text, command=cmd)
            b.pack(side=tk.LEFT, padx=(0, 6))
            return b

        _tbtn('💾  Сохранить как есть', self.save_messages,      C['ok'])
        _tbtn('💾  Backup',           self.create_messages_backup)
        _tbtn('📂  Backups',          self.open_backups_folder)
        _tbtn('↺  Перезагрузить',     self.reload_messages,     C['accent'])
        _tbtn('🧹  Нормализовать…',   self.normalize_messages)
        _tbtn('📋  Копировать всё',
              lambda: self._copy_text_widget(self.messages_text, copy_all=True))
        ttk.Label(toolbar, textvariable=self.message_details_var,
                  style='Hint.TLabel').pack(side=tk.LEFT, padx=(10, 0))

        meta = tk.Frame(self.messages_tab, bg=C['bg'])
        meta.pack(fill=tk.X, pady=(0, 8))
        self._msg_state_label = tk.Label(meta, textvariable=self.message_editor_state_var,
                 bg=C['ok_bg'], fg=C['ok'], padx=10, pady=4,
                 font=('Segoe UI', 9, 'bold'))
        self._msg_state_label.pack(side=tk.LEFT)
        tk.Label(meta, textvariable=self.message_editor_meta_var,
                 bg=C['surface'], fg=C['text_sub'], padx=10, pady=4,
                 font=FONT_SMALL, highlightthickness=1,
                 highlightbackground=C['border']).pack(side=tk.LEFT, padx=(8, 0))
        tk.Label(meta, textvariable=self.message_backup_var,
                 bg=C['accent_bg'], fg=C['accent'], padx=10, pady=4,
                 font=FONT_SMALL).pack(side=tk.LEFT, padx=(8, 0))

        # main split
        main = ttk.Panedwindow(self.messages_tab, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True)
        left  = tk.Frame(main, bg=C['surface'])
        right = tk.Frame(main, bg=C['surface'])
        main.add(left, weight=3)
        main.add(right, weight=2)

        # left: editor
        left_hdr = tk.Frame(left, bg=C['bg'])
        left_hdr.pack(fill=tk.X, pady=(0, 4))
        tk.Label(left_hdr, text='✏️  message_pool.txt', bg=C['bg'],
                 fg=C['text_main'], font=FONT_BOLD).pack(side=tk.LEFT)
        tk.Label(left_hdr, text='— сохранение как есть, нормализация отдельно с preview',
                 bg=C['bg'], fg=C['muted'], font=FONT_SMALL).pack(side=tk.LEFT, padx=6)

        self.messages_text = UnifiedScrolledText(
            left, wrap=tk.WORD, font=FONT_MONO, undo=True, relief=tk.FLAT, bg=C['surface'], bd=0,
            highlightthickness=1, highlightbackground=C['border'])
        self.messages_text.pack(fill=tk.BOTH, expand=True)
        self.messages_text.bind('<<Modified>>', self._on_messages_modified)

        # right: stats + live preview cards
        right_inner = tk.Frame(right, bg=C['bg'])
        right_inner.pack(fill=tk.BOTH, expand=True)
        right_inner.columnconfigure(0, weight=1)
        right_inner.rowconfigure(1, weight=1)

        # stats row
        stats_card = tk.Frame(right_inner, bg=C['surface'],
                              highlightthickness=1, highlightbackground=C['border'])
        stats_card.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        stats_hdr = tk.Frame(stats_card, bg=C['surface'])
        stats_hdr.pack(fill=tk.X, padx=12, pady=(10, 6))
        tk.Label(stats_hdr, text='📊  Статистика пула',
                 bg=C['surface'], fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w')
        tk.Frame(stats_card, bg=C['border'], height=1).pack(fill=tk.X)

        self._msg_stat_grid = tk.Frame(stats_card, bg=C['surface'])
        self._msg_stat_grid.pack(fill=tk.X, padx=14, pady=10)
        self._msg_stat_labels: dict[str, tk.Label] = {}

        stat_defs = [
            ('unique_count',  'Уникальных',  C['ok']),
            ('duplicates',    'Дубликатов',  C['warning']),
            ('blank_lines',   'Пустых',      C['muted']),
            ('comment_lines', 'Коммент.',    C['accent2']),
        ]
        for col, (key, label, color) in enumerate(stat_defs):
            self._msg_stat_grid.columnconfigure(col, weight=1)
            cell = tk.Frame(self._msg_stat_grid, bg=C['surface'])
            cell.grid(row=0, column=col, sticky='ew', padx=(0, 8 if col < 3 else 0))
            val_lbl = tk.Label(cell, text='—', bg=C['surface'],
                               fg=color, font=('Segoe UI', 20, 'bold'))
            val_lbl.pack()
            tk.Label(cell, text=label, bg=C['surface'],
                     fg=C['muted'], font=('Segoe UI', 8)).pack()
            self._msg_stat_labels[key] = val_lbl

        # preview area
        preview_outer = tk.Frame(right_inner, bg=C['surface'],
                                  highlightthickness=1, highlightbackground=C['border'])
        preview_outer.grid(row=1, column=0, sticky='nsew')
        preview_hdr = tk.Frame(preview_outer, bg=C['surface'])
        preview_hdr.pack(fill=tk.X, padx=12, pady=(10, 6))
        tk.Label(preview_hdr, text='💬  Варианты сообщений',
                 bg=C['surface'], fg=C['text_sub'], font=FONT_BOLD).pack(side=tk.LEFT)
        self._msg_preview_count = tk.Label(preview_hdr, text='',
                                            bg=C['surface'], fg=C['muted'],
                                            font=FONT_SMALL)
        self._msg_preview_count.pack(side=tk.LEFT, padx=6)
        tk.Frame(preview_outer, bg=C['border'], height=1).pack(fill=tk.X)

        # scrollable canvas for message pills
        canvas_wrap = tk.Frame(preview_outer, bg=C['surface'])
        canvas_wrap.pack(fill=tk.BOTH, expand=True)
        self._msg_canvas = tk.Canvas(canvas_wrap, bg=C['surface'],
                                      highlightthickness=0)
        sb = ttk.Scrollbar(canvas_wrap, orient=tk.VERTICAL, style='App.Vertical.TScrollbar',
                           command=self._msg_canvas.yview)
        self._msg_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._msg_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._msg_pills_frame = tk.Frame(self._msg_canvas, bg=C['surface'])
        self._msg_canvas_window = self._msg_canvas.create_window(
            (0, 0), window=self._msg_pills_frame, anchor='nw')
        self._msg_pills_frame.bind('<Configure>',
            lambda e: self._msg_canvas.configure(
                scrollregion=self._msg_canvas.bbox('all')))
        self._msg_canvas.bind('<Configure>',
            lambda e: self._msg_canvas.itemconfig(
                self._msg_canvas_window, width=e.width))

        # secondary stats text (hidden, kept for copy support)
        self.message_stats_text = UnifiedScrolledText(
            right_inner, height=0, relief=tk.FLAT, bg=C['surface'], bd=0)
        # don't pack — kept only for legacy references

    # ══════════════════════════════════════════════════════════════════════════
    # Profiles tab
    # ══════════════════════════════════════════════════════════════════════════
    def _build_profiles_tab(self) -> None:
        # Use grid so tools panel gets a guaranteed minimum height
        self.profiles_tab.rowconfigure(0, weight=1)
        self.profiles_tab.rowconfigure(1, weight=0)
        self.profiles_tab.columnconfigure(0, weight=1)

        # Top: profiles list + details
        top_pane = ttk.Panedwindow(self.profiles_tab, orient=tk.HORIZONTAL)
        top_pane.grid(row=0, column=0, sticky='nsew')
        left   = ttk.Frame(top_pane)
        center = ttk.Frame(top_pane)
        right  = ttk.Frame(top_pane)
        top_pane.add(left,   weight=2)
        top_pane.add(center, weight=3)
        top_pane.add(right,  weight=2)

        # profiles list
        profiles_wrap, self.profiles_tree = self._make_tree_with_scrollbar(
            left, columns=('label', 'active', 'targets'))
        for col, title, width in (('label', 'Профиль', 220),
                                   ('active', 'Активный', 90),
                                   ('targets', 'Targets', 80)):
            self.profiles_tree.heading(col, text=title)
            self.profiles_tree.column(col, width=width,
                                      anchor=tk.W if col == 'label' else tk.CENTER)
        profiles_wrap.pack(fill=tk.BOTH, expand=True)
        self.profiles_tree.bind('<<TreeviewSelect>>', lambda _e: self._fill_profile_details())

        ctrl = ttk.Frame(left)
        ctrl.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(ctrl, text='✔ Сделать активным',
                   command=self.activate_selected_profile).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(ctrl, text='📁 Открыть profiles',
                   command=lambda: self.open_path(
                       self.adapter.base_dir / 'profiles')).pack(side=tk.LEFT)
        ttk.Label(left, textvariable=self.profile_hint_var, style='Hint.TLabel', wraplength=260, justify=tk.LEFT).pack(fill=tk.X, pady=(8, 0))

        # profile details
        det_box_card = tk.Frame(center, bg=C['surface'],
                               highlightthickness=1, highlightbackground=C['border'])
        det_box_card.pack(fill=tk.BOTH, expand=True)
        tk.Label(det_box_card, text='Детали профиля', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w', padx=10, pady=(7,3))
        tk.Frame(det_box_card, bg=C['border'], height=1).pack(fill=tk.X)
        det_box = tk.Frame(det_box_card, bg=C['surface'])
        det_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.profile_detail_text = UnifiedScrolledText(
            det_box, wrap=tk.WORD, font=FONT_MONO, relief=tk.FLAT, bg=C['surface'], bd=0)
        self.profile_detail_text.pack(fill=tk.BOTH, expand=True)

        # targets list
        tgt_box_card = tk.Frame(right, bg=C['surface'],
                               highlightthickness=1, highlightbackground=C['border'])
        tgt_box_card.pack(fill=tk.BOTH, expand=True)
        tgt_hdr = tk.Frame(tgt_box_card, bg=C['surface'])
        tgt_hdr.pack(fill=tk.X, padx=10, pady=(7,3))
        tk.Label(tgt_hdr, text='Адресаты', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(side=tk.LEFT)
        self._profile_target_count_lbl = tk.Label(tgt_hdr, text='(0)', bg=C['surface'],
                                                  fg=C['muted'], font=FONT_SMALL)
        self._profile_target_count_lbl.pack(side=tk.LEFT, padx=6)
        search_row = tk.Frame(tgt_box_card, bg=C['surface'])
        search_row.pack(fill=tk.X, padx=10, pady=(0, 4))
        ttk.Label(search_row, text='Поиск:').pack(side=tk.LEFT)
        search_entry = ttk.Entry(search_row, textvariable=self.target_search_var)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        search_entry.bind('<KeyRelease>', lambda _e: self._fill_profile_details())
        ttk.Button(search_row, text='Очистить', command=lambda: (self.target_search_var.set(''), self._fill_profile_details(), self._save_ui_state())).pack(side=tk.LEFT)
        ttk.Label(tgt_box_card, textvariable=self.targets_meta_var, style='Hint.TLabel', justify=tk.LEFT).pack(fill=tk.X, padx=10, pady=(0, 4))
        tk.Frame(tgt_box_card, bg=C['border'], height=1).pack(fill=tk.X)
        tgt_box = tk.Frame(tgt_box_card, bg=C['surface'])
        tgt_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        targets_wrap, self.profile_targets_tree = self._make_tree_with_scrollbar(
            tgt_box, columns=('idx', 'name', 'ready', 'cooldown', 'streak'))
        self.profile_targets_tree.heading('idx',  text='#', command=lambda: self._sort_profile_targets('idx'))
        self.profile_targets_tree.heading('name', text='Имя / username', command=lambda: self._sort_profile_targets('name'))
        self.profile_targets_tree.heading('ready', text='Готов', command=lambda: self._sort_profile_targets('ready'))
        self.profile_targets_tree.heading('cooldown', text='Cooldown', command=lambda: self._sort_profile_targets('cooldown'))
        self.profile_targets_tree.heading('streak', text='Streak', command=lambda: self._sort_profile_targets('streak'))
        self.profile_targets_tree.column('idx',  width=40, anchor=tk.CENTER)
        self.profile_targets_tree.column('name', width=200, anchor=tk.W)
        self.profile_targets_tree.column('ready', width=70, anchor=tk.CENTER)
        self.profile_targets_tree.column('cooldown', width=90, anchor=tk.CENTER)
        self.profile_targets_tree.column('streak', width=70, anchor=tk.CENTER)
        targets_wrap.pack(fill=tk.BOTH, expand=True)
        self.profile_targets_tree.bind('<<TreeviewSelect>>',
                                       lambda _e: self._on_target_selected())

        # ── Target tools panel ────────────────────────────────────────────────
        tools_outer = tk.Frame(self.profiles_tab, bg=C['surface'],
                               highlightthickness=1, highlightbackground=C['border'])
        tools_outer.grid(row=1, column=0, sticky='ew', pady=(8, 0))

        # header
        tools_hdr = tk.Frame(tools_outer, bg=C['panel'])
        tools_hdr.pack(fill=tk.X)
        tk.Label(tools_hdr, text='🔧  Инструменты адресата',
                 bg=C['panel'], fg=C['text_head'],
                 font=FONT_BOLD, padx=12, pady=7).pack(side=tk.LEFT)
        self._target_tools_name_lbl = tk.Label(
            tools_hdr, text='— выберите адресата в списке выше —',
            bg=C['panel'], fg=C['muted'], font=FONT_SMALL, padx=6, pady=7)
        self._target_tools_name_lbl.pack(side=tk.LEFT)

        # body — grid: col0=status card, col1=actions
        tools_body = tk.Frame(tools_outer, bg=C['surface'])
        tools_body.pack(fill=tk.X, padx=12, pady=8)
        tools_body.columnconfigure(1, weight=1)

        # ── Status display ───────────────────────────────────────────────────
        status_card = tk.Frame(tools_body, bg=C['bg'],
                               highlightthickness=1, highlightbackground=C['border'])
        status_card.grid(row=0, column=0, sticky='nsew', padx=(0, 16), pady=2)

        self._tgt_streak_lbl = tk.Label(
            status_card, text='—', bg=C['bg'],
            fg=C['accent'], font=('Segoe UI', 22, 'bold'), padx=16, pady=4)
        self._tgt_streak_lbl.pack()
        tk.Label(status_card, text='текущая серия 🔥',
                 bg=C['bg'], fg=C['muted'], font=('Segoe UI', 8), padx=16).pack()

        tk.Frame(status_card, bg=C['border'], height=1).pack(fill=tk.X, pady=2)

        self._tgt_cooldown_lbl = tk.Label(
            status_card, text='—', bg=C['bg'],
            fg=C['text_sub'], font=FONT_SMALL, padx=16, pady=2)
        self._tgt_cooldown_lbl.pack()

        self._tgt_ready_lbl = tk.Label(
            status_card, text='', bg=C['bg'],
            font=('Segoe UI', 9, 'bold'), padx=16, pady=2)
        self._tgt_ready_lbl.pack()

        # ── Actions ──────────────────────────────────────────────────────────
        actions = tk.Frame(tools_body, bg=C['surface'])
        actions.grid(row=0, column=1, sticky='nsew')

        quick_actions = tk.Frame(actions, bg=C['surface'])
        quick_actions.pack(fill=tk.X, pady=(0, 10))
        tk.Button(quick_actions, text='📋  Скопировать имя адресата',
            command=self.copy_selected_target_name,
            bg='#475569', fg='white', activebackground='#334155',
            relief='flat', font=FONT_BOLD, padx=10, pady=5,
            cursor='hand2', bd=0, highlightthickness=0).pack(anchor='w')

        # Section 1: cooldown reset
        sec1 = tk.Frame(actions, bg=C['surface'])
        sec1.pack(fill=tk.X, pady=(0, 10))
        tk.Label(sec1, text='Сброс cooldown',
                 bg=C['surface'], fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w')
        tk.Label(sec1,
                 text='Удаляет last_send файл — cooldown обнуляется. Серия (streak) не затрагивается.',
                 bg=C['surface'], fg=C['muted'], font=FONT_SMALL).pack(anchor='w', pady=(0, 4))
        self._btn_reset_cooldown = tk.Button(
            sec1, text='🗑  Сбросить cooldown (удалить последнее отправленное)',
            command=self._action_reset_cooldown,
            bg=C['warning'], fg='white', activebackground='#d97706',
            relief='flat', font=FONT_BOLD, padx=12, pady=7,
            cursor='hand2', bd=0, highlightthickness=0)
        self._btn_reset_cooldown.pack(anchor='w')

        tk.Frame(actions, bg=C['border'], height=1).pack(fill=tk.X, pady=(4, 10))

        # Section 2: streak edit — compact single row
        sec2 = tk.Frame(actions, bg=C['surface'])
        sec2.pack(fill=tk.X)
        streak_hdr = tk.Frame(sec2, bg=C['surface'])
        streak_hdr.pack(fill=tk.X)
        tk.Label(streak_hdr, text='Изменить серию',
                 bg=C['surface'], fg=C['text_sub'], font=FONT_BOLD).pack(side=tk.LEFT)
        tk.Label(streak_hdr,
                 text='  (записывает новое значение в stats-файл, без отправки)',
                 bg=C['surface'], fg=C['muted'], font=FONT_SMALL).pack(side=tk.LEFT)

        streak_row = tk.Frame(sec2, bg=C['surface'])
        streak_row.pack(anchor='w', pady=(4, 0))
        tk.Label(streak_row, text='Новое значение:',
                 bg=C['surface'], fg=C['text_sub'], font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 6))
        self._streak_spinbox_var = tk.StringVar(value='0')
        self._streak_spinbox = ttk.Spinbox(
            streak_row, from_=0, to=9999, width=7,
            textvariable=self._streak_spinbox_var)
        self._streak_spinbox.pack(side=tk.LEFT, padx=(0, 8))
        self._btn_set_streak = tk.Button(
            streak_row, text='✏ Применить',
            command=self._action_set_streak,
            bg=C['accent'], fg='white', activebackground='#4f46e5',
            relief='flat', font=FONT_BOLD, padx=10, pady=5,
            cursor='hand2', bd=0, highlightthickness=0)
        self._btn_set_streak.pack(side=tk.LEFT)

        # store selected target name
        self._selected_target_name: str | None = None

    # ══════════════════════════════════════════════════════════════════════════
    # Logs tab
    # ══════════════════════════════════════════════════════════════════════════
    def _build_logs_tab(self) -> None:
        toolbar = tk.Frame(
            self.logs_tab,
            bg=C['surface'],
            highlightthickness=0,
            bd=0,
        )
        toolbar.pack(fill=tk.X, pady=(0, 6))
        toolbar.columnconfigure(0, weight=1)

        # ── controls row ──────────────────────────────────────────────────────
        ctrl = tk.Frame(toolbar, bg=C['surface'])
        ctrl.grid(row=0, column=0, sticky='ew', padx=8, pady=(8, 4))
        ctrl.columnconfigure(0, weight=1)

        ctrl_left = tk.Frame(ctrl, bg=C['surface'])
        ctrl_left.pack(side=tk.LEFT, anchor='w')
        ctrl_right = tk.Frame(ctrl, bg=C['surface'])
        ctrl_right.pack(side=tk.RIGHT, anchor='e')

        tk.Label(ctrl_left, text='Лог', bg=C['surface'], fg=C['text_main'], font=FONT_SMALL).pack(side=tk.LEFT)
        self.log_combo_var.set(dict(LOG_OPTIONS).get(self.selected_log_var.get(), 'Worker stdout'))
        self.log_combo = self._toolbar_dropdown(
            ctrl_left,
            self.log_combo_var,
            [label for _key, label in LOG_OPTIONS],
            lambda _value: self._on_log_selected(),
            width=20,
        )
        self.log_combo.pack(side=tk.LEFT, padx=(6, 10))

        tk.Label(ctrl_left, text='Фильтр', bg=C['surface'], fg=C['text_main'], font=FONT_SMALL).pack(side=tk.LEFT)
        self.log_filter_combo = self._toolbar_dropdown(
            ctrl_left,
            self.log_filter_var,
            list(LOG_FILTER_OPTIONS),
            lambda value: self.set_log_filter_mode(value),
            width=12,
        )
        self.log_filter_combo.pack(side=tk.LEFT, padx=(6, 10))

        tk.Label(ctrl_left, text='Строк', bg=C['surface'], fg=C['text_main'], font=FONT_SMALL).pack(side=tk.LEFT)
        ttk.Spinbox(
            ctrl_left,
            from_=40,
            to=700,
            increment=20,
            textvariable=self.log_line_limit_var,
            width=7,
            command=self.refresh_logs_only,
        ).pack(side=tk.LEFT, padx=(6, 10))

        tk.Checkbutton(
            ctrl_left,
            text='Автоскролл',
            variable=self.log_autoscroll_var,
            bg=C['surface'],
            fg=C['text_main'],
            activebackground=C['surface'],
            activeforeground=C['text_main'],
            selectcolor=C['surface'],
            relief='flat',
            bd=0,
            highlightthickness=0,
            font=FONT_SMALL,
        ).pack(side=tk.LEFT, padx=(0, 10))

        self._toolbar_action(ctrl_right, '↻', 'Обновить', self.refresh_logs_only, width=14).pack(side=tk.LEFT, padx=(0, 6))
        self._toolbar_action(ctrl_right, '📄', 'Открыть файл', self.open_selected_log, width=16).pack(side=tk.LEFT, padx=(0, 6))
        self._toolbar_action(ctrl_right, '📁', 'Папка логов', self.open_selected_log_folder, width=15).pack(side=tk.LEFT)

        # ── quick filters row ─────────────────────────────────────────────────
        filters = tk.Frame(toolbar, bg=C['surface'])
        filters.grid(row=1, column=0, sticky='ew', padx=8, pady=(0, 4))
        tk.Label(filters, text='Быстрые фильтры', bg=C['surface'], fg=C['text_sub'], font=FONT_SMALL).pack(side=tk.LEFT, padx=(0, 8))
        self._quick_filter_button(filters, 'Все', 'all').pack(side=tk.LEFT)
        self._quick_filter_button(filters, 'ERROR', 'errors').pack(side=tk.LEFT, padx=(6, 0))
        self._quick_filter_button(filters, 'WARNING', 'warnings').pack(side=tk.LEFT, padx=(6, 0))
        self._quick_filter_button(filters, 'SUCCESS', 'success').pack(side=tk.LEFT, padx=(6, 0))
        self._quick_filter_button(filters, 'Важное', 'important').pack(side=tk.LEFT, padx=(6, 0))

        # ── search row ────────────────────────────────────────────────────────
        srch = tk.Frame(toolbar, bg=C['surface'])
        srch.grid(row=2, column=0, sticky='ew', padx=8, pady=(0, 8))
        srch.columnconfigure(1, weight=1)

        tk.Label(srch, text='Поиск', bg=C['surface'], fg=C['text_main'], font=FONT_SMALL).grid(row=0, column=0, sticky='w', padx=(0, 6))
        entry = ttk.Entry(srch, textvariable=self.log_search_var)
        entry.grid(row=0, column=1, sticky='ew')
        self.log_search_entry = entry
        entry.bind('<Return>', lambda _e: self.refresh_logs_only())

        srch_right = tk.Frame(srch, bg=C['surface'])
        srch_right.grid(row=0, column=2, sticky='e', padx=(10, 0))
        self._toolbar_action(srch_right, '✎', 'Применить', self.refresh_logs_only, width=14).pack(side=tk.LEFT, padx=(0, 6))
        self._toolbar_action(srch_right, '✕', 'Сбросить', self.clear_log_search, width=14).pack(side=tk.LEFT, padx=(0, 6))
        self._toolbar_action(
            srch_right,
            '📋',
            'Копировать',
            lambda: self._copy_text_widget(self.logs_text, copy_all=True),
            width=14,
        ).pack(side=tk.LEFT)

        chips = tk.Frame(self.logs_tab, bg=C['surface'])
        chips.pack(fill=tk.X, pady=(0, 6))
        self._log_chip_labels = {
            'errors': self._make_pill(chips, self.log_chip_vars['errors'], C['danger']),
            'warnings': self._make_pill(chips, self.log_chip_vars['warnings'], C['warning']),
            'successes': self._make_pill(chips, self.log_chip_vars['successes'], C['ok']),
            'last_error': tk.Label(chips, textvariable=self.log_chip_vars['last_error'], bg=C['surface'], fg=C['text_sub'], padx=10, pady=4, font=FONT_SMALL, highlightthickness=1, highlightbackground=C['border']),
        }
        self._log_chip_labels['errors'].pack(side=tk.LEFT)
        self._log_chip_labels['warnings'].pack(side=tk.LEFT, padx=(8, 0))
        self._log_chip_labels['successes'].pack(side=tk.LEFT, padx=(8, 0))
        self._log_chip_labels['last_error'].pack(side=tk.LEFT, padx=(8, 0), fill=tk.X, expand=True)
        self._refresh_log_quick_filters()

        self.log_summary_label = ttk.Label(
            self.logs_tab, text='—', style='Hint.TLabel',
            wraplength=1200, justify=tk.LEFT)
        self.log_summary_label.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(self.logs_tab, textvariable=self.logs_hint_var, style='Hint.TLabel', wraplength=1200, justify=tk.LEFT).pack(fill=tk.X, pady=(0, 6))

        self.logs_text = UnifiedScrolledText(
            self.logs_tab, wrap=tk.NONE, font=FONT_MONO, relief=tk.FLAT, bg=C['surface'], bd=0,
            highlightthickness=1, highlightbackground=C['border'])
        self.logs_text.pack(fill=tk.BOTH, expand=True)
        self.logs_text.tag_configure('meta',      foreground=C['text_main'])
        self.logs_text.tag_configure('debug',     foreground=C['text_main'])
        self.logs_text.tag_configure('info',      foreground=C['text_main'])
        self.logs_text.tag_configure('error',     foreground='#b91c1c')
        self.logs_text.tag_configure('warning',   foreground='#b45309')
        self.logs_text.tag_configure('success',   foreground='#15803d')
        self.logs_text.tag_configure('highlight', background='#fef08a')

    # ══════════════════════════════════════════════════════════════════════════
    # Diagnostics tab  (merged: issues + deps + recs + files + raw)
    # ══════════════════════════════════════════════════════════════════════════
    def _build_diagnostics_tab(self) -> None:
        # inner notebook to avoid visual clutter
        inner = ttk.Notebook(self.diagnostics_tab, style='Inner.TNotebook')
        inner.pack(fill=tk.BOTH, expand=True)

        sig_tab   = ttk.Frame(inner, padding=8, style='Root.TFrame')
        files_tab = ttk.Frame(inner, padding=8, style='Root.TFrame')
        raw_tab   = ttk.Frame(inner, padding=8, style='Root.TFrame')
        inner.add(sig_tab,   text='Сигналы')
        inner.add(files_tab, text='Файлы')
        inner.add(raw_tab,   text='RAW')

        # ── Signals sub-tab ───────────────────────────────────────────────────
        sig_chips = tk.Frame(sig_tab, bg=C['bg'])
        sig_chips.pack(fill=tk.X, pady=(0, 8))
        self._signal_chip_labels = {
            'critical': self._make_pill(sig_chips, self.signal_chip_vars['critical'], C['danger']),
            'warning': self._make_pill(sig_chips, self.signal_chip_vars['warning'], C['warning']),
            'info': self._make_pill(sig_chips, self.signal_chip_vars['info'], C['accent2']),
            'ok': tk.Label(sig_chips, textvariable=self.signal_chip_vars['ok'], bg=C['ok_bg'], fg=C['ok'], padx=10, pady=4, font=('Segoe UI', 9, 'bold')),
        }
        self._signal_chip_labels['critical'].pack(side=tk.LEFT)
        self._signal_chip_labels['warning'].pack(side=tk.LEFT, padx=(8, 0))
        self._signal_chip_labels['info'].pack(side=tk.LEFT, padx=(8, 0))
        self._signal_chip_labels['ok'].pack(side=tk.LEFT, padx=(8, 0))

        top = tk.Frame(sig_tab, bg=C['surface'])
        top.pack(fill=tk.BOTH, expand=True)
        top.columnconfigure(0, weight=7)
        top.columnconfigure(1, weight=5)
        top.rowconfigure(0, weight=1)

        issues_box_card = tk.Frame(top, bg=C['surface'],
                               highlightthickness=1, highlightbackground=C['border'])
        issues_box_card.grid(row=0, column=0, sticky='nsew', padx=(0, 10))
        tk.Label(issues_box_card, text='Сигналы и проблемы', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w', padx=10, pady=(7,3))
        tk.Frame(issues_box_card, bg=C['border'], height=1).pack(fill=tk.X)
        issues_box = tk.Frame(issues_box_card, bg=C['surface'])
        issues_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        issues_wrap, self.issues_tree = self._make_tree_with_scrollbar(
            issues_box, columns=('severity', 'title', 'details'))
        issues_wrap.pack(fill=tk.BOTH, expand=True)
        for col, title, width in (('severity', 'Уровень', 90),
                                   ('title',    'Сигнал',  200),
                                   ('details',  'Подробности', 500)):
            self.issues_tree.heading(col, text=title)
            self.issues_tree.column(col, width=width,
                                    anchor=tk.W if col != 'severity' else tk.CENTER)

        right_col = tk.Frame(top, bg=C['bg'])
        right_col.grid(row=0, column=1, sticky='nsew')
        right_col.columnconfigure(0, weight=1)
        right_col.rowconfigure(0, weight=4, minsize=140)
        right_col.rowconfigure(1, weight=6, minsize=220)

        deps_box_card = tk.Frame(right_col, bg=C['surface'],
                               highlightthickness=1, highlightbackground=C['border'])
        deps_box_card.grid(row=0, column=0, sticky='nsew')
        tk.Label(deps_box_card, text='Зависимости', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w', padx=10, pady=(7,3))
        tk.Frame(deps_box_card, bg=C['border'], height=1).pack(fill=tk.X)
        deps_box = tk.Frame(deps_box_card, bg=C['surface'])
        deps_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        deps_wrap, self.dependencies_tree = self._make_tree_with_scrollbar(
            deps_box, columns=('module', 'status', 'required', 'hint'))
        deps_wrap.pack(fill=tk.BOTH, expand=True)
        for col, title, width in (('module',   'Модуль',  120),
                                   ('status',   'Статус',  90),
                                   ('required', 'Обяз.',   60),
                                   ('hint',     'Зачем',  260)):
            self.dependencies_tree.heading(col, text=title)
            self.dependencies_tree.column(col, width=width, anchor=tk.W)

        rec_box_card = tk.Frame(right_col, bg=C['surface'],
                               highlightthickness=1, highlightbackground=C['border'])
        rec_box_card.grid(row=1, column=0, sticky='nsew', pady=(10, 0))
        tk.Label(rec_box_card, text='Рекомендации', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w', padx=10, pady=(7,3))
        tk.Frame(rec_box_card, bg=C['border'], height=1).pack(fill=tk.X)
        rec_box = tk.Frame(rec_box_card, bg=C['surface'])
        rec_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.recommendations_text = UnifiedScrolledText(
            rec_box, wrap=tk.WORD, font=FONT_MONO, relief=tk.FLAT, bg=C['surface'], bd=0)
        self.recommendations_text.pack(fill=tk.BOTH, expand=True)

        # ── Files sub-tab ─────────────────────────────────────────────────────
        files_toolbar = tk.Frame(files_tab, bg=C['surface'])
        files_toolbar.pack(fill=tk.X, pady=(0, 8))
        ttk.Button(files_toolbar, text='📄 Открыть файл', command=self.open_selected_file).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(files_toolbar, text='📁 Открыть папку', command=self.open_selected_file_folder).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(files_toolbar, text='📋 Копировать путь', command=self.copy_selected_file_path).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(files_toolbar, text='⬇ TXT', command=self.export_diagnostics_text).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(files_toolbar, text='⬇ JSON', command=self.export_diagnostics_json).pack(side=tk.RIGHT)
        ttk.Label(files_tab, textvariable=self.files_hint_var, style='Hint.TLabel', wraplength=1200, justify=tk.LEFT).pack(fill=tk.X, pady=(0, 8))

        files_main = ttk.Panedwindow(files_tab, orient=tk.HORIZONTAL)
        files_main.pack(fill=tk.BOTH, expand=True)
        fleft  = ttk.Frame(files_main)
        fright = ttk.Frame(files_main)
        files_main.add(fleft,  weight=3)
        files_main.add(fright, weight=2)

        files_wrap, self.files_tree = self._make_tree_with_scrollbar(
            fleft, columns=('kind', 'status', 'size', 'modified', 'path'))
        files_wrap.pack(fill=tk.BOTH, expand=True)
        for col, title, width in (('kind',     'Тип',     110),
                                   ('status',   'Статус',  140),
                                   ('size',     'Размер',  90),
                                   ('modified', 'Изменён', 140),
                                   ('path',     'Путь',    460)):
            self.files_tree.heading(col, text=title)
            self.files_tree.column(col, width=width, anchor=tk.W)
        self.files_tree.bind('<<TreeviewSelect>>', lambda _e: self._fill_file_notes())

        note_box_card = tk.Frame(fright, bg=C['surface'],
                               highlightthickness=1, highlightbackground=C['border'])
        note_box_card.pack(fill=tk.BOTH, expand=True)
        tk.Label(note_box_card, text='Что это значит', bg=C['surface'],
                 fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w', padx=10, pady=(7,3))
        tk.Frame(note_box_card, bg=C['border'], height=1).pack(fill=tk.X)
        note_box = tk.Frame(note_box_card, bg=C['surface'])
        note_box.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        self.file_notes_text = UnifiedScrolledText(
            note_box, wrap=tk.WORD, font=FONT_MONO, relief=tk.FLAT, bg=C['surface'], bd=0)
        self.file_notes_text.pack(fill=tk.BOTH, expand=True)

        # ── RAW sub-tab ───────────────────────────────────────────────────────
        raw_toolbar = tk.Frame(raw_tab, bg=C['bg'])
        raw_toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(raw_toolbar, text='⬇ Экспорт JSON', command=self.export_diagnostics_json).pack(side=tk.RIGHT, padx=(0, 6))
        ttk.Button(raw_toolbar, text='📋 Копировать',
                   command=lambda: self._copy_text_widget(
                       self.raw_text, copy_all=True)).pack(side=tk.RIGHT)
        self.raw_text = UnifiedScrolledText(
            raw_tab, wrap=tk.NONE, font=FONT_MONO, relief=tk.FLAT, bg=C['surface'], bd=0,
            highlightthickness=1, highlightbackground=C['border'])
        self.raw_text.pack(fill=tk.BOTH, expand=True)

    # ══════════════════════════════════════════════════════════════════════════
    # Button registration helpers
    # ══════════════════════════════════════════════════════════════════════════
    def _register_action_button(self, parent, text: str, command,
                                 style: str | None = None) -> ttk.Button:
        btn = ttk.Button(parent, text=text, command=command,
                         style=style or 'TButton')
        self._action_buttons.append(btn)
        return btn

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        state = ['!disabled'] if enabled else ['disabled']
        for btn in self._action_buttons:
            try:
                if isinstance(btn, ttk.Button):
                    btn.state(state)
                else:
                    btn.configure(state=tk.NORMAL if enabled else tk.DISABLED)
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # Copy support
    # ══════════════════════════════════════════════════════════════════════════
    def _install_copy_support(self) -> None:
        for w in (self.summary_text, self.current_run_text, self.overview_rec_text, self.activity_text,
                  self.recommendations_text, self.messages_text,
                  self.message_stats_text, self.profile_detail_text,
                  self.logs_text, self.service_notes_text,
                  self.file_notes_text, self.raw_text):
            self._bind_text_copy(w)
        for w in (self.state_tree, self.profiles_tree, self.profile_targets_tree,
                  self.issues_tree, self.dependencies_tree, self.files_tree):
            self._bind_tree_copy(w)

    def _bind_text_copy(self, w: scrolledtext.ScrolledText) -> None:
        menu = tk.Menu(w, tearoff=False)
        menu.add_command(label='Копировать',       command=lambda: self._copy_text_widget(w))
        menu.add_command(label='Копировать всё',   command=lambda: self._copy_text_widget(w, copy_all=True))
        w.bind('<Control-c>', lambda _e, ww=w: self._copy_text_widget(ww) or 'break')
        w.bind('<Button-3>',  lambda e, m=menu: self._show_menu(e, m))

    def _bind_tree_copy(self, w: ttk.Treeview) -> None:
        menu = tk.Menu(w, tearoff=False)
        menu.add_command(label='Копировать выделенное', command=lambda: self._copy_tree_widget(w))
        menu.add_command(label='Копировать всё',        command=lambda: self._copy_tree_widget(w, copy_all=True))
        w.bind('<Control-c>', lambda _e, ww=w: self._copy_tree_widget(ww) or 'break')
        w.bind('<Button-3>',  lambda e, m=menu: self._show_menu(e, m))

    def _show_menu(self, event: tk.Event, menu: tk.Menu) -> str:
        menu.tk_popup(event.x_root, event.y_root)
        return 'break'

    def _copy_to_clipboard(self, text: str) -> None:
        normalized = text.rstrip('\n')
        if not normalized:
            self.set_status('Нечего копировать')
            return
        self.master.clipboard_clear()
        self.master.clipboard_append(normalized)
        self.set_status('Скопировано в буфер обмена')

    def _copy_text_widget(self, w: scrolledtext.ScrolledText,
                          copy_all: bool = False) -> str:
        try:
            text = (w.get(tk.SEL_FIRST, tk.SEL_LAST)
                    if (not copy_all and w.tag_ranges(tk.SEL))
                    else w.get('1.0', tk.END))
        except tk.TclError:
            text = w.get('1.0', tk.END)
        self._copy_to_clipboard(text)
        return 'break'

    def _copy_tree_widget(self, w: ttk.Treeview, copy_all: bool = False) -> str:
        item_ids = (list(w.get_children('')) if copy_all
                    else list(w.selection()) or list(w.get_children('')))
        rows = ['\t'.join(w.heading(c, option='text') for c in w['columns'])]
        for iid in item_ids:
            rows.append('\t'.join(str(v) for v in w.item(iid, 'values')))
        self._copy_to_clipboard('\n'.join(rows))
        return 'break'

    def _update_mode_button(self, dry_run: bool) -> None:
        button = getattr(self, 'mode_toggle_btn', None)
        if button is None:
            return
        label = 'Режим: DRY RUN' if dry_run else 'Режим: LIVE'
        base = C['warning'] if dry_run else C['accent']
        try:
            button.configure(text=label, activebackground=self._shade_color(base, -0.04))
            self._queue_widget_bg(button, base)
        except Exception:
            pass

    def toggle_dry_run_mode(self) -> None:
        state = dict(self.adapter.get_control_state() or {})
        new_value = not bool(state.get('dry_run'))
        def action() -> None:
            self.adapter.set_dry_run(new_value)
        message = 'Включён DRY RUN — отправка будет пропускаться.' if new_value else 'Включён LIVE — реальная отправка восстановлена.'
        self._run_action(action, message, 'Ошибка переключения режима')

    def copy_current_tab(self) -> None:
        widget = self._tab_widgets.get(self.tabs.select())
        if widget is None:
            self.set_status('Не удалось определить текущую вкладку')
            return
        (self._copy_tree_widget if isinstance(widget, ttk.Treeview)
         else self._copy_text_widget)(widget, copy_all=True)

    # ══════════════════════════════════════════════════════════════════════════
    # Status / log helpers
    # ══════════════════════════════════════════════════════════════════════════
    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        dot = getattr(self, '_status_dot', None)
        if dot:
            low = text.lower()
            color = (C['danger'] if 'ошибк' in low or 'error' in low
                     else C['warning'] if 'обновляю' in low or 'выполняю' in low
                     else C['ok'])
            self._saved_status_base = color
            try:
                dot.configure(fg=color)
            except Exception:
                pass
        try:
            self.master.update_idletasks()
        except Exception:
            pass

    def _reposition_toasts(self) -> None:
        alive = []
        for toast in list(self._toast_windows):
            try:
                if toast.winfo_exists():
                    alive.append(toast)
            except Exception:
                pass
        self._toast_windows = alive
        if not self._toast_windows:
            return
        try:
            self.master.update_idletasks()
            base_x = self.master.winfo_rootx() + self.master.winfo_width() - 340
            base_y = self.master.winfo_rooty() + self.master.winfo_height() - 86
        except Exception:
            return
        for index, toast in enumerate(reversed(self._toast_windows)):
            try:
                toast.geometry(f'+{base_x}+{base_y - index * 72}')
            except Exception:
                pass

    def _show_toast(self, message: str, level: str = 'info') -> None:
        colors = {
            'info': (C['accent'], '#eef2ff'),
            'success': (C['ok'], C['ok_bg']),
            'warning': (C['warning'], C['warn_bg']),
            'error': (C['danger'], C['danger_bg']),
        }
        fg, bg = colors.get(level, colors['info'])
        toast = tk.Toplevel(self.master)
        toast.overrideredirect(True)
        toast.attributes('-topmost', True)
        try:
            toast.attributes('-alpha', 0.0)
        except Exception:
            pass
        frame = tk.Frame(toast, bg=bg, highlightthickness=1, highlightbackground=fg)
        frame.pack(fill=tk.BOTH, expand=True)
        tk.Label(frame, text='●', bg=bg, fg=fg, font=('Segoe UI', 11, 'bold')).pack(side=tk.LEFT, padx=(10, 8), pady=10)
        tk.Label(frame, text=message, bg=bg, fg=C['text_main'], justify=tk.LEFT,
                 font=FONT_SMALL, wraplength=250).pack(side=tk.LEFT, padx=(0, 12), pady=10)
        self._toast_windows.append(toast)
        self._reposition_toasts()

        def fade(step=0):
            try:
                if not toast.winfo_exists():
                    return
                if step <= 6:
                    try:
                        toast.attributes('-alpha', step / 6)
                    except Exception:
                        pass
                    self.after(35, lambda: fade(step + 1))
                elif step <= 42:
                    self.after(65, lambda: fade(step + 1))
                elif step <= 48:
                    try:
                        toast.attributes('-alpha', max(0.0, 1 - (step - 42) / 6))
                    except Exception:
                        pass
                    self.after(35, lambda: fade(step + 1))
                else:
                    try:
                        toast.destroy()
                    except Exception:
                        pass
                    self._reposition_toasts()
            except Exception:
                pass

        self.after(10, fade)

    def _animate_ui(self) -> None:
        self._pulse_phase = (self._pulse_phase + 1) % 100000
        self._anim_clock += 0.05
        wave = (math.sin(self._anim_clock) + 1.0) / 2.0

        try:
            if getattr(self, '_status_dot', None) is not None:
                self._status_dot.configure(fg=self._shade_color(self._saved_status_base, 0.025 + wave * 0.045))
        except Exception:
            pass

        worker_running = bool(dict(self._last_diag.get('worker') or {}).get('running'))
        telegram_running = bool(dict(self._last_diag.get('telegram_bot') or {}).get('running'))
        health_chip = getattr(self, 'hero_health_chip', None)
        if health_chip is not None:
            base = C['ok'] if worker_running or telegram_running else C['chip_idle']
            pulse = self._shade_color(base, 0.008 + wave * (0.024 if (worker_running or telegram_running) else 0.01))
            self._queue_widget_bg(health_chip, pulse)

        mode_btn = getattr(self, 'mode_toggle_btn', None)
        if mode_btn is not None:
            state = dict(self._last_diag.get('state') or {})
            dry = bool(state.get('dry_run'))
            base = C['warning'] if dry else C['accent']
            pulse = self._shade_color(base, 0.006 + wave * 0.02)
            try:
                mode_btn.configure(activebackground=self._shade_color(base, -0.03))
            except Exception:
                pass
            self._queue_widget_bg(mode_btn, pulse)

        for widget, target in list(self._animated_bg_targets.items()):
            try:
                if not widget.winfo_exists():
                    self._animated_bg_targets.pop(widget, None)
                    continue
            except Exception:
                self._animated_bg_targets.pop(widget, None)
                continue
            self._animate_widget_bg(widget, target, speed=0.18)

        for bar, target in list(self._metric_bar_targets.items()):
            try:
                current = float(bar.cget('value') or 0)
                diff = target - current
                if abs(diff) < 0.2:
                    bar.configure(value=target)
                    continue
                new_value = current + diff * 0.16
                bar.configure(value=max(0, min(100, new_value)))
            except Exception:
                pass
        self.after(self._current_animation_interval_ms(), self._animate_ui)

    def _log_action(self, text: str) -> None:
        ts = datetime.now().strftime('%H:%M:%S')
        self._action_history.insert(0, f'[{ts}] {text}')
        self._action_history = self._action_history[:120]

    # ══════════════════════════════════════════════════════════════════════════
    # Auto-refresh
    # ══════════════════════════════════════════════════════════════════════════
    def _toggle_auto_refresh(self) -> None:
        if self.auto_refresh_var.get():
            self._schedule_refresh()
            self.set_status('Автообновление включено')
        else:
            if self._refresh_after_id:
                self.after_cancel(self._refresh_after_id)
                self._refresh_after_id = None
            self.set_status('Автообновление выключено')

    def _schedule_refresh(self) -> None:
        if not self.auto_refresh_var.get():
            return
        if self._refresh_after_id:
            self.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self.after(
            self._current_refresh_interval_ms(), self._auto_refresh_tick)

    def _auto_refresh_tick(self) -> None:
        self._refresh_after_id = None
        self.refresh_all()
        self._schedule_refresh()

    # ══════════════════════════════════════════════════════════════════════════
    # Data refresh
    # ══════════════════════════════════════════════════════════════════════════
    def refresh_all(self, initial: bool = False) -> None:
        if self._refresh_in_flight:
            return
        self._refresh_in_flight = True
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._last_refresh_started_monotonic = time.monotonic()
        self._schedule_refresh_timeout(generation)
        if not initial:
            self.set_status('Обновляю данные…')

        def worker() -> None:
            try:
                diag = self.adapter.diagnostics()
            except Exception as exc:
                self.master.after(0, lambda g=generation, e=exc: self._on_refresh_failed(g, e))
                return
            self.master.after(0, lambda g=generation, d=diag: self._apply_diag(g, d))

        threading.Thread(target=worker, daemon=True, name='diag-refresh').start()

    def _on_refresh_failed(self, generation: int, exc: Exception) -> None:
        if generation != self._refresh_generation:
            return
        self._cancel_refresh_timeout()
        self._refresh_in_flight = False
        msg = f'Ошибка обновления: {exc}'
        self.set_status(msg)
        self._log_action(msg)

    def _apply_diag(self, generation: int, diag: dict[str, object]) -> None:
        if generation != self._refresh_generation:
            return
        self._cancel_refresh_timeout()
        self._last_diag = diag
        self._refresh_in_flight = False
        self.last_refresh_var.set(
            'Обновлено: ' + datetime.now().strftime('%d.%m.%Y %H:%M:%S'))
        errors = []
        for name, fn in [
            ('header',      lambda: self._update_header(diag)),
            ('overview',    lambda: self._fill_overview(diag)),
            ('services',    lambda: self._fill_services(diag)),
            ('messages',    lambda: self._fill_messages(diag)),
            ('profiles',    lambda: self._fill_profiles()),
            ('logs',        lambda: self._fill_logs(diag)),
            ('diagnostics', lambda: self._fill_diagnostics(diag)),
            ('files',       lambda: self._fill_files(diag)),
            ('raw',         lambda: self._fill_raw(diag)),
        ]:
            try:
                fn()
            except Exception as exc:
                errors.append(f'{name}: {exc}')
        self._restyle_treeviews()
        if errors:
            self.set_status('Ошибки рендера: ' + '; '.join(errors[:2]))
        else:
            self.set_status('Данные обновлены')

    def refresh_logs_only(self) -> None:
        if self._last_diag:
            self._fill_logs(self._last_diag)

    # ══════════════════════════════════════════════════════════════════════════
    # Format helpers
    # ══════════════════════════════════════════════════════════════════════════
    def _format_started(self, value) -> str:
        if not value:
            return '—'
        try:
            return datetime.fromtimestamp(float(value)).strftime('%d.%m %H:%M:%S')
        except Exception:
            return '—'

    def _format_file_size(self, size: int) -> str:
        if size <= 0:
            return '—'
        value = float(size)
        for unit in ('B', 'KB', 'MB', 'GB'):
            if value < 1024 or unit == 'GB':
                return f'{value:.1f} {unit}' if unit != 'B' else f'{int(value)} B'
            value /= 1024
        return f'{value:.1f} GB'

    def _format_timestamp(self, value) -> str:
        if not value:
            return '—'
        try:
            return datetime.fromtimestamp(float(value)).strftime('%d.%m.%Y %H:%M')
        except Exception:
            return '—'

    def _health_state(self, score: int) -> tuple[str, str]:
        if score >= 85:
            return 'Стабильно', 'ok'
        if score >= 65:
            return 'Нужно внимание', 'warning'
        return 'Проблемы', 'danger'

    def _status_text(self, running: bool) -> str:
        return '● Запущен' if running else '○ Остановлен'

    def _format_duration_short(self, value) -> str:
        try:
            seconds = max(0, int(float(value or 0)))
        except Exception:
            return '0м'
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if hours:
            return f'{hours}ч {minutes}м'
        return f'{minutes}м'

    def _format_run_status(self, status: str | None) -> str:
        mapping = {
            'starting': 'Подготовка',
            'running': 'В процессе',
            'paused': 'Пауза',
            'idle': 'Ожидание',
            'offline': 'Оффлайн',
            'auth_failed': 'Ошибка авторизации',
            'unknown': 'Нет данных',
        }
        return mapping.get(str(status or '').strip().lower(), str(status or 'Нет данных'))

    def _format_target_cooldown_short(self, state: dict[str, object]) -> str:
        if not state.get('last_send_at'):
            return '—'
        if state.get('ready'):
            return 'Готов'
        left_s = int(state.get('cooldown_left_s') or 0)
        hours = left_s // 3600
        minutes = (left_s % 3600) // 60
        return f'{hours}ч {minutes}м'

    def _selected_file_reference(self) -> str | None:
        selected = self.files_tree.selection()
        if not selected:
            return None
        values = self.files_tree.item(selected[0], 'values')
        if not values:
            return None
        return str(values[-1])

    def _resolve_file_reference(self, raw_ref: str | None) -> Path | None:
        if not raw_ref:
            return None
        path = Path(raw_ref)
        if path.is_absolute():
            return path
        if raw_ref == 'telegram_bot_v2.lock':
            return self.adapter.telegram_lock_path
        return (self.adapter.base_dir / path).resolve()

    def _build_diagnostics_text_report(self) -> str:
        diag = self._last_diag or {}
        parts = [
            'TikTok Heart Bot — diagnostics export',
            '',
            '=== Summary ===',
            self.summary_text.get('1.0', tk.END).strip(),
            '',
            '=== Current run ===',
            self.current_run_text.get('1.0', tk.END).strip(),
            '',
            '=== Recommendations ===',
            self.overview_rec_text.get('1.0', tk.END).strip(),
            '',
            '=== Activity ===',
            self.activity_text.get('1.0', tk.END).strip(),
            '',
            '=== Diagnostics JSON ===',
            json.dumps(diag, ensure_ascii=False, indent=2),
        ]
        return "\n".join(parts).strip() + "\n"

    def _update_message_editor_meta(self, text: str | None = None) -> None:
        payload = self.messages_text.get('1.0', tk.END) if text is None else str(text)
        normalized = payload.rstrip('\n')
        lines = normalized.splitlines() if normalized else []
        self.message_editor_meta_var.set(
            f'Строк: {len(lines)}  ·  Символов: {len(normalized)}')
        dirty = bool(self._message_dirty)
        self.message_editor_state_var.set('Состояние: есть несохранённые изменения' if dirty else 'Состояние: сохранено')
        label = getattr(self, '_msg_state_label', None)
        if label is not None:
            try:
                label.configure(
                    bg=C['warn_bg'] if dirty else C['ok_bg'],
                    fg=C['warning'] if dirty else C['ok'])
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: header
    # ══════════════════════════════════════════════════════════════════════════
    def _update_header(self, diag: dict[str, object]) -> None:
        worker = dict(diag.get('worker') or {})
        telegram = dict(diag.get('telegram_bot') or {})
        state = dict(diag.get('state') or {})
        health = dict(diag.get('health') or {})
        profile_name = str(state.get('active_profile') or '—')
        score = int(health.get('score') or 0)
        health_lbl, _ = self._health_state(score)
        w_run = bool(worker.get('running'))
        tg_run = bool(telegram.get('running'))
        self.hero_subtitle_var.set(
            f'Профиль: {profile_name}  ·  '
            f'Worker: {"запущен" if w_run else "остановлен"}  ·  '
            f'Telegram: {"запущен" if tg_run else "остановлен"}'
        )
        self._chip_update(self.hero_worker_chip,
                          f'Worker  {"▲" if w_run else "▼"}',
                          'ok' if w_run else 'danger')
        self._chip_update(self.hero_tg_chip,
                          f'Telegram  {"▲" if tg_run else "▼"}',
                          'ok' if tg_run else 'warning')
        self._chip_update(self.hero_profile_chip,
                          f'👤  {profile_name}', 'info')
        self._chip_update(self.hero_health_chip,
                          f'{score}/100  {health_lbl}',
                          self._health_state(score)[1])
        self._update_mode_button(bool(state.get('dry_run')))

    def _fill_overview(self, diag: dict[str, object]) -> None:
        worker = dict(diag.get('worker') or {})
        telegram = dict(diag.get('telegram_bot') or {})
        state = dict(diag.get('state') or {})
        health = dict(diag.get('health') or {})
        msg_det = dict(diag.get('message_pool_details') or {})
        profiles = dict(diag.get('profiles') or {})
        run = dict(diag.get('run') or {})
        score = int(health.get('score') or 0)

        self.health_score_var.set(str(score))
        self.health_summary_var.set(str(health.get('summary') or '—'))
        self.message_details_var.set(
            f"Уникальных: {msg_det.get('unique_count', 0)}  ·  "
            f"Дублей: {msg_det.get('duplicates', 0)}  ·  "
            f"Пустых: {msg_det.get('blank_lines', 0)}"
        )

        self._draw_health_gauge(score, str(health.get('summary') or '—'))
        self._fill_breakdown(dict(health.get('breakdown') or {}))

        self._update_metric_card(
            self.metric_cards['worker'],
            '● Запущен' if worker.get('running') else '○ Стоп',
            f"PID: {worker.get('pid') or '—'}   Старт: {self._format_started(worker.get('started_at'))}",
            100 if worker.get('running') else 5)
        tg_ready_text = 'Настроен' if diag.get('telegram_ready') else 'Не настроен'
        self._update_metric_card(
            self.metric_cards['telegram'],
            '● Запущен' if telegram.get('running') else '○ Стоп',
            f"{tg_ready_text}   PID: {telegram.get('pid') or '—'}",
            100 if telegram.get('running') else (50 if diag.get('telegram_ready') else 15))
        self._update_metric_card(
            self.metric_cards['profile'],
            str(state.get('active_profile') or '—'),
            f"Профилей: {profiles.get('total', 0)}",
            min(100, 30 + int(profiles.get('total', 0)) * 10))
        self._update_metric_card(
            self.metric_cards['messages'],
            str(msg_det.get('unique_count', 0)),
            f"Макс: {msg_det.get('max_length', 0)} симв.   Ср: {msg_det.get('avg_length', 0)} симв.",
            min(100, 20 + int(msg_det.get('unique_count', 0)) * 4))

        for row in self.state_tree.get_children():
            self.state_tree.delete(row)
        rows = [
            ('Worker', self._status_text(bool(worker.get('running'))),
             f"PID {worker.get('pid') or '—'}  ·  старт {self._format_started(worker.get('started_at'))}"),
            ('Telegram', self._status_text(bool(telegram.get('running'))),
             f"ready: {'да' if diag.get('telegram_ready') else 'нет'}  ·  PID {telegram.get('pid') or '—'}"),
            ('Пауза', 'Вкл' if state.get('paused') else 'Выкл',
             'Пауза мягко останавливает рабочий цикл, не ломая конфигурацию.'),
            ('Текущий прогон', self._format_run_status(run.get('status')),
             f"Текущий адресат: {run.get('current_target') or '—'}  ·  Всего: {run.get('total_targets') or 0}"),
            ('Сообщения', f"{msg_det.get('unique_count', 0)} шт.", self.message_details_var.get()),
            ('Последний успех', '✅ OK' if health.get('recent_success') else '—',
             str(health.get('recent_success') or 'Нет успешных событий в последних логах.')),
            ('Последняя ошибка', '❌ Есть' if health.get('recent_error') else '—',
             str(health.get('recent_error') or 'Свежих ошибок не найдено.')),
        ]
        for row in rows:
            self.state_tree.insert('', tk.END, values=row)

        rec_lines = ['Рекомендации:']
        recommendations = list(health.get('recommendations') or [])
        if recommendations:
            rec_lines.extend(f'• {item}' for item in recommendations)
        else:
            rec_lines.append('• Явных обязательных действий сейчас нет.')
        preflight = dict(diag.get('preflight') or {})
        if preflight.get('issues'):
            rec_lines.extend(['', 'Проверка окружения:'])
            for item in list(preflight.get('issues') or [])[:4]:
                line = f"• [{item.get('level')}] {item.get('title')}: {item.get('details')}"
                rec_lines.append(line)
        self.overview_rec_text.delete('1.0', tk.END)
        self.overview_rec_text.insert(tk.END, '\n'.join(rec_lines))

        run_lines = [
            f"Статус          : {self._format_run_status(run.get('status'))}",
            f"Профиль         : {state.get('active_profile') or '—'}",
            f"Адресат         : {run.get('current_target') or '—'}",
            f"Всего целей     : {run.get('total_targets') or 0}",
            f"Успешно         : {run.get('success_count') or 0}",
            f"Пропущено       : {run.get('skipped_count') or 0}",
            f"Ошибок          : {run.get('failure_count') or 0}",
        ]
        if run.get('started_at'):
            run_lines.append(f"Старт           : {self._format_started(run.get('started_at'))}")
        if run.get('finished_at'):
            run_lines.append(f"Финиш           : {self._format_started(run.get('finished_at'))}")
        if run.get('last_reason'):
            run_lines.extend(['', f"Последняя причина: {run.get('last_reason')}"])
        if not any((run.get('status'), run.get('current_target'), run.get('total_targets'))):
            run_lines = ['Нет активного прогона.', '', 'Когда worker запустится, здесь появятся текущий адресат, счётчики и итог по последнему запуску.']
        self.current_run_text.delete('1.0', tk.END)
        self.current_run_text.insert(tk.END, '\n'.join(run_lines))

        recent_stdout = list((diag.get('recent_worker_stdout') or []))[-10:]
        activity_lines = ['Действия в сессии:']
        if self._action_history:
            activity_lines.extend(self._action_history[:10])
        else:
            activity_lines.append('— Пока без пользовательских действий.')
        activity_lines.extend(['', 'Последние записи worker:'])
        if recent_stdout:
            activity_lines.extend(recent_stdout)
        else:
            activity_lines.append('— Лог ещё не создан или пока пуст.')
        if health.get('recent_error'):
            activity_lines.extend(['', f"Последняя ошибка: {health.get('recent_error')}"])
        self.activity_text.delete('1.0', tk.END)
        self.activity_text.insert(tk.END, '\n'.join(activity_lines))

        summary_lines = [
            f'Итоговая оценка: {score}/100.  {health.get("summary") or ""}',
            '',
            f'Активный профиль : {state.get("active_profile") or "—"}',
            f'Worker           : {"запущен" if worker.get("running") else "остановлен"}  PID: {worker.get("pid") or "—"}',
            f'Telegram         : {"запущен" if telegram.get("running") else "остановлен"}  PID: {telegram.get("pid") or "—"}',
            f'Сообщений в пуле : {msg_det.get("unique_count", 0)} уник., {msg_det.get("duplicates", 0)} дубл.',
            '',
            'Составляющие оценки:',
            '  runtime — живы ли worker и управляющие процессы',
            '  config  — читаются ли JSON-файлы и есть ли обязательные файлы',
            '  content — есть ли нормальный message_pool и профили',
            '  control — готов ли Telegram control, нет ли 409/lock-конфликтов',
        ]
        self.summary_text.delete('1.0', tk.END)
        self.summary_text.insert(tk.END, '\n'.join(summary_lines))

    def _update_metric_card(self, card, value: str, sub: str, progress: int) -> None:
        card['value'].configure(text=value)
        card['sub'].configure(text=sub)
        pct = max(0, min(100, int(progress)))
        bar = card['bar']
        self._metric_bar_targets[bar] = pct
        try:
            style = ('Green.TProgressbar' if pct >= 80
                     else 'Amber.TProgressbar' if pct >= 40
                     else 'Red.TProgressbar')
            bar.configure(style=style)
        except Exception:
            pass

    def _draw_health_gauge(self, score: int, summary: str) -> None:
        c = self.health_canvas
        try:
            W = int(c.cget('width'))
            H = int(c.cget('height'))
        except Exception:
            W, H = 176, 104
        W = max(168, W)
        H = max(104, H)
        cx = W // 2
        cy = max(42, min(H - 44, H // 2 - 8))
        th = max(10, min(12, H // 10))
        R = max(34, min((W - 42) // 2, cy - 8))
        c.configure(width=W, height=H)
        c.delete('all')

        color = (C['ok'] if score >= 85 else
                 C['warning'] if score >= 65 else C['danger'])

        c.create_oval(cx-R-th, cy-R-th, cx+R+th, cy+R+th,
                      fill=C['surface'], outline='')

        c.create_arc(cx-R, cy-R, cx+R, cy+R,
                     start=90, extent=-359.9,
                     style='arc', width=th, outline=C['border'])

        if score > 0:
            extent = -(359.9 * max(0, min(100, score)) / 100)
            c.create_arc(cx-R, cy-R, cx+R, cy+R,
                         start=90, extent=extent,
                         style='arc', width=th, outline=color)

        inner = R - th - 1
        c.create_oval(cx-inner, cy-inner, cx+inner, cy+inner,
                      fill=C['surface'], outline='')

        c.create_text(cx, cy-5, text=str(score),
                      fill=C['text_main'], font=('Segoe UI', 22, 'bold'))
        c.create_text(cx, cy+10, text='из 100',
                      fill=C['muted'], font=('Segoe UI', 8))
        label = ('Стабильно' if score >= 85
                 else 'Нужно внимание' if score >= 65
                 else 'Требует внимания')
        c.create_text(cx, min(H - 10, cy + R + th + 4), text=label, fill=color,
                      font=('Segoe UI', 8, 'bold'), width=max(90, W - 18), justify=tk.CENTER)

    def _fill_breakdown(self, breakdown: dict[str, object]) -> None:
        for child in self.breakdown_frame.winfo_children():
            child.destroy()
        self.breakdown_frame.columnconfigure(1, weight=1)
        labels = [('Runtime', 'runtime'), ('Config', 'config'),
                  ('Content', 'content'), ('Control', 'control')]
        for row, (lbl, key) in enumerate(labels):
            val = int(breakdown.get(key) or 0)
            style = ('Green.TProgressbar' if val >= 80
                     else 'Amber.TProgressbar' if val >= 40
                     else 'Red.TProgressbar')
            tk.Label(self.breakdown_frame, text=lbl,
                     bg=C['surface'], fg=C['text_sub'],
                     font=('Segoe UI', 8), width=7, anchor='w').grid(
                row=row, column=0, sticky='w', padx=(0, 4), pady=1)
            bar = ttk.Progressbar(self.breakdown_frame, orient='horizontal',
                                  mode='determinate', maximum=100)
            bar.grid(row=row, column=1, sticky='ew', pady=1)
            try:
                bar.configure(value=val)
            except Exception:
                pass
            try:
                bar.configure(style=style)
            except Exception:
                pass
            tk.Label(self.breakdown_frame, text=str(val),
                     bg=C['surface'], fg=C['text_sub'],
                     font=('Segoe UI', 8), width=3, anchor='e').grid(
                row=row, column=2, sticky='e', padx=(4, 0))

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: services
    # ══════════════════════════════════════════════════════════════════════════
    def _fill_services(self, diag: dict[str, object]) -> None:
        self._fill_service_card(self.worker_card, dict(diag.get('worker') or {}))
        self._fill_service_card(self.telegram_card, dict(diag.get('telegram_bot') or {}))

        lines = [
            '── Справка ──',
            '• Worker должен быть запущен только в одном экземпляре для активного профиля.',
            '• Telegram control bot опрашивает getUpdates; второй экземпляр даёт HTTP 409 Conflict.',
            '• «Очистить lock» удаляет stale lock-файл, если бот уже не жив, а lock остался.',
            '• «Сбросить флаги» снимает stop_requested / paused, не меняя режим отправки.',
            '',
            '── Preflight ──',
        ]
        preflight = dict(diag.get('preflight') or {})
        for item in list(preflight.get('issues') or []):
            lines.append(f"• [{item.get('level')}] {item.get('title')} — {item.get('details')}")
            if item.get('command'):
                lines.append(f"  Команда: {item.get('command')}")
        if not list(preflight.get('issues') or []):
            lines.append('• Критичных preflight-предупреждений нет.')
        self.service_notes_text.delete('1.0', tk.END)
        self.service_notes_text.insert(tk.END, '\n'.join(lines))

    def _fill_service_card(self, card, payload: dict[str, object]) -> None:
        running = bool(payload.get('running'))
        status_color = C['ok'] if running else C['danger']
        card['status'].configure(
            text=self._status_text(running),
            fg=status_color)
        # update stripe accent color
        stripe = card.get('stripe')
        if stripe:
            try:
                stripe.configure(bg=status_color)
            except Exception:
                pass
        card['pid'].configure(text=f"PID: {payload.get('pid') or '—'}")
        card['started'].configure(
            text=f"Запущен: {self._format_started(payload.get('started_at'))}")
        cmd = payload.get('command') or []
        cmd_text = ' '.join(str(p) for p in cmd) if cmd else '—'
        if len(cmd_text) > 140:
            cmd_text = cmd_text[:137] + '…'
        card['cmd'].configure(text=cmd_text)
        # update sidebar status dot
        key = 'worker' if card is self.worker_card else 'telegram'
        dot = getattr(self, '_sidebar_status_lbls', {}).get(key)
        if dot:
            dot.configure(fg=status_color)

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: messages
    # ══════════════════════════════════════════════════════════════════════════
    def _fill_messages(self, diag: dict[str, object]) -> None:
        latest = self.adapter.get_message_pool_text().rstrip('\n')
        if not self._message_dirty:
            current = self.messages_text.get('1.0', tk.END).rstrip('\n')
            if current != latest:
                self.messages_text.delete('1.0', tk.END)
                self.messages_text.insert('1.0', latest)
                self.messages_text.edit_modified(False)
        details = dict(diag.get('message_pool_details') or {})
        self.message_details_var.set(
            f"Уникальных: {details.get('unique_count', 0)}  ·  "
            f"Дублей: {details.get('duplicates', 0)}  ·  "
            f"Пустых: {details.get('blank_lines', 0)}  ·  "
            f"Комментариев: {details.get('comment_lines', 0)}")
        self._update_message_editor_meta(self.messages_text.get('1.0', tk.END))

        # update stat counters
        for key, lbl in getattr(self, '_msg_stat_labels', {}).items():
            lbl.configure(text=str(details.get(key, 0)))

        # rebuild message pills
        pills_frame = getattr(self, '_msg_pills_frame', None)
        if pills_frame is None:
            return
        for child in pills_frame.winfo_children():
            child.destroy()

        sample = list(details.get('sample') or [])
        unique_count = int(details.get('unique_count', 0))

        # update count label
        count_lbl = getattr(self, '_msg_preview_count', None)
        if count_lbl:
            count_lbl.configure(
                text=f'({unique_count} шт.)' if unique_count else '(пусто)')

        if not sample:
            # empty state card
            empty = tk.Frame(pills_frame, bg='#f8fafc',
                             highlightthickness=1, highlightbackground=C['border'])
            empty.pack(fill=tk.X, padx=12, pady=(16, 4))
            tk.Label(empty, text='💬', bg='#f8fafc',
                     font=('Segoe UI', 32)).pack(pady=(16, 4))
            tk.Label(empty, text='Пул сообщений пуст',
                     bg='#f8fafc', fg=C['text_sub'],
                     font=('Segoe UI', 11, 'bold')).pack()
            tk.Label(empty, text='Добавьте варианты сообщений в редактор слева\n'
                                  '(по одному на строку) и нажмите «Сохранить».',
                     bg='#f8fafc', fg=C['muted'],
                     font=FONT_SMALL, justify=tk.CENTER).pack(pady=(4, 16))
            # also clear legacy stats widget
            self.message_stats_text.delete('1.0', tk.END)
            return

        # pill colors cycling
        pill_colors = [
            ('#eef2ff', '#6366f1'),  # indigo
            ('#dcfce7', '#16a34a'),  # green
            ('#e0f2fe', '#0284c7'),  # sky
            ('#fef3c7', '#d97706'),  # amber
            ('#fce7f3', '#db2777'),  # pink
            ('#f3e8ff', '#9333ea'),  # purple
        ]

        for idx, msg in enumerate(sample):
            bg_color, text_color = pill_colors[idx % len(pill_colors)]
            row = tk.Frame(pills_frame, bg=C['surface'])
            row.pack(fill=tk.X, padx=12, pady=(8, 0))

            # index badge
            badge = tk.Label(row, text=f'{idx + 1}',
                             bg=text_color, fg='white',
                             font=('Segoe UI', 8, 'bold'),
                             width=3, padx=4, pady=2)
            badge.pack(side=tk.LEFT, anchor='n', pady=3)

            # message bubble
            bubble = tk.Frame(row, bg=bg_color,
                               highlightthickness=1,
                               highlightbackground=text_color)
            bubble.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
            display = msg if len(msg) <= 120 else msg[:117] + '…'
            tk.Label(bubble, text=display,
                     bg=bg_color, fg=text_color,
                     font=('Segoe UI', 9), justify=tk.LEFT,
                     wraplength=380, anchor='w',
                     padx=10, pady=6).pack(anchor='w')

        # "and N more" footer
        if unique_count > len(sample):
            more = unique_count - len(sample)
            tk.Label(pills_frame,
                     text=f'… и ещё {more} сообщений в файле',
                     bg=C['surface'], fg=C['muted'],
                     font=FONT_SMALL).pack(anchor='w', padx=16, pady=(8, 4))

        # fill legacy stats text (for copy support)
        lines = [
            'Статистика message_pool.txt',
            f"  Сырьевых строк  : {details.get('raw_lines', 0)}",
            f"  Рабочих строк   : {details.get('usable_count', 0)}",
            f"  Уникальных      : {details.get('unique_count', 0)}",
            f"  Дубликатов      : {details.get('duplicates', 0)}",
            f"  Пустых строк    : {details.get('blank_lines', 0)}",
            f"  Комментариев    : {details.get('comment_lines', 0)}",
            f"  Макс. длина     : {details.get('max_length', 0)} симв.",
            f"  Средняя длина   : {details.get('avg_length', 0)} симв.",
            '', 'Варианты (все):',
        ]
        lines.extend(f'  {item}' for item in sample) if sample else lines.append('  —')
        self.message_stats_text.delete('1.0', tk.END)
        self.message_stats_text.insert(tk.END, '\n'.join(lines))

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: profiles
    # ══════════════════════════════════════════════════════════════════════════
    def _fill_profiles(self) -> None:
        selected = self.profiles_tree.selection()
        profiles = self.adapter.get_profiles()
        for row in self.profiles_tree.get_children():
            self.profiles_tree.delete(row)
        for idx, profile in enumerate(profiles):
            self.profiles_tree.insert(
                '', tk.END, iid=str(idx),
                values=(profile.label,
                        '✔ Да' if profile.active else '—',
                        profile.raw.get('target_count', 0)))
        if profiles:
            active_count = sum(1 for profile in profiles if profile.active)
            self.profile_hint_var.set(f'Профилей: {len(profiles)}  ·  Активных: {active_count}. Выберите профиль слева, чтобы посмотреть адресатов.')
        else:
            self.profile_hint_var.set('Профили не найдены. Проверьте папку profiles и control_state.json.')
        if selected and self.profiles_tree.exists(selected[0]):
            self.profiles_tree.selection_set(selected[0])
        elif self.profiles_tree.get_children():
            self.profiles_tree.selection_set(self.profiles_tree.get_children()[0])
        self._fill_profile_details()

    def _fill_profile_details(self) -> None:
        # Preserve current target selection across refresh
        previous_target: str | None = None
        previous_selection = self.profile_targets_tree.selection()
        if previous_selection:
            values = self.profile_targets_tree.item(previous_selection[0], 'values')
            if values and len(values) >= 2:
                previous_target = str(values[1])

        self.profile_detail_text.delete('1.0', tk.END)
        for row in self.profile_targets_tree.get_children():
            self.profile_targets_tree.delete(row)

        index = self._selected_profile_index()
        if index is None:
            self.profile_detail_text.insert(tk.END, 'Профиль не выбран')
            self._profile_target_rows = []
            self._profile_target_count_lbl.configure(text='(0)')
            return

        profiles = self.adapter.get_profiles()
        if index < 0 or index >= len(profiles):
            self.profile_detail_text.insert(tk.END, 'Профиль не найден')
            self._profile_target_rows = []
            self._profile_target_count_lbl.configure(text='(0)')
            return

        profile = profiles[index]
        targets = profile.raw.get('targets', []) or []
        lines = [
            f'Профиль : {profile.label}',
            f'Ключ    : {profile.key}',
            f'Активный: {"да" if profile.active else "нет"}',
            f'Targets : {profile.raw.get("target_count", 0)}',
            '',
            '── Заметки ──',
            '• active_profile в control_state.json определяет, какой профиль выберет worker при старте.',
            '• Список справа можно фильтровать и сортировать по cooldown / streak / готовности.',
            '',
            '── RAW ──',
            json.dumps(profile.raw.get('source') or {}, ensure_ascii=False, indent=2),
        ]
        self.profile_detail_text.insert(tk.END, '\n'.join(lines))

        query = self.target_search_var.get().strip().lower()
        rows: list[dict[str, object]] = []
        for idx, target in enumerate(targets[:200], start=1):
            label = (
                str(target.get('name') or target.get('username') or target.get('profile_url') or target)
                if isinstance(target, dict)
                else str(target)
            )
            if query and query not in label.lower():
                continue
            state = self.adapter.get_target_state(label, profile.key)
            rows.append({
                'idx': idx,
                'name': label,
                'ready': bool(state.get('ready')),
                'ready_text': 'Да' if state.get('ready') else 'Нет',
                'cooldown': self._format_target_cooldown_short(state),
                'cooldown_seconds': int(state.get('cooldown_left_s') or 0),
                'streak': int(state.get('streak_count') or 0),
            })

        self._profile_target_rows = rows
        self._profile_target_count_lbl.configure(text=f'({len(rows)})')
        total_targets = len(targets[:200])
        if query and rows:
            self.targets_meta_var.set(f'Фильтр: «{query}»  ·  Показано: {len(rows)} из {total_targets}')
        elif query and not rows:
            self.targets_meta_var.set(f'По запросу «{query}» ничего не найдено.')
        elif rows:
            self.targets_meta_var.set(f'Показано адресатов: {len(rows)}. Сортировка: {self._target_sort_column}.')
        else:
            self.targets_meta_var.set('У профиля пока нет адресатов.')
        self._save_ui_state()
        self._render_profile_target_rows(previous_target=previous_target)

    def _render_profile_target_rows(self, previous_target: str | None = None) -> None:
        rows = list(self._profile_target_rows)
        column = self._target_sort_column
        reverse = self._target_sort_desc

        if column == 'name':
            rows.sort(key=lambda row: str(row.get('name') or '').lower(), reverse=reverse)
        elif column == 'ready':
            rows.sort(
                key=lambda row: (0 if row.get('ready') else 1, str(row.get('name') or '').lower()),
                reverse=reverse,
            )
        elif column == 'cooldown':
            rows.sort(
                key=lambda row: (int(row.get('cooldown_seconds') or 0), str(row.get('name') or '').lower()),
                reverse=reverse,
            )
        elif column == 'streak':
            rows.sort(
                key=lambda row: (int(row.get('streak') or 0), str(row.get('name') or '').lower()),
                reverse=reverse,
            )
        else:
            rows.sort(key=lambda row: int(row.get('idx') or 0), reverse=reverse)

        for row_id in self.profile_targets_tree.get_children():
            self.profile_targets_tree.delete(row_id)

        selected_iid = None
        for row in rows:
            iid = self.profile_targets_tree.insert(
                '',
                tk.END,
                values=(row.get('idx'), row.get('name'), row.get('ready_text'), row.get('cooldown'), row.get('streak')),
            )
            if previous_target and str(row.get('name')) == previous_target:
                selected_iid = iid

        if selected_iid:
            self.profile_targets_tree.selection_set(selected_iid)
            self.profile_targets_tree.see(selected_iid)
        elif self.profile_targets_tree.get_children():
            first = self.profile_targets_tree.get_children()[0]
            self.profile_targets_tree.selection_set(first)

    def _sort_profile_targets(self, column: str) -> None:
        if self._target_sort_column == column:
            self._target_sort_desc = not self._target_sort_desc
        else:
            self._target_sort_column = column
            self._target_sort_desc = False
        current = self.profile_targets_tree.selection()
        previous_target = None
        if current:
            values = self.profile_targets_tree.item(current[0], 'values')
            if values and len(values) >= 2:
                previous_target = str(values[1])
        self._render_profile_target_rows(previous_target=previous_target)

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: logs
    # ══════════════════════════════════════════════════════════════════════════
    def _current_log_path(self) -> Path:
        self.adapter._refresh_runtime_paths()
        return {
            'worker_stdout': self.adapter.worker_stdout_path,
            'telegram_log':  self.adapter.telegram_log_path,
            'launcher_log':  self.adapter.launcher_log_path,
            'auth_debug':    self.adapter.auth_debug_log_path,
            'log':           self.adapter.log_path,
        }.get(self.selected_log_var.get(), self.adapter.worker_stdout_path)

    def _on_log_selected(self) -> None:
        selected_label = self.log_combo_var.get().strip()
        for key, label in LOG_OPTIONS:
            if label == selected_label:
                self.selected_log_var.set(key)
                break
        self.refresh_logs_only()

    def set_log_filter_mode(self, mode: object) -> None:
        self.log_filter_var.set(normalize_log_filter_mode(mode))
        self.refresh_logs_only()

    def _apply_log_filter(self, lines: list[str]) -> list[str]:
        mode   = normalize_log_filter_mode(self.log_filter_var.get())
        search = self.log_search_var.get().strip().lower()
        def keep(line: str) -> bool:
            low = line.lower()
            if mode == 'errors'    and not ('error' in low or '[error]' in low):     return False
            if mode == 'warnings'  and not ('warning' in low or '[warning]' in low): return False
            if mode == 'success'   and not ('✅' in line or 'успех' in low or 'success' in low): return False
            if mode == 'important' and not any(
                    t in low for t in ('error', 'warning', 'success', 'успех',
                                       'plan a', 'auth', 'login', 'captcha', '409')
            ) and '✅' not in line:
                return False
            if search and search not in low:
                return False
            return True
        return [line for line in lines if keep(line)]

    def _fill_logs(self, diag: dict[str, object]) -> None:
        path       = self._current_log_path()
        line_limit = max(40, min(700, int(self.log_line_limit_var.get() or 160)))
        filtered   = self._apply_log_filter(
            self.adapter.tail_file(path, lines=line_limit))
        log_summary = dict(dict(diag.get('health') or {}).get('log_summary') or {})
        log_key = self.selected_log_var.get()
        key = ('worker' if log_key in {'worker_stdout', 'log', 'auth_debug'}
               else 'telegram' if log_key == 'telegram_log'
               else 'launcher')
        ts = dict(log_summary.get(key) or {})
        self.log_chip_vars['errors'].set(f"Ошибок: {ts.get('errors', 0)}")
        self.log_chip_vars['warnings'].set(f"Предупреждений: {ts.get('warnings', 0)}")
        self.log_chip_vars['successes'].set(f"Успехов: {ts.get('successes', 0)}")
        self.log_chip_vars['last_error'].set(f"Последняя ошибка: {ts.get('last_error') or '—'}")
        self.log_summary_label.configure(
            text=(f"Ошибок: {ts.get('errors', 0)}  ·  "
                  f"Предупреждений: {ts.get('warnings', 0)}  ·  "
                  f"Успехов: {ts.get('successes', 0)}  ·  "
                  f"Последняя ошибка: {ts.get('last_error') or '—'}"))
        mode = normalize_log_filter_mode(self.log_filter_var.get())
        self.log_filter_var.set(mode)
        self._refresh_log_quick_filters()
        self.logs_hint_var.set(f'Источник: {path}. Фильтр: {mode}. Поиск: {self.log_search_var.get().strip() or '—'}.')
        self._save_ui_state()
        self.logs_text.delete('1.0', tk.END)
        header_start = self.logs_text.index(tk.END)
        self.logs_text.insert(
            tk.END,
            f'===== {path.name} =====\n'
            f'Показано строк: {len(filtered)} из последних {line_limit}\n\n')
        self.logs_text.tag_add('meta', header_start, self.logs_text.index(tk.END))
        search = self.log_search_var.get().strip().lower()
        for line in filtered:
            start = self.logs_text.index(tk.END)
            self.logs_text.insert(tk.END, line + '\n')
            low = line.lower()
            end = self.logs_text.index(tk.END)
            tag = classify_log_line(line)
            if tag:
                self.logs_text.tag_add(tag, start, end)
            if search and search in low:
                self.logs_text.tag_add('highlight', start, end)
        if not filtered:
            empty_start = self.logs_text.index(tk.END)
            self.logs_text.insert(tk.END, '(пусто)\n')
            self.logs_text.tag_add('meta', empty_start, self.logs_text.index(tk.END))
        try:
            self.logs_text.tag_remove(tk.SEL, '1.0', tk.END)
            self.logs_text.mark_set(tk.INSERT, '1.0')
        except Exception:
            pass
        if self.log_autoscroll_var.get():
            self.logs_text.see(tk.END)

    def clear_log_search(self) -> None:
        self.log_search_var.set('')
        self.refresh_logs_only()

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: diagnostics
    # ══════════════════════════════════════════════════════════════════════════
    def _fill_diagnostics(self, diag: dict[str, object]) -> None:
        health = dict(diag.get('health') or {})
        for row in self.issues_tree.get_children():
            self.issues_tree.delete(row)
        for issue in list(health.get('issues') or []):
            self.issues_tree.insert(
                '', tk.END,
                values=(issue.get('severity'), issue.get('title'), issue.get('details')))
        if not self.issues_tree.get_children():
            self.issues_tree.insert('', tk.END,
                                    values=('ok', 'Критичных сигналов нет',
                                            'Система выглядит стабильно.'))

        for row in self.dependencies_tree.get_children():
            self.dependencies_tree.delete(row)
        deps = dict(diag.get('dependencies') or {})
        for item in list(deps.get('modules') or []):
            self.dependencies_tree.insert(
                '', tk.END,
                values=(item.get('module'),
                        'installed' if item.get('installed') else 'missing',
                        'да' if item.get('required') else 'опц.',
                        item.get('hint')))

        severities = {'critical': 0, 'warning': 0, 'info': 0, 'ok': 0}
        for issue in list(health.get('issues') or []):
            severity = str(issue.get('severity') or '').lower()
            if severity in severities:
                severities[severity] += 1
            elif severity in {'error', 'danger'}:
                severities['critical'] += 1
            else:
                severities['info'] += 1
        if not list(health.get('issues') or []):
            severities['ok'] = 1
        self.signal_chip_vars['critical'].set(f"critical: {severities['critical']}")
        self.signal_chip_vars['warning'].set(f"warning: {severities['warning']}")
        self.signal_chip_vars['info'].set(f"info: {severities['info']}")
        self.signal_chip_vars['ok'].set(f"ok: {severities['ok']}")

        lines = []
        if self.startup_errors:
            lines.extend(['Ошибки запуска:',
                           *[f'• {e}' for e in self.startup_errors], ''])
        if self.startup_warnings:
            lines.extend(['Предупреждений запуска:',
                           *[f'• {w}' for w in self.startup_warnings], ''])
        lines.extend([
            '── Пояснение по JSON ──',
            '• «JSON корректен» — только синтаксис, не гарантия правильности полей.',
            '',
        ])
        python_info = dict(deps.get('python') or {})
        lines.extend([
            '── Окружение ──',
            f"Python    : {python_info.get('version') or '—'}",
            f"Executable: {python_info.get('executable') or '—'}",
            f"Platform  : {python_info.get('platform') or '—'}",
            '',
        ])
        commands = dict(deps.get('commands') or {})
        lines.extend([
            '── Команды восстановления ──',
            f"Desktop/tray   : {commands.get('desktop') or '—'}",
            f"Browser/runtime: {commands.get('runtime') or '—'}",
            '',
        ])
        recommendations = list(health.get('recommendations') or [])
        if recommendations:
            lines.append('── Рекомендации ──')
            lines.extend(f'• {item}' for item in recommendations)
        self.recommendations_text.delete('1.0', tk.END)
        self.recommendations_text.insert(tk.END, '\n'.join(lines))

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: files
    # ══════════════════════════════════════════════════════════════════════════
    def _fill_files(self, diag: dict[str, object]) -> None:
        self._file_notes_by_path.clear()
        for row in self.files_tree.get_children():
            self.files_tree.delete(row)
        file_items = list(diag.get('file_details') or [])
        json_items = list(diag.get('json_checks') or [])
        self.files_hint_var.set(f'Артефактов: {len(file_items)}  ·  JSON-проверок: {len(json_items)}. Выберите строку, чтобы увидеть пояснение справа.')
        for item in file_items:
            status = 'OK' if item.get('exists') else '⚠ Не найден'
            path   = str(item.get('path'))
            self.files_tree.insert(
                '', tk.END,
                values=(item.get('kind'), status,
                        self._format_file_size(int(item.get('size') or 0)),
                        self._format_timestamp(item.get('modified_at')), path))
            self._file_notes_by_path[path] = (
                f"Тип   : {item.get('kind')}\n"
                f"Статус: {status}\n"
                f"Путь  : {path}\n\n"
                "Этот артефакт участвует в работе приложения или worker.")
        for item in json_items:
            path  = str(item.get('path'))
            label = str(item.get('label') or
                        ('JSON корректен' if item.get('valid') else '⚠ JSON повреждён'))
            self.files_tree.insert(
                '', tk.END, values=('json', label, '—', '—', path))
            note = [f"Файл  : {path}", f"Статус: {label}", '',
                    str(item.get('explanation') or '')]
            if item.get('error'):
                note.extend(['', f"Ошибка парсинга: {item.get('error')}"])
            self._file_notes_by_path[path] = '\n'.join(note)
        lock_pid  = dict(diag.get('locks') or {}).get('telegram_lock')
        lock_path = 'telegram_bot_v2.lock'
        self.files_tree.insert(
            '', tk.END,
            values=('lock', f'PID {lock_pid or "—"}', '—', '—', lock_path))
        self._file_notes_by_path[lock_path] = (
            'Lock-файл для Telegram control bot.\n'
            'Если бот уже не жив, а lock остался — '
            'очисти его кнопкой «Очистить lock» на вкладке «Сервисы».')
        self._fill_file_notes()

    def _fill_file_notes(self) -> None:
        selected = self.files_tree.selection()
        if not selected:
            rows     = self.files_tree.get_children()
            selected = rows[:1] if rows else []
        path = None
        if selected:
            values = self.files_tree.item(selected[0], 'values')
            if values:
                path = str(values[-1])
        text = self._file_notes_by_path.get(
            path or '',
            'Выбери строку, чтобы увидеть пояснение.')
        self.file_notes_text.delete('1.0', tk.END)
        self.file_notes_text.insert(tk.END, text)

    # ══════════════════════════════════════════════════════════════════════════
    # Fill: raw
    # ══════════════════════════════════════════════════════════════════════════
    def _fill_raw(self, diag: dict[str, object]) -> None:
        self.raw_text.delete('1.0', tk.END)
        self.raw_text.insert(tk.END, json.dumps(diag, ensure_ascii=False, indent=2))

    # ══════════════════════════════════════════════════════════════════════════
    # Profile helpers
    # ══════════════════════════════════════════════════════════════════════════
    def _selected_profile_index(self) -> int | None:
        selected = self.profiles_tree.selection()
        if not selected:
            return None
        return int(selected[0])

    def activate_selected_profile(self) -> None:
        index = self._selected_profile_index()
        if index is None:
            self.set_status('Профиль не выбран')
            return
        self._run_action(
            lambda: self.adapter.set_active_profile(index),
            'Активный профиль обновлён', 'Ошибка выбора профиля')

    # ── Target tools ──────────────────────────────────────────────────────────

    def _on_target_selected(self) -> None:
        """Called when user clicks a target in the targets tree."""
        selected = self.profile_targets_tree.selection()
        if not selected:
            return
        values = self.profile_targets_tree.item(selected[0], 'values')
        if not values or len(values) < 2:
            return
        name = str(values[1])
        self._selected_target_name = name
        self._target_tools_name_lbl.configure(text=f'→  {name}')
        # Show loading state
        self._tgt_streak_lbl.configure(text='…')
        self._tgt_cooldown_lbl.configure(text='Загрузка…')
        self._tgt_ready_lbl.configure(text='', fg=C['muted'])
        self._load_target_state(name)

    def _load_target_state(self, name: str) -> None:
        """Load and display state for the selected target in a background thread.

        Profile index is captured on the main thread to avoid reading GUI state
        from a worker thread (unsafe on Python 3.14 tkinter).
        """
        import threading

        # Capture GUI state on the MAIN thread before spawning worker
        profile_idx = self._selected_profile_index()
        profiles = self.adapter.get_profiles()
        profile_key = (profiles[profile_idx].key
                       if profile_idx is not None and profile_idx < len(profiles)
                       else None)

        def worker():
            try:
                state = self.adapter.get_target_state(name, profile_key)
                self.master.after(0, lambda s=state: self._apply_target_state(s))
            except Exception as exc:
                err = str(exc)
                self.master.after(0, lambda e=err: self._apply_target_state_error(e))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_target_state(self, state: dict) -> None:
        streak  = state.get('streak_count', 0)
        ready   = state.get('ready', True)
        left_h  = float(state.get('cooldown_left_h', 0.0))

        # Update streak display and spinbox
        self._tgt_streak_lbl.configure(text=str(streak))
        try:
            self._streak_spinbox_var.set(str(streak))
        except Exception:
            pass

        # Update cooldown info
        if state.get('last_send_at'):
            import datetime
            ts = datetime.datetime.fromtimestamp(float(state['last_send_at']))
            sent_str = ts.strftime('%d.%m %H:%M')
            if ready:
                self._tgt_cooldown_lbl.configure(text=f'Последняя отправка: {sent_str}')
                self._tgt_ready_lbl.configure(text='✅ Готов к отправке', fg=C['ok'])
            else:
                h = int(left_h)
                m = int((left_h - h) * 60)
                self._tgt_cooldown_lbl.configure(
                    text=f'Отправлено: {sent_str}   Осталось: {h}ч {m}м')
                self._tgt_ready_lbl.configure(text='⏳ На кулдауне', fg=C['warning'])
        else:
            self._tgt_cooldown_lbl.configure(text='Ещё ни разу не отправлялось')
            self._tgt_ready_lbl.configure(text='✅ Готов к отправке', fg=C['ok'])

        # Buttons are always active; target check happens at click time

    def _apply_target_state_error(self, err: str) -> None:
        self._tgt_streak_lbl.configure(text='?')
        self._tgt_cooldown_lbl.configure(text=f'Ошибка чтения состояния: {err}')
        self._tgt_ready_lbl.configure(text='⚠ Нет данных', fg=C['warning'])

    def copy_selected_target_name(self) -> None:
        name = self._selected_target_name
        if not name:
            messagebox.showwarning('Адресат не выбран', 'Сначала выберите адресата в списке.')
            return
        self.master.clipboard_clear()
        self.master.clipboard_append(name)
        self.set_status(f'Имя адресата скопировано: {name}')

    def _action_reset_cooldown(self) -> None:
        name = self._selected_target_name
        if not name:
            from tkinter import messagebox as _mb
            _mb.showwarning('Адресат не выбран',
                            'Сначала выберите адресата в списке «Адресаты» справа.')
            return

        if not messagebox.askyesno(
                'Сброс cooldown',
                f'Удалить файл last_send для «{name}»?\n\n'
                'Таймер cooldown будет обнулён — бот отправит сообщение '
                'при следующем запуске. Серия (streak) не изменится.'):
            return

        profile_idx = self._selected_profile_index()
        profiles    = self.adapter.get_profiles()
        profile_key = (profiles[profile_idx].key
                       if profile_idx is not None and profile_idx < len(profiles)
                       else None)

        result: dict[str, object] = {'count': 0, 'path': ''}

        def _do():
            count, path = self.adapter.reset_target_cooldown(name, profile_key)
            result['count'] = count
            result['path']  = path

        def _msg():
            if result['count']:
                fname = Path(str(result['path'])).name
                return (f'Cooldown сброшен для «{name}». '
                        f'Удалён файл: {fname}. '
                        'Бот отправит сообщение при следующем запуске.')
            return (f'Файл last_send для «{name}» не найден — '
                    'cooldown уже был сброшен или отправки ещё не было.')

        def _after():
            if self._selected_target_name == name:
                self._load_target_state(name)

        self._run_action(_do, _msg, 'Ошибка сброса cooldown', on_success=_after)

    def _action_set_streak(self) -> None:
        name = self._selected_target_name
        if not name:
            from tkinter import messagebox as _mb
            _mb.showwarning('Адресат не выбран',
                            'Сначала выберите адресата в списке «Адресаты» справа.')
            return

        try:
            raw = self._streak_spinbox_var.get()
            new_count = int(str(raw).strip())
        except (ValueError, tk.TclError):
            self.set_status('Введите корректное целое число')
            return
        if new_count < 0:
            self.set_status('Серия не может быть отрицательной')
            return

        profile_idx = self._selected_profile_index()
        profiles    = self.adapter.get_profiles()
        profile_key = (profiles[profile_idx].key
                       if profile_idx is not None and profile_idx < len(profiles)
                       else None)

        result: dict[str, object] = {'count': 0, 'path': ''}

        def _do():
            count, path = self.adapter.set_target_streak(name, new_count, profile_key)
            result['count'] = count
            result['path']  = path

        def _msg():
            fname = Path(str(result['path'])).name if result['path'] else '—'
            return (f'Серия для «{name}» → {new_count} 🔥  '
                    f'(файл: {fname})')

        def _after():
            if self._selected_target_name == name:
                self._load_target_state(name)

        self._run_action(_do, _msg, 'Ошибка изменения серии', on_success=_after)

    # ══════════════════════════════════════════════════════════════════════════
    # Generic action runner
    # ══════════════════════════════════════════════════════════════════════════
    def _run_action(self, action, success_message,
                    error_title: str,
                    on_success=None) -> None:
        """Run *action* in a background thread.

        *success_message* may be either a ``str`` or a zero-argument callable
        that returns a ``str``; the callable form is evaluated **after** the
        action finishes so it can reference mutable result containers set by
        *action*.

        *on_success* is an optional zero-argument callable invoked on the main
        thread after the action succeeds (before the global refresh).
        """
        if self._action_busy:
            self.set_status('Дождись завершения предыдущего действия')
            return
        self._action_busy = True
        self._set_action_buttons_enabled(False)
        self.set_status('Выполняю действие…')

        def worker() -> None:
            try:
                action()
            except Exception as exc:
                self.master.after(
                    0, lambda: self._finish_action(
                        f'{error_title}: {exc}', is_error=True))
                return
            msg = success_message() if callable(success_message) else success_message
            self.master.after(0, lambda: self._finish_action(msg, on_success=on_success))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_action(self, message: str, *, is_error: bool = False, on_success=None) -> None:
        self._action_busy = False
        self._set_action_buttons_enabled(True)
        self.set_status(message)
        self._log_action(message)
        self._show_toast(message, 'error' if is_error else 'success')
        if is_error:
            messagebox.showerror('Ошибка', message)
        else:
            if on_success is not None:
                try:
                    on_success()
                except Exception:
                    pass
        self.refresh_all()

    def reload_messages(self) -> None:
        self._message_dirty = False
        self.message_backup_var.set('Backup: —')
        self.refresh_all()

    def save_messages(self) -> None:
        text = self.messages_text.get('1.0', tk.END)
        self._run_action(
            lambda: self.adapter.save_message_pool_text_raw(text),
            'message_pool.txt сохранён без автонормализации', 'Ошибка сохранения сообщений')
        self._message_dirty = False
        self._update_message_editor_meta(text)

    def create_messages_backup(self) -> None:
        text = self.messages_text.get('1.0', tk.END)
        result: dict[str, str] = {'path': ''}

        def action() -> None:
            result['path'] = str(self.adapter.create_message_pool_backup(text))

        def message() -> str:
            return f'Backup создан: {Path(result["path"]).name}'

        def after() -> None:
            self.message_backup_var.set(f'Backup: {Path(result["path"]).name}')

        self._run_action(action, message, 'Ошибка создания backup', on_success=after)

    def _show_normalize_preview_dialog(self, original_text: str, normalized_text: str) -> tuple[bool, bool]:
        result = {'apply': False, 'backup': True}
        dlg = tk.Toplevel(self.master)
        dlg.title('Предпросмотр нормализации')
        dlg.transient(self.master)
        dlg.grab_set()
        dlg.geometry('980x620')
        dlg.minsize(860, 520)
        dlg.configure(bg=C['bg'])

        orig_stats = self.adapter.get_message_pool_stats_for_text(original_text)
        norm_stats = self.adapter.get_message_pool_stats_for_text(normalized_text)

        tk.Label(
            dlg,
            text=(
                f"Было: {orig_stats.get('usable_count', 0)} рабочих строк / {orig_stats.get('unique_count', 0)} уникальных   →   "
                f"Станет: {norm_stats.get('usable_count', 0)} / {norm_stats.get('unique_count', 0)}"
            ),
            bg=C['bg'], fg=C['text_main'], font=FONT_BOLD,
        ).pack(anchor='w', padx=14, pady=(12, 8))

        compare = ttk.Panedwindow(dlg, orient=tk.HORIZONTAL)
        compare.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 8))
        left = tk.Frame(compare, bg=C['surface'])
        right = tk.Frame(compare, bg=C['surface'])
        compare.add(left, weight=1)
        compare.add(right, weight=1)

        for parent, title, payload in ((left, 'До', original_text), (right, 'После', normalized_text)):
            tk.Label(parent, text=title, bg=C['surface'], fg=C['text_sub'], font=FONT_BOLD).pack(anchor='w', padx=10, pady=(8, 4))
            widget = UnifiedScrolledText(parent, wrap=tk.WORD, font=FONT_MONO,
                                               relief=tk.FLAT, bg=C['surface'], bd=0,
                                               highlightthickness=1, highlightbackground=C['border'])
            widget.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
            widget.insert('1.0', payload.rstrip('\n'))
            widget.configure(state=tk.DISABLED)

        backup_var = tk.BooleanVar(value=True)
        footer = tk.Frame(dlg, bg=C['bg'])
        footer.pack(fill=tk.X, padx=12, pady=(0, 12))
        ttk.Checkbutton(footer, text='Создать backup перед применением', variable=backup_var).pack(side=tk.LEFT)

        def apply_changes() -> None:
            result['apply'] = True
            result['backup'] = bool(backup_var.get())
            dlg.destroy()

        ttk.Button(footer, text='Отмена', command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(footer, text='Применить', command=apply_changes).pack(side=tk.RIGHT, padx=(0, 8))
        self.master.wait_window(dlg)
        return bool(result['apply']), bool(result['backup'])

    def normalize_messages(self) -> None:
        text = self.messages_text.get('1.0', tk.END)
        normalized = self.adapter.normalize_message_pool_text(text)
        if normalized == text.replace('\r\n', '\n'):
            messagebox.showinfo('Нормализация', 'Изменений не требуется — текст уже нормализован.')
            return
        apply_changes, create_backup = self._show_normalize_preview_dialog(text, normalized)
        if not apply_changes:
            return

        result: dict[str, str] = {'backup': ''}

        def action() -> None:
            if create_backup:
                result['backup'] = str(self.adapter.create_message_pool_backup(text))
            self.adapter.save_message_pool_text_raw(normalized)

        def message() -> str:
            if result['backup']:
                return f'Пул сообщений нормализован. Backup: {Path(result["backup"]).name}'
            return 'Пул сообщений нормализован'

        self._run_action(action, message, 'Ошибка нормализации')
        self._message_dirty = False
        self._update_message_editor_meta(normalized)
        if result['backup']:
            self.message_backup_var.set(f'Backup: {Path(result["backup"]).name}')

    def _on_messages_modified(self, _event=None) -> None:
        self._message_dirty = bool(self.messages_text.edit_modified())
        self.messages_text.edit_modified(False)
        self._update_message_editor_meta()

    # ══════════════════════════════════════════════════════════════════════════
    # Service actions
    # ══════════════════════════════════════════════════════════════════════════
    def toggle_pause(self) -> None:
        paused = bool(self.adapter.get_control_state().get('paused'))
        self._run_action(
            lambda: self.adapter.set_paused(not paused),
            'Пауза переключена', 'Ошибка смены паузы')

    def clear_telegram_lock(self) -> None:
        self._run_action(
            self.adapter.clear_telegram_lock,
            'Telegram lock очищен', 'Ошибка очистки Telegram lock')

    def reset_runtime_flags(self) -> None:
        self._run_action(
            self.adapter.reset_runtime_flags,
            'Runtime-флаги сброшены', 'Ошибка сброса runtime-флагов')

    def start_worker(self)         : self._run_action(self.adapter.start_worker,          'Worker запущен',          'Ошибка запуска worker')
    def stop_worker(self)          : self._run_action(self.adapter.stop_worker,           'Worker остановлен',       'Ошибка остановки worker')
    def restart_worker(self)       : self._run_action(self.adapter.restart_worker,        'Worker перезапущен',      'Ошибка перезапуска worker')
    def start_telegram_bot(self)   : self._run_action(self.adapter.start_telegram_bot,    'TG bot запущен',          'Ошибка запуска TG bot')
    def stop_telegram_bot(self)    : self._run_action(self.adapter.stop_telegram_bot,     'TG bot остановлен',       'Ошибка остановки TG bot')
    def restart_telegram_bot(self) : self._run_action(self.adapter.restart_telegram_bot,  'TG bot перезапущен',      'Ошибка перезапуска TG bot')
    def start_all(self)            : self._run_action(self.adapter.start_all,             'Сервисы запущены',        'Ошибка запуска сервисов')
    def stop_all(self)             : self._run_action(self.adapter.stop_all,              'Сервисы остановлены',     'Ошибка остановки сервисов')
    def restart_all(self)          : self._run_action(self.adapter.restart_all,           'Сервисы перезапущены',    'Ошибка перезапуска сервисов')

    # ══════════════════════════════════════════════════════════════════════════
    # Open paths
    # ══════════════════════════════════════════════════════════════════════════
    def open_backups_folder(self) -> None:
        path = self.adapter.base_dir / 'backups'
        path.mkdir(parents=True, exist_ok=True)
        self.open_path(path)

    def open_selected_log(self) -> None:
        self.open_path(self._current_log_path())

    def open_selected_log_folder(self) -> None:
        self.open_path(self._current_log_path().parent)

    def open_selected_log_path(self) -> None:
        self.open_selected_log()

    def open_selected_file(self) -> None:
        path = self._resolve_file_reference(self._selected_file_reference())
        if not path:
            messagebox.showwarning('Файл не выбран', 'Сначала выбери файл на вкладке «Файлы / JSON».')
            return
        self.open_path(path)

    def open_selected_file_folder(self) -> None:
        path = self._resolve_file_reference(self._selected_file_reference())
        if not path:
            messagebox.showwarning('Файл не выбран', 'Сначала выбери файл на вкладке «Файлы / JSON».')
            return
        self.open_path(path.parent)

    def copy_selected_file_path(self) -> None:
        path = self._resolve_file_reference(self._selected_file_reference())
        if not path:
            messagebox.showwarning('Файл не выбран', 'Сначала выбери файл на вкладке «Файлы / JSON».')
            return
        self.clipboard_clear()
        self.clipboard_append(str(path))
        self.set_status('Путь к файлу скопирован')

    def export_diagnostics_json(self) -> None:
        if not self._last_diag:
            self.refresh_all()
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            title='Экспортировать диагностику в JSON',
            defaultextension='.json',
            filetypes=[('JSON files', '*.json'), ('All files', '*.*')],
            initialfile='diagnostics_export.json',
        )
        if not target:
            return
        Path(target).write_text(json.dumps(self._last_diag, ensure_ascii=False, indent=2), encoding='utf-8')
        self.set_status(f'Диагностика экспортирована: {Path(target).name}')

    def export_diagnostics_text(self) -> None:
        if not self._last_diag:
            self.refresh_all()
            return
        target = filedialog.asksaveasfilename(
            parent=self,
            title='Экспортировать диагностику в TXT',
            defaultextension='.txt',
            filetypes=[('Text files', '*.txt'), ('All files', '*.*')],
            initialfile='diagnostics_export.txt',
        )
        if not target:
            return
        Path(target).write_text(self._build_diagnostics_text_report(), encoding='utf-8')
        self.set_status(f'Диагностика экспортирована: {Path(target).name}')

    def open_path(self, path: Path | str) -> None:
        path = Path(path)
        try:
            if sys.platform.startswith('win'):
                os.startfile(str(path))          # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(path)])
            else:
                subprocess.Popen(['xdg-open', str(path)])
        except Exception as exc:
            messagebox.showerror('Ошибка открытия', str(exc))

    # ══════════════════════════════════════════════════════════════════════════
    # Cleanup
    # ══════════════════════════════════════════════════════════════════════════
    def destroy(self) -> None:
        self._save_ui_state()
        if self._refresh_after_id:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        super().destroy()


# ─────────────────────────────────────────────────────────────────────────────
def show_diagnostics_panel(base_dir=None, startup_errors=None,
                            startup_warnings=None) -> None:
    root = tk.Tk()
    DiagnosticsApp(root, base_dir=base_dir,
                   startup_errors=startup_errors,
                   startup_warnings=startup_warnings)
    root.mainloop()
