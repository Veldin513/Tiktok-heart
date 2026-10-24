const state = {
  diagnostics: null,
  autoRefresh: true,
  autoRefreshIntervalMs: 30000,
  lastRefreshAt: 0,
  theme: (() => {
    try {
      return localStorage.getItem("tiktok-heart-theme") || "light";
    } catch {
      return "light";
    }
  })(),
  refreshTimer: null,
  refreshInFlight: false,
  refreshQueued: false,
  refreshQueuedFull: false,
  lastLiveAt: 0,
  activeQueue: null,
  activeView: "overview",
  selectedProfileKey: null,
  messageLoaded: false,
  messageDirty: false,
  currentLogPath: "",
  logSearchTimer: null,
  selectedBrowserProfile: null,
  scheduleSaveInFlight: false,
  fullDiagnosticsLoaded: false,
  uiMode: (() => {
    try {
      return localStorage.getItem("tiktok-heart-ui-mode") || "simple";
    } catch {
      return "simple";
    }
  })(),
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 3200);
}

async function api(path, options = {}) {
  const method = String(options.method || "GET").toUpperCase();
  const timeoutMs = Number(options.timeoutMs || (method === "GET" ? 20000 : 180000));
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), Math.max(1000, timeoutMs));
  const { timeoutMs: _timeoutMs, headers = {}, signal, ...fetchOptions } = options;
  const requestPath = method === "GET"
    ? `${path}${path.includes("?") ? "&" : "?"}_=${Date.now()}`
    : path;
  try {
    const response = await fetch(requestPath, {
      headers: { "Content-Type": "application/json", ...headers },
      cache: "no-store",
      signal: signal || controller.signal,
      ...fetchOptions,
    });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload.data;
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error("Запрос превысил лимит ожидания");
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

function text(value, fallback = "--") {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

function setText(selector, value) {
  const node = $(selector);
  if (!node) return;
  const next = String(value);
  if (node.textContent !== next) {
    node.textContent = next;
    flashChange(node.closest(".metric-card") || node);
  }
}

function setPlainText(selector, value) {
  const node = $(selector);
  if (!node) return;
  const next = String(value);
  if (node.textContent !== next) {
    node.textContent = next;
  }
}

function formatBytes(value) {
  let size = Number(value || 0);
  if (!size) return "--";
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (size < 1024 || unit === "GB") {
      return unit === "B" ? `${Math.round(size)} B` : `${size.toFixed(1)} ${unit}`;
    }
    size /= 1024;
  }
  return `${size.toFixed(1)} GB`;
}

function backupSummary(count, bytes) {
  const total = Number(count || 0);
  return total > 0 ? `${total} · ${formatBytes(bytes)}` : "нет";
}

function formatDuration(seconds) {
  let left = Math.max(0, Math.round(Number(seconds || 0)));
  if (left <= 0) return "сейчас";
  const hours = Math.floor(left / 3600);
  left -= hours * 3600;
  const minutes = Math.floor(left / 60);
  const secs = left - minutes * 60;
  if (hours > 0) return `${hours} ч ${minutes} мин`;
  if (minutes > 0) return `${minutes} мин ${secs.toString().padStart(2, "0")} с`;
  return `${secs} с`;
}

function compactDateTime(value) {
  const raw = String(value || "").trim();
  const match = raw.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  if (!match) return raw;
  return `${match[3]}.${match[2]} ${match[4]}:${match[5]}`;
}

function scheduleNextRunTimestamp(schedule = {}) {
  const direct = Number(schedule.next_run_timestamp);
  if (Number.isFinite(direct) && direct > 0) return direct * 1000;
  const raw = String(schedule.next_run_time || "").trim();
  const match = raw.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!match) return null;
  const value = new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
    Number(match[6] || 0),
  ).getTime();
  return Number.isFinite(value) ? value : null;
}

function scheduleIsStale(schedule = {}) {
  if (schedule.next_run_stale) return true;
  if (!schedule.installed || schedule.enabled === false || !schedule.every_12_hours) return false;
  const nextRun = scheduleNextRunTimestamp(schedule);
  return nextRun !== null && nextRun < Date.now() - 60000;
}

function scheduleKind(schedule = {}) {
  if (!schedule.installed) return "warn";
  if (scheduleIsStale(schedule)) return "warn";
  return "ok";
}

function scheduleShortLabel(schedule = {}) {
  if (!schedule.installed) return "выключен";
  if (scheduleIsStale(schedule)) return "проверить";
  return compactDateTime(schedule.next_run_time) || "включён";
}

function scheduleMetaLabel(schedule = {}) {
  if (!schedule.installed) return "ручной режим";
  if (scheduleIsStale(schedule)) return "дата в прошлом · пересохрани";
  const trigger = schedule.at_startup ? "старт ПК" : "ручной режим";
  const repeat = schedule.every_12_hours ? "повтор 12 ч" : "без повтора";
  return `${trigger} · ${repeat}`;
}

function setPill(id, label, kind) {
  const node = $(id);
  const previousText = node.textContent;
  const previousClass = node.className;
  node.textContent = label;
  node.title = label;
  node.className = `status-pill ${kind || ""}`.trim();
  if (previousText !== label || previousClass !== node.className) {
    flashChange(node);
  }
}

function healthKind(status) {
  if (status === "critical") return "critical";
  if (status === "warning") return "warning";
  if (status === "ok") return "ok";
  return "info";
}

function badgeKind(status) {
  if (status === "critical" || status === "bad" || status === "error") return "bad";
  if (status === "warning" || status === "warn") return "warn";
  if (status === "ok" || status === "ready") return "ok";
  return "";
}

function flashChange(node) {
  if (!node) return;
  node.classList.remove("changed");
  window.requestAnimationFrame(() => {
    node.classList.add("changed");
    window.setTimeout(() => node.classList.remove("changed"), 560);
  });
}

function applyTheme(theme) {
  const nextTheme = theme === "dark" ? "dark" : "light";
  state.theme = nextTheme;
  document.documentElement.dataset.theme = nextTheme;
  const button = $("#theme-toggle");
  if (button) {
    const dark = nextTheme === "dark";
    button.classList.toggle("on", dark);
    button.setAttribute("aria-pressed", String(dark));
    button.setAttribute("title", dark ? "Темная тема включена" : "Светлая тема включена");
    button.setAttribute("aria-label", dark ? "Темная тема включена" : "Светлая тема включена");
  }
  try {
    localStorage.setItem("tiktok-heart-theme", nextTheme);
  } catch {
    // Local storage can be unavailable in restricted webview modes.
  }
}

function isAdvancedView(view) {
  return ["services", "diagnostics", "logs"].includes(view);
}

function applyUiMode(mode) {
  const nextMode = mode === "advanced" ? "advanced" : "simple";
  state.uiMode = nextMode;
  document.documentElement.dataset.uiMode = nextMode;
  const button = $("#mode-toggle");
  if (button) {
    const advanced = nextMode === "advanced";
    button.textContent = advanced ? "Простой режим" : "Расширенный режим";
    button.setAttribute("aria-pressed", String(advanced));
    button.setAttribute("title", advanced ? "Скрыть технические разделы" : "Показать технические разделы");
  }
  try {
    localStorage.setItem("tiktok-heart-ui-mode", nextMode);
  } catch {
    // The app remains usable when local storage is unavailable.
  }
  if (nextMode === "simple" && isAdvancedView(state.activeView)) {
    activateView("overview");
  }
}

function browserAuthStatus(browser = {}) {
  if (browser.manual_login_pending) return { label: "Ручной вход", kind: "critical", details: "Закройте отдельный Chrome и нажмите «Проверить вход». Worker до проверки остановлен." };
  if (browser.session_import_required || browser.session_validation?.valid === false) return { label: "Нужен вход", kind: "critical", details: "Последняя проверка не подтвердила вход; откройте отдельный Chrome в разделе «Браузер»." };
  if (browser.needs_recovery) return { label: "Профиль не читается", kind: "critical", details: "Windows не может расшифровать данные профиля." };
  if (!browser.default_profile_exists || !browser.cookies_exists) return { label: "Профиль не готов", kind: "warning", details: "Проверьте профиль и перенос входа в разделе «Браузер»." };
  if (browser.session_validation?.valid === true) return { label: "Проверена ранее", kind: "ok", details: `Последняя успешная проверка: ${formatDate(browser.session_validation.checked_at)}. Текущий вход удалённо не проверялся.` };
  return { label: "Вход не проверен", kind: "info", details: "Cookies сохранены; результат проверки входа ещё не записан. Это не означает потерю авторизации." };
}

function pendingHealth() {
  return {
    score: null,
    status: "info",
    label: "Расчёт готовности",
    summary: "Ожидание результата локальной диагностики.",
    issues: [],
    recommendations: [],
  };
}

function renderDiagnostics(diag) {
  state.diagnostics = diag;
  state.fullDiagnosticsLoaded = true;
  renderLiveStatus(diag, { full: true });
  renderHealthSignals((diag.health || {}).signals || []);
  renderRecommendations(diag.health || {});
  renderSelectors(diag);
  renderBrowser(diag.browser_profile || {});
  const selectedBotProfile = $("#bot-profile")?.value || "";
  const browser = diag.browser_profile || {};
  if (selectedBotProfile && browser.profile_key && selectedBotProfile !== browser.profile_key) {
    loadSelectedBrowserProfile().catch((error) => toast(error.message));
  }
  if (!state.messageDirty) {
    renderMessagesStats(diag.message_pool_details || {});
  }
  renderProfiles(diag);
  renderDiagnosticsPanel(diag);
  renderOverviewSummary(diag);
}

function renderLiveStatus(diag, options = {}) {
  renderProfileLoginShortcut(diag);
  const worker = diag.worker || {};
  const telegram = diag.telegram_bot || {};
  const browser = diag.browser_profile || {};
  const schedule = diag.worker_schedule || {};
  const health = diag.health || {};
  const appState = diag.state || {};
  const messages = diag.message_pool_details || {};
  const profiles = diag.profiles || {};
  const run = diag.run || {};
  const queue = diag.queue || {};

  if (options.full) {
    renderHealth(health);
  }

  setPill(
    "#worker-pill",
    worker.running ? `Worker запущен · PID ${worker.pid}` : (schedule.installed ? "Worker ждёт расписание" : "Worker ручной запуск"),
    worker.running || schedule.installed ? "ok" : "info",
  );
  setPill(
    "#telegram-pill",
    telegram.running ? `Telegram запущен · PID ${telegram.pid}` : (diag.telegram_ready ? "Telegram готов" : "Telegram не настроен"),
    telegram.running ? "ok" : "info",
  );
  setPill("#browser-pill", browser.default_profile_exists ? "Профиль Chromium готов" : "Профиль Chromium пуст", browser.default_profile_exists ? "ok" : "warn");
  setPill("#schedule-pill", schedule.installed ? `Автозапуск · ${scheduleShortLabel(schedule)}` : "Автозапуск выключен", scheduleKind(schedule));

  $("#dry-run-button").textContent = appState.dry_run ? "DRY RUN" : "LIVE";
  $("#dry-run-button").classList.toggle("danger", Boolean(appState.dry_run));

  setText("#worker-value", worker.running ? "Запущен" : "Стоп");
  setText("#worker-meta", `PID: ${text(worker.pid)} · старт: ${formatDate(worker.started_at)}`);
  setText("#telegram-value", telegram.running ? "Запущен" : "Стоп");
  setText("#telegram-meta", `PID: ${text(telegram.pid)} · ready: ${diag.telegram_ready ? "да" : "нет"}`);
  setText("#profile-value", text(appState.active_profile));
  const profileValue = $("#profile-value");
  if (profileValue) profileValue.title = text(appState.active_profile);
  setText("#profile-meta", `${profiles.total || 0} профилей · ${browser.cookies_exists ? "cookies есть" : "cookies нет"}`);
  setText("#messages-value", text(messages.unique_count || messages.count || 0, "0"));
  setText("#messages-meta", `дублей: ${messages.duplicates || messages.duplicate_count || 0}`);
  setText("#schedule-value", scheduleShortLabel(schedule));
  const scheduleValue = $("#schedule-value");
  const scheduleStale = scheduleIsStale(schedule);
  if (scheduleValue) scheduleValue.title = scheduleStale
    ? `Просрочено: ${schedule.next_run_time || ""}`
    : (schedule.next_run_time || "");
  setText("#schedule-card-meta", scheduleMetaLabel(schedule));
  setText("#queue-value", queue.ready_count ? `${queue.ready_count} готово` : formatDuration(queue.next_send_in_s));
  setText("#queue-meta", queue.target_count
    ? `${queue.cooldown_count || 0} cooldown · ${queue.next_target || "без очереди"}`
    : "адресатов нет");
  if (browser.size_bytes !== undefined) {
    setText("#browser-size-value", formatBytes(browser.size_bytes));
  }
  setText("#browser-size-meta", `${browser.backup_count || 0} backup · ${browser.cookies_exists ? "cookies есть" : "cookies нет"}`);
  state.activeQueue = queue;

  $("#run-state").textContent = [
    `Статус   : ${text(run.status, "idle")}`,
    `Профиль  : ${text(appState.active_profile)}`,
    `Адресат  : ${text(run.current_target)}`,
    `Всего    : ${text(run.total_targets, 0)}`,
    `Успешно  : ${text(run.success_count, 0)}`,
    `Пропуск  : ${text(run.skipped_count, 0)}`,
    `Ошибок   : ${text(run.failed_count, 0)}`,
    `Очередь   : ${queue.ready_count || 0} готово / ${queue.cooldown_count || 0} cooldown`,
    `Ближайший : ${queue.next_target ? `${queue.next_target} через ${formatDuration(queue.next_send_in_s)}` : "—"}`,
  ].join("\n");

  renderSchedule(schedule);
  if (options.full || Array.isArray(diag.recent_worker_stdout) || Array.isArray(diag.recent_launcher_log)) {
    renderLogs(diag);
  }
  if (options.full || diag.health || Array.isArray(diag.run_history)) {
    renderFocusPanel(diag);
  }
  renderRunProgress(run);
  if (options.full || Array.isArray(diag.run_history)) {
    renderRunHistory(diag.run_history || []);
  }
  if (options.full || diag.performance) {
    renderPerformanceSummary(diag.performance || {});
  }
  if (options.full || diag.worker_lifecycle) {
    renderWorkerLifecycle(diag.worker_lifecycle || {});
  }
}

function renderHealth(health) {
  const hasScore = Number.isFinite(health.score);
  const score = hasScore ? Math.max(0, Math.min(100, health.score)) : 0;
  const kind = healthKind(health.status);
  const panel = $(".health-panel");
  panel.className = `health-panel ${kind}`;
  $("#health-score").textContent = hasScore ? String(score) : "--";
  $("#health-label").textContent = text(health.label, "Проверка");
  $("#health-summary").textContent = text(health.summary, "Нет данных");
  $("#health-score-fill").style.width = `${score}%`;

  const flags = $("#health-flags");
  flags.innerHTML = "";
  const visible = (health.issues || []).filter((item) => item.severity !== "info").slice(0, 3);
  const data = visible.length ? visible : [{ severity: kind, title: kind === "ok" ? "Блокеров нет" : text(health.label) }];
  for (const item of data) {
    const flag = document.createElement("div");
    flag.className = `health-flag ${healthKind(item.severity)}`;
    flag.textContent = item.title || item.severity;
    flags.appendChild(flag);
  }
}

function renderHealthSignals(items) {
  const box = $("#health-signals");
  box.innerHTML = "";
  const data = items.length ? items : [{
    label: "Состояние",
    state: "Нет данных",
    severity: "info",
    details: "Диагностика ещё не вернула сигналы.",
  }];
  for (const item of data.slice(0, 8)) {
    const card = document.createElement("div");
    card.className = `signal-item ${healthKind(item.severity)}`;
    const top = document.createElement("div");
    top.className = "signal-top";
    const label = document.createElement("div");
    label.className = "signal-label";
    label.textContent = text(item.label);
    const stateLabel = document.createElement("div");
    stateLabel.className = "signal-state";
    stateLabel.textContent = text(item.state);
    const details = document.createElement("div");
    details.className = "signal-details";
    details.textContent = text(item.details);
    top.append(label, stateLabel);
    card.append(top, details);
    box.appendChild(card);
  }
}

function renderRecommendations(health) {
  const box = $("#recommendations");
  box.innerHTML = "";
  const issues = (health.issues || []).filter((item) => item.severity === "critical" || item.severity === "warning");
  const data = (health.recommendations || []).length
    ? health.recommendations
    : issues.map((item) => `${item.title}: ${item.details}`);
  const visible = data.length ? data : ["Обязательных действий сейчас нет."];
  for (const item of visible.slice(0, 8)) {
    const div = document.createElement("div");
    div.className = "list-item";
    div.textContent = item;
    box.appendChild(div);
  }
}

function messageStatsFromText(raw) {
  const normalized = String(raw || "").replaceAll("\r\n", "\n");
  const lines = normalized.split("\n");
  const messages = [];
  let blankLines = 0;
  let commentLines = 0;

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      blankLines += 1;
      continue;
    }
    if (trimmed.startsWith("#")) {
      commentLines += 1;
      continue;
    }
    messages.push(trimmed);
  }

  const unique = Array.from(new Set(messages));
  const lengths = unique.map((item) => item.length);
  return {
    exists: true,
    count: unique.length,
    unique_count: unique.length,
    raw_lines: lines.length,
    usable_count: messages.length,
    duplicates: messages.length - unique.length,
    duplicate_count: messages.length - unique.length,
    blank_lines: blankLines,
    comment_lines: commentLines,
    max_length: lengths.length ? Math.max(...lengths) : 0,
    avg_length: lengths.length ? Math.round(lengths.reduce((total, item) => total + item, 0) / lengths.length) : 0,
    sample: unique.slice(0, 8),
  };
}

function renderMessagesStats(stats = {}) {
  const rows = [
    ["Уникальных", stats.unique_count ?? stats.count ?? 0],
    ["Строк", stats.raw_lines ?? 0],
    ["Дублей", stats.duplicates ?? stats.duplicate_count ?? 0],
    ["Пустых", stats.blank_lines ?? 0],
    ["Комментариев", stats.comment_lines ?? 0],
    ["Средняя длина", stats.avg_length ?? 0],
  ];
  const statsBox = $("#message-stats");
  if (statsBox) {
    statsBox.innerHTML = rows.map(([label, value]) => (
      `<div class="stat-card"><div class="stat-value">${escapeHtml(value)}</div><div class="stat-label">${escapeHtml(label)}</div></div>`
    )).join("");
  }

  const sampleBox = $("#message-sample");
  if (!sampleBox) return;
  const sample = Array.isArray(stats.sample) ? stats.sample : [];
  const visible = sample.length ? sample : ["Нет сохранённых сообщений"];
  sampleBox.innerHTML = visible.map((item) => `<div class="list-item">${escapeHtml(item)}</div>`).join("");
}

async function loadMessages(force = false) {
  if (state.messageLoaded && !force) return;
  if (state.messageDirty && !force) return;
  const editor = $("#message-editor");
  if (!editor) return;
  const data = await api("/api/message-pool");
  editor.value = data.text || "";
  state.messageLoaded = true;
  state.messageDirty = false;
  renderMessagesStats(data.stats || {});
  $("#message-editor-state").textContent = "Загружено";
}

function renderProfileLoginShortcut(diag) {
  const button = $("#profile-import-shortcut");
  if (!button) return;
  const profileKey = state.selectedProfileKey || diag.state?.active_profile;
  button.hidden = !profileKey || diag.profile_login_confirmed?.[profileKey] === true;
}

function renderProfiles(diag) {
  const profiles = ((diag.profiles && diag.profiles.items) || []).filter(Boolean);
  const profilesBox = $("#profiles-list");
  const targetsBox = $("#targets-list");
  if (!profilesBox || !targetsBox) return;

  if (!profiles.length) {
    $("#profile-import-shortcut").hidden = true;
    profilesBox.innerHTML = `<div class="list-item">Профили не найдены</div>`;
    targetsBox.innerHTML = `<div class="list-item">Нет адресатов</div>`;
    return;
  }

  const activeProfile = profiles.find((profile) => profile.active) || profiles[0];
  if (!state.selectedProfileKey || !profiles.some((profile) => profile.key === state.selectedProfileKey)) {
    state.selectedProfileKey = activeProfile.key;
  }
  const selectedProfile = profiles.find((profile) => profile.key === state.selectedProfileKey) || activeProfile;
  renderProfileLoginShortcut(diag);
  $("#target-profile-caption").textContent = `Адресаты профиля: ${selectedProfile.label || selectedProfile.key}. Для работы нажмите «Использовать» у нужного профиля.`;

  profilesBox.innerHTML = profiles.map((profile) => {
    const selected = profile.key === selectedProfile.key;
    const targetCount = Number(profile.target_count || (profile.targets || []).length || 0);
    const flags = [
      profile.active ? `<span class="badge ok">активен</span>` : "",
      profile.supports_enabled ? (profile.enabled ? `<span class="badge ok">включён</span>` : `<span class="badge warn">выключен</span>`) : "",
    ].filter(Boolean).join(" ");
    const selectLabel = selected ? "Открыт" : "Открыть";
    const activateControl = profile.active
      ? `<span class="badge ok">текущий профиль</span>`
      : `<button class="btn secondary" data-profile-action="set_active" data-profile-key="${escapeHtml(profile.key)}" data-index="${Number(profile.index || 0)}">Использовать</button>`;
    const toggleControl = !profile.active && profile.supports_enabled
      ? `<button class="btn secondary" data-profile-action="toggle" data-profile-key="${escapeHtml(profile.key)}" data-index="${Number(profile.index || 0)}">${profile.enabled ? "Выключить" : "Включить"}</button>`
      : "";
    return `
      <div class="profile-item ${profile.active ? "active" : ""} ${selected ? "selected" : ""}">
        <div class="profile-top">
          <div>
            <div class="profile-name">${escapeHtml(profile.label || profile.key)}</div>
            <div class="profile-meta">${escapeHtml(profile.key)} · ${targetCount} адресатов</div>
          </div>
          <div>${flags}</div>
        </div>
        <div class="profile-actions">
          <button class="btn secondary" data-profile-select="${escapeHtml(profile.key)}" ${selected ? "disabled" : ""}>${selectLabel}</button>
          ${activateControl}
          ${toggleControl}
        </div>
      </div>
    `;
  }).join("");

  const targets = Array.isArray(selectedProfile.targets) ? selectedProfile.targets : [];
  if (!targets.length) {
    targetsBox.innerHTML = `<div class="list-item">В профиле нет адресатов</div>`;
    return;
  }

  targetsBox.innerHTML = targets.map((target) => {
    const targetState = target.state || {};
    const unconfirmed = Boolean(targetState.delivery_unconfirmed);
    const ready = Boolean(targetState.ready);
    const cooldown = Number(targetState.cooldown_left_h || 0);
    const cooldownLeft = targetState.cooldown_left_text || `${cooldown.toFixed(1)} ч`;
    const nextSend = targetState.next_send_at_text || "";
    const cooldownText = unconfirmed ? "результат отправки неизвестен; проверьте чат перед новой попыткой" : (ready ? "можно отправить сейчас" : `отправка через ${cooldownLeft}`);
    const nextSendText = ready ? "" : ` · ${escapeHtml(nextSend)}`;
    const badge = unconfirmed ? `<span class="badge warn">не подтверждено</span>` : (ready ? `<span class="badge ok">готов</span>` : `<span class="badge warn">cooldown</span>`);
    const streak = Number(targetState.streak_count || 0);
    const stateFiles = targetState.state_files || {};
    const fileCount = (stateFiles.last_send || []).length + (stateFiles.stats || []).length;
    const cooldownProgress = Math.max(0, Math.min(100, Number(targetState.cooldown_progress_percent || 0)));
    return `
      <div class="target-item">
        <div class="target-top">
          <div>
            <div class="target-name">${escapeHtml(target.name || target.url || "target")}</div>
            <div class="target-meta">${escapeHtml(target.url || "")}</div>
          </div>
          <div>${badge}</div>
        </div>
        <div class="target-meta">серия: ${streak} · ${escapeHtml(cooldownText)}${nextSendText} · файлов состояния: ${fileCount}</div>
        <div class="target-cooldown ${ready ? "ready" : ""}">
          <span style="width:${ready ? 100 : cooldownProgress}%"></span>
        </div>
        <div class="target-actions">
          <button class="btn secondary" data-target-action="reset_cooldown" data-unconfirmed="${unconfirmed}" data-profile-key="${escapeHtml(selectedProfile.key)}" data-target-name="${escapeHtml(target.name || target.url || "")}">${unconfirmed ? "Разрешить новую попытку" : "Сброс cooldown"}</button>
          <button class="btn secondary" data-copy-text="${escapeHtml(target.url || target.name || "")}" data-copy-label="Ссылка на адресата">Скопировать ссылку</button>
          <input class="mini-input" type="number" min="0" step="1" value="${streak}" data-streak-input />
          <button class="btn secondary" data-target-action="set_streak" data-profile-key="${escapeHtml(selectedProfile.key)}" data-target-name="${escapeHtml(target.name || target.url || "")}">Серия</button>
        </div>
      </div>
    `;
  }).join("");
}

function renderDiagnosticsPanel(diag) {
  const health = diag.health || {};
  const issuesBox = $("#diagnostic-issues");
  if (issuesBox) {
    const issues = (health.issues || diag.issues || []).filter(Boolean);
    const visible = issues.length ? issues : [{ severity: "ok", title: "Блокеров нет", details: "Диагностика не видит критичных проблем." }];
    issuesBox.innerHTML = visible.map((issue) => `
      <div class="list-item">
        <span class="badge ${badgeKind(issue.severity || issue.level)}">${escapeHtml(issue.severity || issue.level || "info")}</span>
        <strong>${escapeHtml(issue.title || "Сигнал")}</strong>
        <div class="target-meta">${escapeHtml(issue.details || "")}</div>
      </div>
    `).join("");
  }

  const depsBox = $("#dependency-list");
  if (depsBox) {
    const modules = ((diag.dependencies && diag.dependencies.modules) || []).filter(Boolean);
    depsBox.innerHTML = modules.map((item) => `
      <div class="dependency-item">
        <div class="dependency-top">
          <div class="dependency-name">${escapeHtml(item.module)}</div>
          <span class="badge ${item.installed ? "ok" : item.required ? "bad" : "warn"}">${item.installed ? "есть" : "нет"}</span>
        </div>
        <div class="dependency-meta">${escapeHtml(item.hint || "")}</div>
      </div>
    `).join("") || `<div class="list-item">Нет данных по зависимостям</div>`;
  }

  const filesBox = $("#file-list");
  if (filesBox) {
    const files = (diag.file_details || []).filter(Boolean);
    filesBox.innerHTML = files.map((item) => {
      const valid = item.valid !== undefined ? Boolean(item.valid) : Boolean(item.exists);
      return `
        <div class="file-item">
          <div class="file-top">
            <div class="file-name">${escapeHtml(item.file_kind || item.kind || "file")}</div>
            <span class="badge ${valid ? "ok" : "warn"}">${escapeHtml(item.status || (item.exists ? "OK" : "нет"))}</span>
          </div>
          <div class="file-meta">${escapeHtml(item.path || "")}</div>
          <div class="file-meta">${formatBytes(item.size)} · ${formatDate(item.modified_at)} · ${escapeHtml(item.meaning || "")}</div>
          <div class="file-actions">
            <button class="btn secondary mini" data-open-path="${escapeHtml(item.path || "")}">Открыть</button>
            <button class="btn secondary mini" data-open-path="${escapeHtml(item.path || "")}" data-open-parent="1">Папка</button>
            <button class="btn secondary mini" data-copy-path="${escapeHtml(item.path || "")}">Копировать путь</button>
          </div>
        </div>
      `;
    }).join("") || `<div class="list-item">Нет данных по файлам</div>`;
  }

  const raw = $("#raw-diagnostics");
  if (raw) {
    raw.textContent = JSON.stringify(diag, null, 2);
  }
}

function renderFocusPanel(diag = {}) {
  const queue = diag.queue || {};
  const health = diag.health || {};
  const run = diag.run || {};
  const history = Array.isArray(diag.run_history) ? diag.run_history : [];
  const issues = Array.isArray(health.issues) ? health.issues : [];
  const blocker = issues.find((item) => item.severity === "critical" || item.severity === "warning") || issues[0] || null;
  const last = history.length ? history[history.length - 1] : null;
  const runStatus = String(run.status || "unknown");
  const runLabels = {
    success: run.delivery_verified ? `TikTok принял ${Number(run.success_count || 0)}/${Number(run.total_targets || 0)}` : "Успех записан без подтверждения (старая версия)",
    partial: `Частично: ${Number(run.success_count || 0)} успешно`,
    failed: "Запуск завершён с ошибкой",
    skipped: "Запуск пропущен",
    stopped: "Запуск остановлен",
    paused: "Worker на паузе",
    running: "Worker выполняет отправку",
    starting: "Worker запускается",
    idle: "Worker ожидает",
    unknown: "Результата пока нет",
  };
  const reasonLabels = {
    cooldown: "ожидание cooldown",
    no_targets: "нет адресатов",
    offline: "нет подключения к интернету",
    auth_failed: "нужна авторизация",
    auth_check_inconclusive: "страница не проверена; потеря входа не подтверждена",
    delivery_unconfirmed: "результат отправки неизвестен — проверьте чат",
    send_rejected: "TikTok отклонил сообщение",
    send_not_attempted: "отправка не выполнялась",
    chat_not_confirmed: "чат адресата не подтверждён",
    dry_run: "тест без отправки",
    stop_requested: "остановлено пользователем",
    paused: "пауза",
    recent_guard: "защита от повторной отправки",
    unhandled_exception: "необработанная ошибка",
  };
  const runTimestamp = Number(run.completed_at || run.updated_at || 0);
  const runMeta = [
    runTimestamp ? formatDate(runTimestamp) : "",
    Number(run.duration_seconds || 0) > 0 ? `${Number(run.duration_seconds).toFixed(1)} с` : "",
    reasonLabels[run.reason] || run.reason || "",
  ].filter(Boolean).join(" · ");

  const nextTitle = Number(queue.ready_count || 0) > 0
    ? `${queue.ready_count} адресатов готовы`
    : queue.next_target
      ? `Ждать ${formatDuration(queue.next_send_in_s)}`
      : (run.status && run.status !== "idle" ? run.status : "Ожидание расписания");
  const nextMeta = queue.next_target
    ? `${queue.next_target} · ${queue.next_send_at_text || "без точного времени"}`
    : `${Number(queue.target_count || 0)} адресатов · ${Number(queue.cooldown_count || 0)} cooldown`;

  setText("#focus-next", nextTitle);
  setText("#focus-next-meta", nextMeta);
  setText("#focus-blocker", blocker ? blocker.title || blocker.severity : "Блокеров нет");
  setText("#focus-blocker-meta", blocker ? blocker.details || "" : "Критичных действий сейчас нет.");
  setText("#focus-last", runLabels[runStatus] || runStatus || (last ? last.event : "Результата пока нет"));
  setText(
    "#focus-last-meta",
    runMeta || (last
      ? [last.time, reasonLabels[last.reason] || last.reason, last.duration_seconds ? `${last.duration_seconds} с` : ""].filter(Boolean).join(" · ")
      : "Worker ещё не писал историю прогонов."),
  );
}

function renderRunProgress(run = {}) {
  const runProgress = $("#run-progress");
  if (!runProgress) return;
  const total = Number(run.total_targets || 0);
  const done = Number(run.success_count || 0) + Number(run.skipped_count || 0) + Number(run.failed_count || 0);
  const percent = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0;
  runProgress.innerHTML = `
    <div class="progress-head">
      <span>${escapeHtml(run.status || "idle")}</span>
      <span>${percent}%</span>
    </div>
    <div class="progress-track"><span style="width:${percent}%"></span></div>
    <div class="progress-meta">
      <span>ok: ${Number(run.success_count || 0)}</span>
      <span>skip: ${Number(run.skipped_count || 0)}</span>
      <span>fail: ${Number(run.failed_count || 0)}</span>
    </div>
  `;
}

function renderRunHistory(items = []) {
  const runHistory = $("#run-history");
  if (!runHistory) return;
  const recent = Array.isArray(items) ? items.slice(-6).reverse() : [];
  const visible = recent.length ? recent : [{ event: "empty", time: "--", reason: "Истории прогонов пока нет" }];
  runHistory.innerHTML = visible.map((item) => {
    const event = item.event || "event";
    const ok = item.success === true || event === "run_finished";
    const bad = item.success === false || event === "run_failed";
    const kind = ok ? "ok" : bad ? "bad" : "info";
    const title = item.target || event;
    const meta = [
      item.time || "--",
      item.reason || "",
      item.duration_seconds ? `${item.duration_seconds}s` : "",
    ].filter(Boolean).join(" · ");
    return `
      <div class="history-item ${kind}">
        <span>${escapeHtml(title)}</span>
        <small>${escapeHtml(meta)}</small>
      </div>
    `;
  }).join("");
}

function renderWorkerLifecycle(summary = {}) {
  const box = $("#worker-lifecycle");
  if (!box) return;
  const events = Array.isArray(summary.events) ? summary.events.slice(-10).reverse() : [];
  const visible = events.length
    ? events
    : [{ event: "empty", stage: "Истории worker пока нет", time: "--", reason: "Запусти self-test или worker, чтобы увидеть этапы." }];
  box.innerHTML = visible.map((item) => {
    const event = item.event || "event";
    const phase = item.phase || item.stage || event;
    const elapsed = item.elapsed_ms !== undefined ? `${Math.round(Number(item.elapsed_ms || 0))} ms` : "";
    const reason = item.reason || item.status || item.profile_key || "";
    const kind = event === "run_failed" || item.success === false
      ? "bad"
      : Number(item.elapsed_ms || 0) >= 5000
        ? "warn"
        : "ok";
    const meta = [item.time || "--", elapsed, reason].filter(Boolean).join(" · ");
    return `
      <div class="lifecycle-item ${kind}">
        <span>${escapeHtml(phase)}</span>
        <small>${escapeHtml(meta)}</small>
      </div>
    `;
  }).join("");
}

function renderOverviewSummary(diag) {
  const health = diag.health || {};
  const browser = diag.browser_profile || {};
  const schedule = diag.worker_schedule || {};
  const stateData = diag.state || {};
  const messages = diag.message_pool_details || {};
  const worker = diag.worker || {};
  const telegram = diag.telegram_bot || {};
  const run = diag.run || {};
  const app = diag.app || {};
  const summary = $("#setup-summary");
  const report = $("#setup-report");
  const timeline = $("#overview-timeline");
  if (!summary || !report) return;
  const scheduleStale = scheduleIsStale(schedule);

  const cards = [
    ["Готовность", Number.isFinite(health.score) ? `${health.score}/100` : "Расчёт", health.status || "info"],
    ["Авторизация", browserAuthStatus(browser).label, browserAuthStatus(browser).kind],
    ["Автозапуск", schedule.installed ? (scheduleStale ? "проверить" : "включён") : "выключен", scheduleKind(schedule)],
    ["Профиль", stateData.active_profile || "—", "info"],
  ];
  summary.innerHTML = cards.map(([label, value, kind]) => `
    <div class="summary-card ${badgeKind(kind)}">
      <div class="summary-value">${escapeHtml(value)}</div>
      <div class="summary-label">${escapeHtml(label)}</div>
    </div>
  `).join("");

  if (timeline) {
    const recentSuccess = health.recent_success || "нет свежего успеха";
    const recentError = health.recent_error || "нет свежих ошибок";
    const items = [
      ["Worker", worker.running ? `запущен · PID ${worker.pid}` : "ожидает расписание"],
      ["Telegram", telegram.running ? `запущен · PID ${telegram.pid}` : (diag.telegram_ready ? "готов, не запущен" : "не настроен")],
      ["Версия", `${app.name || "TikTok Heart"} ${app.version || "2.0.0"}`],
      ["Последний успех", recentSuccess],
      ["Последняя ошибка", recentError],
      ["Резервные копии профиля", backupSummary(browser.backup_count, browser.backup_bytes)],
    ];
    timeline.innerHTML = items.map(([label, value]) => `
      <div class="timeline-item">
        <div class="timeline-label">${escapeHtml(label)}</div>
        <div class="timeline-value">${escapeHtml(value)}</div>
      </div>
    `).join("");
  }

  renderRunProgress(run);
  renderRunHistory(diag.run_history || []);

  const triggers = [
    schedule.at_startup ? "старт Windows" : "",
    schedule.every_12_hours ? "повтор 12 ч" : "",
  ].filter(Boolean).join(" + ") || "выключен";
  const recommendations = (health.recommendations || []).slice(0, 5);
  report.textContent = [
    `Готовность         : ${Number.isFinite(health.score) ? `${health.score}/100` : "расчёт"} · ${health.summary || "—"}`,
    `Активный профиль  : ${stateData.active_profile || "—"}`,
    `Cookies Chromium   : ${browser.cookies_exists ? "есть" : "нет"}`,
    `Размер профиля     : ${formatBytes(browser.size_bytes)}`,
    `Резервные копии    : ${backupSummary(browser.backup_count, browser.backup_bytes)}`,
    `Сообщения          : ${messages.unique_count || messages.count || 0} уникальных`,
    `Автозапуск         : ${schedule.installed ? (scheduleStale ? "проверить" : "включён") : "выключен"} · ${triggers}`,
    `Следующий запуск   : ${scheduleStale ? "просрочен" : (schedule.next_run_time || "—")}`,
    "",
    "Что сделать",
    ...(recommendations.length ? recommendations.map((item) => `- ${item}`) : ["- Критичных действий сейчас нет."]),
  ].join("\n");
}

function renderPerformanceSummary(performance = {}) {
  const box = $("#performance-summary");
  if (!box) return;
  const workerPerf = performance.worker || {};
  const latest = performance.latest || {};
  const worst = performance.worst || {};
  const events = Array.isArray(performance.events) ? performance.events : [];
  const status = performance.status || "ok";
  const statusKind = status === "critical" ? "bad" : status === "warning" ? "warn" : "ok";
  const statusLabel = status === "critical" ? "критично" : status === "warning" ? "есть задержки" : "спокойно";
  const latestMs = latest.elapsed_ms !== undefined ? `${Math.round(Number(latest.elapsed_ms || 0))} ms` : "--";
  const worstMs = worst.elapsed_ms !== undefined ? `${Math.round(Number(worst.elapsed_ms || 0))} ms` : "--";
  const workerWorst = workerPerf.worst || {};
  const workerWorstMs = workerWorst.elapsed_ms !== undefined ? `${Math.round(Number(workerWorst.elapsed_ms || 0))} ms` : "--";
  const rows = [
    ["Статус", statusLabel, statusKind],
    ["Последний slow", latestMs, statusKind],
    ["Худший", worstMs, statusKind],
    ["Событий", String(performance.slow_count || 0), Number(performance.warning_count || 0) ? "warn" : "ok"],
    ["Worker slow", workerWorstMs, workerPerf.status === "critical" ? "bad" : Number(workerPerf.warning_count || 0) ? "warn" : "ok"],
  ];
  const visibleEvents = events.length
    ? events
    : [{ method: "", path: "Медленных запросов пока нет", elapsed_ms: 0, time: "" }];
  const workerEvents = Array.isArray(workerPerf.events) && workerPerf.events.length
    ? workerPerf.events
    : [{ phase: "worker phase history empty", elapsed_ms: 0, time: "" }];

  box.innerHTML = `
    <div class="performance-cards">
      ${rows.map(([label, value, kind]) => `
        <div class="performance-card ${badgeKind(kind)}">
          <strong>${escapeHtml(value)}</strong>
          <span>${escapeHtml(label)}</span>
        </div>
      `).join("")}
    </div>
    <div class="performance-events">
      ${visibleEvents.map((item) => {
        const title = [item.method, item.path].filter(Boolean).join(" ") || item.path || "--";
        const meta = item.elapsed_ms
          ? `${Math.round(Number(item.elapsed_ms))} ms · ${item.time || ""}`
          : "лог пуст";
        return `
          <div class="performance-event">
            <span>${escapeHtml(title)}</span>
            <small>${escapeHtml(meta)}</small>
          </div>
        `;
      }).join("")}
    </div>
    <div class="performance-events">
      ${workerEvents.map((item) => {
        const title = item.phase || item.stage || "worker";
        const meta = item.elapsed_ms
          ? `${Math.round(Number(item.elapsed_ms))} ms · ${item.time || ""}`
          : "worker phase log пуст";
        return `
          <div class="performance-event">
            <span>${escapeHtml(title)}</span>
            <small>${escapeHtml(meta)}</small>
          </div>
        `;
      }).join("")}
    </div>
  `;
}

function renderBrowser(browser) {
  state.selectedBrowserProfile = browser || {};
  const rows = [
    ["Профиль", browser.profile_key],
    ["Данные профиля", browser.exists ? "есть" : "нет"],
    ["Размер", formatBytes(browser.size_bytes)],
    ["Основной профиль", browser.default_profile_exists ? "готов" : "не найден"],
    ["Настройки браузера", browser.local_state_exists ? "готовы" : "не найдены"],
    ["Авторизация", browserAuthStatus(browser).label],
    ["Проверка входа", browserAuthStatus(browser).details],
    ["Браузер worker", browser.browser_channel === "chrome" ? "Google Chrome (скрыто)" : "Chromium"],
    ["Ручной вход", browser.manual_login_pending ? "Требуется закрыть отдельный Chrome и проверить вход" : "не выполняется"],
    ["Пауза авторизации", browser.auth_backoff_left ? `${Math.ceil(browser.auth_backoff_left / 60)} мин` : "нет"],
    ["Резервные копии", backupSummary(browser.backup_count, browser.backup_bytes)],
    ["Копии авторизации", backupSummary(browser.auth_backup_count, browser.auth_backup_bytes)],
    ["Путь к профилю", browser.user_data_dir],
  ];
  const dl = $("#browser-kv");
  dl.innerHTML = "";
  for (const [key, value] of rows) {
    const dt = document.createElement("dt");
    const dd = document.createElement("dd");
    dt.textContent = key;
    dd.textContent = text(value);
    dl.append(dt, dd);
  }
  renderBrowserStorage(browser);
  renderBackupList([...(browser.latest_auth_backups || []), ...(browser.latest_backups || [])]);
  const pruneButton = $("#prune-backups-button");
  const totalBackups = Number(browser.backup_count || 0) + Number(browser.auth_backup_count || 0);
  pruneButton.disabled = totalBackups <= 1;
  pruneButton.textContent = totalBackups > 1
    ? "Удалить старые копии"
    : "Старых копий нет";
}

function renderBrowserStorage(browser) {
  const box = $("#browser-storage");
  const userData = Number(browser.size_bytes || 0);
  const backup = Number(browser.backup_bytes || 0);
  const total = Math.max(1, userData + backup);
  const rows = [
    ["Активный", userData, "active"],
    ["Резервные копии", backup, "backup"],
  ];
  box.innerHTML = "";
  for (const [label, size, kind] of rows) {
    const row = document.createElement("div");
    row.className = "storage-row";
    const name = document.createElement("div");
    name.textContent = label;
    const track = document.createElement("div");
    track.className = `storage-track ${kind}`;
    const fill = document.createElement("span");
    fill.style.width = `${Math.round((Number(size || 0) / total) * 100)}%`;
    const value = document.createElement("div");
    value.textContent = size ? formatBytes(size) : "нет";
    track.appendChild(fill);
    row.append(name, track, value);
    box.appendChild(row);
  }
}

function renderBackupList(backups) {
  const box = $("#backup-list");
  box.innerHTML = "";
  const deleteButton = $("#delete-selected-backups-button");
  const restoreButton = $("#restore-selected-backup-button");
  if (deleteButton) deleteButton.disabled = true;
  if (restoreButton) restoreButton.disabled = true;
  const data = backups.length ? backups : [{ name: "Резервных копий нет", size_bytes: 0, modified_at: null, disabled: true }];
  for (const backup of data.slice(0, 8)) {
    const item = document.createElement("div");
    item.className = `backup-item${backup.disabled ? " disabled" : ""}`;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "backup-check";
    checkbox.disabled = Boolean(backup.disabled);
    checkbox.dataset.backupKind = backup.kind || "";
    checkbox.dataset.backupName = backup.name || "";
    checkbox.dataset.backupValid = backup.valid === false ? "0" : "1";
    const name = document.createElement("div");
    name.className = "backup-name";
    name.textContent = backup.kind === "auth" ? `Авторизация · ${backup.name}` : backup.name;
    const meta = document.createElement("div");
    meta.className = "backup-meta";
    const status = backup.kind === "auth"
      ? (backup.valid === false ? "не прошла проверку" : `файлов: ${Number(backup.entry_count || 0)}`)
      : "снимок профиля";
    meta.textContent = `${formatBytes(backup.size_bytes)} · ${formatDate(backup.modified_at)} · ${status}`;
    const body = document.createElement("div");
    body.append(name, meta);
    item.append(checkbox, body);
    box.appendChild(item);
  }
}

function selectedBackupItems() {
  return $$(".backup-check:checked").map((item) => ({
    kind: item.dataset.backupKind || "",
    name: item.dataset.backupName || "",
    valid: item.dataset.backupValid !== "0",
  })).filter((item) => item.kind && item.name);
}

function updateSelectedBackupButton() {
  const button = $("#delete-selected-backups-button");
  const restoreButton = $("#restore-selected-backup-button");
  const selected = selectedBackupItems();
  if (button) {
    button.disabled = selected.length === 0;
    button.textContent = selected.length ? `Удалить выбранные копии · ${selected.length}` : "Удалить выбранные копии";
  }
  if (restoreButton) {
    const authSelected = selected.filter((item) => item.kind === "auth");
    const canRestore = authSelected.length === 1 && authSelected[0].valid !== false;
    restoreButton.disabled = !canRestore;
    restoreButton.textContent = authSelected.length === 1 && authSelected[0].valid === false
      ? "Копия не прошла проверку"
      : authSelected.length === 1
        ? "Восстановить выбранную копию"
        : "Выберите одну копию авторизации";
  }
}

function renderSelectors(diag) {
  const chromeSelect = $("#chrome-profile");
  const botSelect = $("#bot-profile");
  const chromeProfiles = (diag.chrome_profiles && diag.chrome_profiles.items) || [];
  const botProfiles = (diag.profiles && diag.profiles.items) || [];
  const oldChrome = chromeSelect.value;
  const oldBot = botSelect.value;

  chromeSelect.innerHTML = chromeProfiles.map((profile) => (
    `<option value="${escapeHtml(profile.id)}">${escapeHtml(profile.label || profile.id)}</option>`
  )).join("");
  botSelect.innerHTML = botProfiles.map((profile) => (
    `<option value="${escapeHtml(profile.key)}">${escapeHtml(profile.label || profile.key)}</option>`
  )).join("");
  if (oldChrome) chromeSelect.value = oldChrome;
  const activeProfile = (diag.state && diag.state.active_profile) || "";
  const selectedProfile = oldBot || activeProfile;
  if (selectedProfile) botSelect.value = selectedProfile;
}

async function loadSelectedBrowserProfile() {
  const profileKey = $("#bot-profile").value || "";
  const browser = await api(`/api/browser-profile?profile_key=${encodeURIComponent(profileKey)}`);
  renderBrowser(browser || {});
}

function renderSchedule(schedule) {
  $("#schedule-enabled").checked = Boolean(schedule.installed);
  $("#schedule-12h").checked = Boolean(schedule.every_12_hours);
  const scheduleStale = scheduleIsStale(schedule);
  const mode = schedule.installed
    ? (scheduleStale ? "просрочено" : (schedule.at_startup ? "старт Windows" : "ручной режим"))
    : "выключен";
  const repeat = schedule.every_12_hours ? "повтор 12 ч" : "без повтора";
  const modeNode = $("#schedule-mode");
  if (modeNode) {
    modeNode.textContent = mode;
    modeNode.className = `badge ${scheduleKind(schedule)}`;
  }
  $("#schedule-meta").textContent = [
    `Следующий: ${scheduleStale ? "просрочен" : (schedule.next_run_time || "—")}`,
    `результат: ${text(schedule.last_task_result)}`,
    mode,
    repeat,
  ].join(" · ");
}

function renderLogs(diag) {
  if (state.activeView === "logs") return;
  const lines = [
    "Worker stdout",
    ...((diag.recent_worker_stdout || []).slice(-28).reverse()),
    "",
    "Launcher",
    ...((diag.recent_launcher_log || []).slice(-12).reverse()),
  ];
  $("#logs").textContent = lines.join("\n");
}

function renderLogsPayload(data) {
  state.currentLogPath = data.path || "";
  const source = $("#log-source");
  if (source && !source.options.length) {
    source.innerHTML = (data.options || []).map((item) => (
      `<option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>`
    )).join("");
  }
  if (source) source.value = data.name || "worker_stdout";
  $("#log-filter").value = data.filter || "all";
  $("#log-lines").value = data.line_limit || 160;
  $("#logs-meta").textContent = `${data.label || data.name} · новые записи сверху · показано ${data.shown}/${data.total} · ${data.path || ""}`;

  const header = [
    `===== ${data.label || data.name} =====`,
    `Фильтр: ${data.filter || "all"} · поиск: ${data.search || "—"} · строк: ${data.shown}/${data.total}`,
    "",
  ];
  const lines = [...(data.lines || [])].reverse().map((item) => (
    `<span class="log-line ${escapeHtml(item.kind || "")}">${escapeHtml(item.text || "")}</span>`
  )).join("");
  $("#logs").innerHTML = `${escapeHtml(header.join("\n"))}${lines || '<span class="log-line muted">(пусто)</span>'}`;
}

async function loadLogs() {
  const source = $("#log-source");
  const filter = $("#log-filter");
  const search = $("#log-search");
  const lines = $("#log-lines");
  if (!source || !filter || !search || !lines) return;
  const params = new URLSearchParams({
    name: source.value || "worker_stdout",
    filter: filter.value || "all",
    search: search.value || "",
    lines: lines.value || "160",
  });
  const data = await api(`/api/logs?${params.toString()}`);
  renderLogsPayload(data);
}

function formatDate(timestamp) {
  if (!timestamp) return "--";
  try {
    return new Date(Number(timestamp) * 1000).toLocaleString();
  } catch {
    return "--";
  }
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function refresh(options = {}) {
  if (state.refreshInFlight) {
    if (!options.silent || options.full) {
      state.refreshQueued = true;
      state.refreshQueuedFull = state.refreshQueuedFull || Boolean(options.full);
    }
    return;
  }
  state.refreshInFlight = true;
  const status = $("#refresh-state");
  const useLive = !options.full;
  if (status && !options.silent) {
    status.textContent = "обновление";
    status.classList.add("active");
  }
  try {
    const diag = await api(useLive ? "/api/live" : "/api/diagnostics?fresh=1");
    state.lastRefreshAt = Date.now();
    document.documentElement.dataset.connection = "online";
    status?.classList.remove("error");
    if (useLive) {
      state.lastLiveAt = Date.now();
      const authKeys = ["profile_key", "exists", "local_state_exists", "preferences_exists", "cookies_exists", "needs_recovery", "session_import_required", "session_validation"];
      const oldBrowser = state.diagnostics?.browser_profile || {};
      const newBrowser = diag.browser_profile || {};
      if (state.fullDiagnosticsLoaded && authKeys.some((key) => JSON.stringify(oldBrowser[key]) !== JSON.stringify(newBrowser[key]))) {
        state.refreshQueued = true;
        state.refreshQueuedFull = true;
      }
      const merged = {
        ...(state.diagnostics || {}),
        ...diag,
        profiles: { ...(state.diagnostics?.profiles || {}), ...(diag.profiles || {}) },
        browser_profile: { ...(state.diagnostics?.browser_profile || {}), ...(diag.browser_profile || {}) },
      };
      if (!state.fullDiagnosticsLoaded) {
        merged.health = pendingHealth();
        state.refreshQueued = true;
        state.refreshQueuedFull = true;
      }
      state.diagnostics = merged;
      renderLiveStatus(merged, { full: true });
      renderOverviewSummary(merged);
    } else {
      renderDiagnostics(diag);
    }
    if (state.activeView === "logs") {
      await loadLogs();
    }
    if (status) {
      status.textContent = `${useLive ? "актуально" : "обновлено"} ${new Date().toLocaleTimeString()}`;
      status.classList.remove("active");
      if (!useLive) flashChange(status);
    }
  } catch (error) {
    document.documentElement.dataset.connection = "offline";
    if (!options.silent) toast(error.message);
    if (status) {
      const ageSeconds = state.lastRefreshAt ? Math.max(0, Math.round((Date.now() - state.lastRefreshAt) / 1000)) : 0;
      status.textContent = ageSeconds ? `нет связи · данные ${formatDuration(ageSeconds)} назад` : "нет связи с backend";
      status.classList.remove("active");
      status.classList.add("error");
    }
    throw error;
  } finally {
    state.refreshInFlight = false;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      const full = state.refreshQueuedFull;
      state.refreshQueuedFull = false;
      refresh({ full, silent: !full }).catch((error) => toast(error.message));
    }
  }
}

async function loadFullDiagnostics(options = {}) {
  if (state.fullDiagnosticsLoaded && !options.force) return;
  await refresh({ full: true, silent: Boolean(options.silent) });
}

function confirmAction({ title, message, confirmLabel = "Продолжить", danger = true }) {
  const dialog = $("#confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(`${title}\n\n${message}`));
  }
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  const accept = $("#confirm-accept");
  accept.textContent = confirmLabel;
  accept.className = `btn ${danger ? "danger" : "primary"}`;
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
    dialog.showModal();
    window.setTimeout(() => accept.focus(), 0);
  });
}

const ACTION_CONFIRMATIONS = {
  stop_all: {
    title: "Остановить всё?",
    message: "Текущий запуск будет прерван. Новые действия не начнутся, пока вы не запустите worker снова.",
    confirmLabel: "Остановить",
  },
  restart_all: {
    title: "Перезапустить всё?",
    message: "Текущий запуск будет прерван, после чего сервисы запустятся заново.",
    confirmLabel: "Перезапустить",
    danger: false,
  },
  stop_worker: {
    title: "Остановить worker?",
    message: "Текущая обработка будет прервана. Настройки и данные профилей сохранятся.",
    confirmLabel: "Остановить",
  },
  stop_telegram: {
    title: "Остановить Telegram?",
    message: "Команды из Telegram временно станут недоступны, пока сервис не будет запущен снова.",
    confirmLabel: "Остановить",
  },
  reset_runtime_flags: {
    title: "Сбросить служебные флаги?",
    message: "Будут сняты запросы на паузу и остановку. Настройки и данные профилей не изменятся.",
    confirmLabel: "Сбросить",
  },
};

async function runAction(action) {
  const confirmation = ACTION_CONFIRMATIONS[action];
  if (confirmation && !(await confirmAction(confirmation))) return false;
  await api("/api/action", {
    method: "POST",
    body: JSON.stringify({ action }),
  });
  toast("Действие выполнено");
  await refresh();
  return true;
}

function renderMaintenanceResult(title, result = {}) {
  const box = $("#maintenance-result");
  if (!box) return;
  const actions = Array.isArray(result.actions) ? result.actions : [];
  const lines = [
    title,
    `Статус: ${result.ok === false ? "требует внимания" : "готово"}`,
    `Профиль: ${result.profile_key || "--"}`,
    `Освобождено: ${formatBytes(result.freed_bytes || 0)}`,
    "",
    ...actions.map((item) => {
      const suffix = item.ok === false ? `ошибка: ${item.error || "неизвестно"}` : `освобождено: ${formatBytes(item.freed_bytes || 0)}`;
      return `${item.name || "действие"}: ${item.ok === false ? "пропущено" : "готово"} · ${suffix}`;
    }),
  ];
  if (!actions.length && result.validation) {
    lines.push(`Главный скрипт: ${result.main_script_exists ? "найден" : "не найден"}`);
    lines.push(`Предварительная проверка: ${result.preflight && result.preflight.ok ? "пройдена" : "нужна проверка"}`);
    lines.push(`Worker: ${result.worker_running ? `запущен, PID ${result.worker_pid}` : "остановлен"}`);
    lines.push(`Код завершения: ${result.exit_code ?? "--"}`);
  }
  if (result.name) lines.push(`Файл: ${result.name}`);
  if (result.profile_key) lines.push(`Профиль: ${result.profile_key}`);
  if (result.restored_count !== undefined) lines.push(`Восстановлено: ${result.restored_count}`);
  if (result.skipped_count !== undefined) lines.push(`Пропущено: ${result.skipped_count}`);
  if (result.size_bytes !== undefined) lines.push(`Размер: ${formatBytes(result.size_bytes)}`);
  if (result.included_count !== undefined) lines.push(`Файлов: ${result.included_count}`);
  if (result.pre_restore_backup && result.pre_restore_backup.name) lines.push(`Копия до восстановления: ${result.pre_restore_backup.name}`);
  if (result.error_count) lines.push(`Предупреждений: ${result.error_count}`);
  if (result.path) lines.push(`Путь: ${result.path}`);
  if (result.error) lines.push(`Ошибка: ${result.error}`);
  if (Array.isArray(result.lines) && result.lines.length) {
    lines.push("", ...result.lines.slice(0, 10));
  }
  if (Array.isArray(result.error_lines) && result.error_lines.length) {
    lines.push("", ...result.error_lines.slice(0, 10));
  }
  if (result.exit_code !== undefined) lines.push(`Код завершения: ${result.exit_code ?? "--"}`);
  if (result.duration_ms !== undefined) lines.push(`Длительность: ${result.duration_ms} мс`);
  if (result.browser_probe) lines.push(`Проверка браузера: ${result.browser_probe.enabled ? "включена" : "выключена"}`);
  box.textContent = lines.join("\n").trim();
  flashChange(box.closest(".panel") || box);
}

function parentPath(path) {
  const normalized = String(path || "").replaceAll("\\", "/");
  const index = normalized.lastIndexOf("/");
  return index > 0 ? normalized.slice(0, index) : normalized;
}

async function runWorkerSelfTestFromButton(button) {
  if (!button) return;
  button.disabled = true;
  try {
    const result = await api("/api/worker-self-test", {
      method: "POST",
      body: JSON.stringify({ no_browser: true }),
    });
    renderMaintenanceResult("Проверка worker", result);
    toast(result.ok ? "Self-test пройден" : "Self-test требует внимания");
    await refresh();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

function activateView(view) {
  if (state.uiMode === "simple" && isAdvancedView(view)) {
    view = "overview";
  }
  state.activeView = view;
  const navButton = $(`.nav-item[data-view="${view}"]`);
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item === navButton));
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${view}`));
  $("#page-title").textContent = navButton ? navButton.textContent.trim() : "TikTok Heart";
  if (["browser", "profiles", "services", "diagnostics"].includes(view)) {
    loadFullDiagnostics().catch((error) => toast(error.message));
  }
  if (view === "messages") {
    loadMessages().catch((error) => toast(error.message));
  }
  if (view === "logs") {
    loadLogs().catch((error) => toast(error.message));
  }
}

function bindNavigation() {
  $$(".nav-item").forEach((button) => {
    button.addEventListener("click", () => activateView(button.dataset.view));
  });
}

function bindActions() {
  $("#create-profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    const status = $("#profile-create-status");
    button.disabled = true;
    try {
      const result = await api("/api/profile-action", { method: "POST", body: JSON.stringify({ action: "create", profile_key: $("#new-profile-key").value.trim() }) });
      state.selectedProfileKey = result.key;
      form.reset();
      await refresh({ full: true });
      status.textContent = `Профиль ${result.key} создан. Добавьте адресата справа, затем перенесите вход.`;
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });

  $("#add-target-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    const status = $("#target-create-status");
    const profileKey = state.selectedProfileKey;
    if (!profileKey) { status.textContent = "Сначала создайте или откройте профиль слева."; return; }
    button.disabled = true;
    try {
      await api("/api/target-action", { method: "POST", body: JSON.stringify({ action: "add", profile_key: profileKey, target_name: $("#new-target-name").value.trim(), profile_url: $("#new-target-url").value.trim() }) });
      form.reset();
      await refresh({ full: true });
      status.textContent = `Адресат добавлен в профиль ${profileKey}.`;
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });

  $("#profile-import-shortcut").addEventListener("click", () => {
    if (!state.selectedProfileKey) { toast("Сначала выберите профиль"); return; }
    $("#bot-profile").value = state.selectedProfileKey;
    activateView("browser");
    loadSelectedBrowserProfile().catch((error) => toast(error.message));
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-action]");
    if (!button) return;
    button.disabled = true;
    try {
      await runAction(button.dataset.action);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-profile-select]");
    if (!button) return;
    state.selectedProfileKey = button.dataset.profileSelect;
    renderProfiles(state.diagnostics || {});
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-go-view]");
    if (!button) return;
    activateView(button.dataset.goView);
  });

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-open-log-source]");
    if (!button) return;
    activateView("logs");
    const source = $("#log-source");
    if (source) {
      source.value = button.dataset.openLogSource || "app_shell_perf";
    }
    loadLogs().catch((error) => toast(error.message));
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-open-kind], [data-open-path], [data-open-current-log]");
    if (!button) return;
    button.disabled = true;
    try {
      let payload = {};
      if (button.hasAttribute("data-open-current-log")) {
        payload = { path: state.currentLogPath };
      } else if (button.dataset.openKind) {
        payload = { kind: button.dataset.openKind };
      } else {
        let targetPath = button.dataset.openPath || "";
        if (button.dataset.openParent === "1") {
          const normalized = targetPath.replaceAll("\\", "/");
          targetPath = normalized.slice(0, normalized.lastIndexOf("/")) || targetPath;
        }
        payload = { path: targetPath };
      }
      const result = await api("/api/open-path", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      toast(`Открыто: ${result.path}`);
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy-path], [data-copy-text]");
    if (!button) return;
    const value = button.dataset.copyText ?? button.dataset.copyPath ?? "";
    const label = button.dataset.copyLabel || (button.dataset.copyText !== undefined ? "Ссылка" : "Путь");
    const success = button.dataset.copyText !== undefined ? `${label} скопирована` : `${label} скопирован`;
    try {
      await navigator.clipboard.writeText(value);
      toast(success);
    } catch {
      toast(value || `${label} недоступна`);
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-export-diagnostics]");
    if (!button) return;
    button.disabled = true;
    try {
      const result = await api("/api/export-diagnostics", {
        method: "POST",
        body: JSON.stringify({ format: button.dataset.exportDiagnostics }),
      });
      toast(`Диагностика экспортирована: ${result.name}`);
      await api("/api/open-path", {
        method: "POST",
        body: JSON.stringify({ path: result.path }),
      });
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-profile-action]");
    if (!button) return;
    button.disabled = true;
    try {
      const result = await api("/api/profile-action", {
        method: "POST",
        body: JSON.stringify({
          action: button.dataset.profileAction,
          profile_key: button.dataset.profileKey,
          index: Number(button.dataset.index || 0),
        }),
      });
      if (result && result.key) {
        state.selectedProfileKey = result.key;
      }
      toast(button.dataset.profileAction === "set_active" ? "Активный профиль изменён" : "Профиль обновлён");
      await refresh({ full: true });
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-target-action]");
    if (!button) return;
    button.disabled = true;
    try {
      let value = 0;
      if (button.dataset.unconfirmed === "true") {
        const confirmed = await confirmAction({
          title: "Разрешить новую попытку?",
          message: "Предыдущий результат неизвестен. Проверьте чат TikTok: повторная попытка может создать дубликат. Вы проверили, что сообщение не отправлено?",
          confirmLabel: "Проверил, разрешить",
        });
        if (!confirmed) return;
      }
      if (button.dataset.targetAction === "set_streak") {
        const input = button.closest(".target-item")?.querySelector("[data-streak-input]");
        value = Math.max(0, Number(input ? input.value : 0) || 0);
      }
      await api("/api/target-action", {
        method: "POST",
        body: JSON.stringify({
          action: button.dataset.targetAction,
          profile_key: button.dataset.profileKey,
          target_name: button.dataset.targetName,
          value,
        }),
      });
      toast(button.dataset.targetAction === "set_streak" ? "Серия сохранена" : "Cooldown сброшен");
      await refresh({ full: true });
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#refresh-button").addEventListener("click", () => {
    const full = state.fullDiagnosticsLoaded || isAdvancedView(state.activeView);
    refresh({ full }).catch((error) => toast(error.message));
  });
  $("#bot-profile").addEventListener("change", () => {
    loadSelectedBrowserProfile().catch((error) => toast(error.message));
  });
  $("#backup-list").addEventListener("change", (event) => {
    if (event.target.closest(".backup-check")) updateSelectedBackupButton();
  });
  $("#refresh-toggle").addEventListener("click", () => {
    state.autoRefresh = !state.autoRefresh;
    $("#refresh-toggle").classList.toggle("on", state.autoRefresh);
    $("#refresh-toggle").textContent = state.autoRefresh ? "Включено" : "Выключено";
  });
  $("#theme-toggle").addEventListener("click", () => {
    applyTheme(state.theme === "dark" ? "light" : "dark");
  });
  $("#mode-toggle").addEventListener("click", () => {
    applyUiMode(state.uiMode === "simple" ? "advanced" : "simple");
  });
  $("#overview-details").addEventListener("toggle", (event) => {
    if (event.currentTarget.open) {
      loadFullDiagnostics().catch((error) => toast(error.message));
    }
  });

  const editor = $("#message-editor");
  if (editor) {
    editor.addEventListener("input", () => {
      state.messageDirty = true;
      $("#message-editor-state").textContent = "Есть несохранённые изменения";
      renderMessagesStats(messageStatsFromText(editor.value));
    });
  }

  $("#message-save").addEventListener("click", async () => {
    const button = $("#message-save");
    button.disabled = true;
    try {
      const result = await api("/api/message-pool", {
        method: "POST",
        body: JSON.stringify({
          text: $("#message-editor").value,
          backup: true,
          normalize: false,
        }),
      });
      state.messageLoaded = true;
      state.messageDirty = false;
      $("#message-editor-state").textContent = "Сохранено";
      renderMessagesStats(result || {});
      toast("Сообщения сохранены");
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#message-normalize").addEventListener("click", async () => {
    const button = $("#message-normalize");
    button.disabled = true;
    try {
      await api("/api/message-pool", {
        method: "POST",
        body: JSON.stringify({
          text: $("#message-editor").value,
          backup: true,
          normalize: true,
        }),
      });
      state.messageDirty = false;
      await loadMessages(true);
      toast("Пул нормализован");
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#message-backup").addEventListener("click", async () => {
    const button = $("#message-backup");
    button.disabled = true;
    try {
      await api("/api/message-pool/backup", {
        method: "POST",
        body: JSON.stringify({ text: $("#message-editor").value }),
      });
      toast("Копия списка сообщений создана");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#logs-refresh").addEventListener("click", () => loadLogs().catch((error) => toast(error.message)));
  $("#log-source").addEventListener("change", () => loadLogs().catch((error) => toast(error.message)));
  $("#log-filter").addEventListener("change", () => loadLogs().catch((error) => toast(error.message)));
  $("#log-lines").addEventListener("change", () => loadLogs().catch((error) => toast(error.message)));
  $("#log-search").addEventListener("input", () => {
    clearTimeout(state.logSearchTimer);
    state.logSearchTimer = setTimeout(() => loadLogs().catch((error) => toast(error.message)), 220);
  });

  $("#schedule-save").addEventListener("click", async () => {
    const button = $("#schedule-save");
    if (state.scheduleSaveInFlight) return;
    state.scheduleSaveInFlight = true;
    button.disabled = true;
    const oldText = button.textContent;
    button.textContent = "Сохраняю";
    try {
      await api("/api/schedule", {
        method: "POST",
        body: JSON.stringify({
          enabled: $("#schedule-enabled").checked,
          at_startup: true,
          at_logon: false,
          every_12_hours: $("#schedule-12h").checked,
        }),
      });
      toast("Автозапуск сохранён");
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      state.scheduleSaveInFlight = false;
      button.disabled = false;
      button.textContent = oldText;
    }
  });

  $("#worker-self-test-button").addEventListener("click", async () => {
    await runWorkerSelfTestFromButton($("#worker-self-test-button"));
  });
  $("#overview-worker-self-test-button").addEventListener("click", async () => {
    await runWorkerSelfTestFromButton($("#overview-worker-self-test-button"));
  });

  $("#auth-backup-button").addEventListener("click", async () => {
    const button = $("#auth-backup-button");
    button.disabled = true;
    try {
      const result = await api("/api/auth-backup", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
        }),
      });
      renderMaintenanceResult("Резервная копия авторизации", result);
      const status = $("#auth-backup-result");
      if (status) {
        status.textContent = `${result.name || "резервная копия"} · ${formatBytes(result.size_bytes || 0)} · файлов: ${result.included_count || 0}`;
        flashChange(status);
      }
      toast(`Копия авторизации создана · ${formatBytes(result.size_bytes || 0)}`);
      if (result.path) {
        await api("/api/open-path", {
          method: "POST",
          body: JSON.stringify({ path: parentPath(result.path) }),
        });
      }
      await loadSelectedBrowserProfile();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#project-backup-button").addEventListener("click", async () => {
    const button = $("#project-backup-button");
    button.disabled = true;
    try {
      const result = await api("/api/project-backup", {
        method: "POST",
        body: JSON.stringify({}),
      });
      renderMaintenanceResult("Резервная копия исходников", result);
      toast(`Копия исходников создана · ${formatBytes(result.size_bytes || 0)}`);
      if (result.path) {
        await api("/api/open-path", {
          method: "POST",
          body: JSON.stringify({ path: parentPath(result.path) }),
        });
      }
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#maintenance-button").addEventListener("click", async () => {
    const button = $("#maintenance-button");
    const confirmed = await confirmAction({
      title: "Очистить кэш и старые копии?",
      message: "Будут удалены ненужные временные данные. Последние резервные копии сохранятся.",
      confirmLabel: "Очистить",
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      const result = await api("/api/maintenance", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
        }),
      });
      renderMaintenanceResult("Обслуживание", result);
      toast(`Обслуживание завершено · ${formatBytes(result.freed_bytes || 0)}`);
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  for (const action of ["open", "verify", "cancel"]) {
    $(`#dedicated-login-${action}`).addEventListener("click", async () => {
      const profileKey = $("#bot-profile").value;
      const buttons = [$("#dedicated-login-open"), $("#dedicated-login-verify"), $("#dedicated-login-cancel")];
      const status = $("#dedicated-login-result");
      buttons.forEach((button) => { button.disabled = true; });
      status.textContent = action === "open"
        ? "Открываем отдельный Chrome для ручного входа…"
        : action === "verify" ? "Проверяем вход скрыто, без отправки сообщений…" : "Отменяем незавершённый вход…";
      try {
        const result = await api(`/api/dedicated-login/${action}`, {
          method: "POST", body: JSON.stringify({ bot_profile_key: profileKey }),
        });
        status.textContent = action === "open"
          ? `Окно входа профиля «${result.bot_profile_key}» ${result.already_open ? "уже открыто" : "открыто"}. Войдите в TikTok, закройте это окно Chrome и нажмите «Проверить вход». Worker до проверки приостановлен.`
          : action === "verify"
            ? `Вход профиля «${result.bot_profile_key}» подтверждён. Последующие запуски будут скрытыми. Worker автоматически не запускался.`
            : "Незавершённый вход отменён. Профили сохранены. Прежнее требование входа, если оно было, остаётся в силе.";
        await refresh();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        buttons.forEach((button) => { button.disabled = false; });
      }
    });
  }

  $("#session-import-button").addEventListener("click", async () => {
    const button = $("#session-import-button");
    const input = $("#session-request");
    const status = $("#session-import-result");
    if (!input.value.trim()) {
      status.textContent = "Сначала скопируйте запрос в Chrome по инструкции выше и вставьте его в поле.";
      input.focus();
      return;
    }
    button.disabled = true;
    status.textContent = "Проверяем вход в отдельном профиле браузера… Дождитесь результата.";
    try {
      const body = JSON.stringify({ request_text: input.value, bot_profile_key: $("#bot-profile").value });
      input.value = "";
      const result = await api("/api/import-chrome-request", { method: "POST", body });
      status.textContent = `Вход подтверждён и сохранён в профиле «${result.bot_profile_key}». Можно запускать worker.${result.backup ? " Предыдущая авторизация сохранена в резервной копии." : ""}`;
      toast("Авторизация TikTok сохранена");
      await refresh();
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  $("#import-button").addEventListener("click", async () => {
    const button = $("#import-button");
    const confirmed = await confirmAction({
      title: "Импортировать данные из Chrome?",
      message: "Данные выбранного профиля Chrome будут скопированы в профиль приложения. Текущая авторизация может измениться.",
      confirmLabel: "Импортировать",
      danger: false,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      const result = await api("/api/import-chrome", {
        method: "POST",
        body: JSON.stringify({
          chrome_profile_id: $("#chrome-profile").value,
          bot_profile_key: $("#bot-profile").value,
          copy_mode: $("#import-mode").value,
        }),
      });
      const cookies = result.cookies || {};
      const cookieText = cookies.copied ? ` · записей авторизации: ${cookies.kept}/${cookies.before}` : "";
      const modeText = result.copy_mode === "tiktok_session" ? "авторизация TikTok" : "полный профиль";
      const recoveryText = result.pre_import_auth_backup ? " · создана резервная копия" : "";
      const backoffText = result.auth_backoff_preserved ? " · пауза входа сохранена до подтверждения сессии" : "";
      toast(`Импортирована ${modeText} · ${formatBytes(result.size_bytes)}${cookieText}${recoveryText}${backoffText}`);
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#compact-browser-button").addEventListener("click", async () => {
    const button = $("#compact-browser-button");
    const confirmed = await confirmAction({
      title: "Очистить кэш профиля?",
      message: "Временные файлы браузера будут удалены. Авторизация и рабочие cookies сохранятся.",
      confirmLabel: "Очистить",
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      const result = await api("/api/compact-browser", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
          filter_cookies: true,
        }),
      });
      const cookies = (result.cookies || [])[0] || {};
      const cookieText = cookies.copied ? ` · cookies ${cookies.kept}/${cookies.before}` : "";
      toast(`Профиль очищен · освобождено ${formatBytes(result.freed_bytes)}${cookieText}`);
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#prune-backups-button").addEventListener("click", async () => {
    try {
      const browser = state.selectedBrowserProfile || (state.diagnostics && state.diagnostics.browser_profile) || {};
      const backupCount = Number(browser.backup_count || 0) + Number(browser.auth_backup_count || 0);
      if (backupCount <= 1) {
        toast("Старых копий нет");
        return;
      }
      const confirmed = await confirmAction({
        title: "Удалить старые резервные копии?",
        message: `Для профиля ${$("#bot-profile").value} будет оставлена только последняя копия.`,
        confirmLabel: "Удалить",
      });
      if (!confirmed) return;
      const result = await api("/api/prune-browser-backups", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
          keep_latest: 1,
        }),
      });
      toast(`Старые копии удалены · ${result.removed_count} · освобождено ${formatBytes(result.freed_bytes)}`);
      await loadSelectedBrowserProfile();
    } catch (error) {
      toast(error.message);
    }
  });

  $("#restore-selected-backup-button").addEventListener("click", async () => {
    const button = $("#restore-selected-backup-button");
    const items = selectedBackupItems().filter((item) => item.kind === "auth");
    if (items.length !== 1) {
      toast("Выберите одну копию авторизации для восстановления");
      return;
    }
    if (items[0].valid === false) {
      toast("Эта копия не прошла проверку");
      return;
    }
    const confirmed = await confirmAction({
      title: "Восстановить авторизацию?",
      message: `Текущая авторизация профиля ${$("#bot-profile").value} будет сохранена в отдельной резервной копии перед восстановлением.`,
      confirmLabel: "Восстановить",
      danger: false,
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      const result = await api("/api/restore-auth-backup", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
          name: items[0].name,
          include_global: false,
        }),
      });
      renderMaintenanceResult("Восстановление авторизации", result);
      toast(`Авторизация восстановлена · файлов: ${result.restored_count || 0}`);
      await loadSelectedBrowserProfile();
      await refresh();
    } catch (error) {
      toast(error.message);
      updateSelectedBackupButton();
    }
  });

  $("#delete-selected-backups-button").addEventListener("click", async () => {
    const button = $("#delete-selected-backups-button");
    const items = selectedBackupItems();
    if (!items.length) {
      toast("Выберите копию для удаления");
      return;
    }
    const confirmed = await confirmAction({
      title: "Удалить выбранные резервные копии?",
      message: `Профиль: ${$("#bot-profile").value}. Будет удалено копий: ${items.length}.`,
      confirmLabel: "Удалить",
    });
    if (!confirmed) return;
    button.disabled = true;
    try {
      const result = await api("/api/delete-backups", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
          items,
        }),
      });
      toast(`Удалено ${result.removed_count} · освобождено ${formatBytes(result.freed_bytes)}`);
      await loadSelectedBrowserProfile();
    } catch (error) {
      toast(error.message);
      updateSelectedBackupButton();
    }
  });
}

function startAutoRefresh() {
  state.refreshTimer = setInterval(() => {
    if (!state.autoRefresh || document.hidden) return;
    if (Date.now() - state.lastRefreshAt < state.autoRefreshIntervalMs - 1000) return;
    refresh({ silent: true }).catch((error) => toast(error.message));
  }, state.autoRefreshIntervalMs);
}

function startQueueClock() {
  window.setInterval(() => {
    if (document.hidden) return;
    const queue = state.activeQueue || {};
    if (!queue.target_count) return;
    const nextAt = Number(queue.next_send_at || 0);
    const left = nextAt ? Math.max(0, Math.round(nextAt - Date.now() / 1000)) : Number(queue.next_send_in_s || 0);
    const value = Number(queue.ready_count || 0) > 0 ? `${queue.ready_count} готово` : formatDuration(left);
    const meta = Number(queue.cooldown_count || 0) > 0
      ? `${queue.cooldown_count} cooldown · ${queue.next_target || "ожидание"}`
      : `${queue.target_count} адресатов · можно отправлять`;
    setPlainText("#queue-value", value);
    setPlainText("#queue-meta", meta);
  }, 1000);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.autoRefresh) {
    refresh({ silent: true }).catch((error) => toast(error.message));
  }
});

function bindShortcuts() {
  document.addEventListener("keydown", async (event) => {
    if (event.key === "F5") {
      event.preventDefault();
      refresh({ full: state.fullDiagnosticsLoaded || isAdvancedView(state.activeView) }).catch((error) => toast(error.message));
      return;
    }
    if (event.ctrlKey && !event.shiftKey && /^[1-8]$/.test(event.key)) {
      event.preventDefault();
      const item = $$(".nav-item")[Number(event.key) - 1];
      if (item) activateView(item.dataset.view);
      return;
    }
    if (event.ctrlKey && event.key.toLowerCase() === "l") {
      event.preventDefault();
      activateView("logs");
      $("#log-search").focus();
      return;
    }
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "c") {
      event.preventDefault();
      const active = $(".view.active");
      try {
        await navigator.clipboard.writeText(active ? active.innerText : "");
        toast("Раздел скопирован");
      } catch {
        toast("Не удалось скопировать раздел");
      }
      return;
    }
    if (event.ctrlKey && event.shiftKey && event.key.toLowerCase() === "r") {
      event.preventDefault();
      runAction("restart_all").catch((error) => toast(error.message));
      return;
    }
    if (event.ctrlKey && !event.shiftKey && event.key.toLowerCase() === "r") {
      event.preventDefault();
      runAction("start_all").catch((error) => toast(error.message));
      return;
    }
    if (event.ctrlKey && event.key === ".") {
      event.preventDefault();
      runAction("stop_all").catch((error) => toast(error.message));
    }
  });
}

applyTheme(state.theme);
applyUiMode(state.uiMode);
bindNavigation();
bindActions();
bindShortcuts();
refresh().catch((error) => toast(error.message));
startAutoRefresh();
startQueueClock();
