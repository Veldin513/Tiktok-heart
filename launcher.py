from __future__ import annotations

import importlib
import logging
import traceback
from pathlib import Path

from project_adapter import ProjectAdapter
from telegram_control_bot import TelegramControlBot


class UnifiedBotLauncher:
    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.adapter = ProjectAdapter(base_dir)
        self.adapter.ensure_runtime_files()
        self.logger = self._configure_logger()

    def _configure_logger(self) -> logging.Logger:
        logger = logging.getLogger("unified_bot_launcher")
        if logger.handlers:
            return logger
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(self.adapter.launcher_log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
        return logger

    def _show_diagnostics(self, errors: list[str], warnings: list[str] | None = None) -> None:
        warnings = warnings or []
        self.logger.error("Opening diagnostics panel: %s", " | ".join(errors or ["manual open"]))
        module = importlib.import_module("diagnostics_app")
        panel_fn = getattr(module, "show_diagnostics_panel", None)
        if callable(panel_fn):
            panel_fn(self.adapter.base_dir, startup_errors=errors, startup_warnings=warnings)
            return
        app_cls = getattr(module, "DiagnosticsApp", None)
        tk_module = getattr(module, "tk", None)
        if app_cls is not None and tk_module is not None:
            root = tk_module.Tk()
            app_cls(root, base_dir=self.adapter.base_dir, startup_errors=errors, startup_warnings=warnings)
            root.mainloop()
            return
        raise ImportError("diagnostics_app не содержит совместимый интерфейс")

    def run(self) -> None:
        validation = self.adapter.validate_project()
        telegram_ready, telegram_reason = self.adapter.telegram_bot_ready()

        if not validation.get("ok", True):
            self._show_diagnostics(validation.get("critical_errors", []), validation.get("warnings", []))
            return

        try:
            worker = self.adapter.get_worker_status()
            if not worker.running:
                worker = self.adapter.start_worker()
                self.logger.info("Worker start requested, PID %s", worker.pid)
            else:
                self.logger.info("Worker already running with PID %s", worker.pid)

            if telegram_ready:
                self.logger.info("Starting Telegram control bot")
                TelegramControlBot(self.adapter.base_dir).run_forever()
                return

            self.logger.warning("Telegram control bot disabled: %s", telegram_reason)
        except KeyboardInterrupt:
            self.logger.info("Launcher interrupted by user")
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("Unified launch failed: %s", exc)
            errors = [str(exc)]
            tb = traceback.format_exc().strip().splitlines()
            if tb:
                errors.append(tb[-1])
            self._show_diagnostics(errors, validation.get("warnings", []))


def main() -> None:
    UnifiedBotLauncher().run()


if __name__ == "__main__":
    main()
