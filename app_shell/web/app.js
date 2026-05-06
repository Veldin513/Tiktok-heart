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
  activeView: "overview",
  selectedProfileKey: null,
  messageLoaded: false,
  messageDirty: false,
  currentLogPath: "",
  logSearchTimer: null,
  selectedBrowserProfile: null,
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
  const requestPath = method === "GET"
    ? `${path}${path.includes("?") ? "&" : "?"}_=${Date.now()}`
    : path;
  const response = await fetch(requestPath, {
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    ...options,
  });
  const payload = await response.json();
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload.data;
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

function setPill(id, label, kind) {
  const node = $(id);
  const previousText = node.textContent;
  const previousClass = node.className;
  node.textContent = label;
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

function renderDiagnostics(diag) {
  state.diagnostics = diag;
  const worker = diag.worker || {};
  const telegram = diag.telegram_bot || {};
  const browser = diag.browser_profile || {};
  const schedule = diag.worker_schedule || {};
  const health = diag.health || {};
  const appState = diag.state || {};
  const messages = diag.message_pool_details || {};
  const profiles = diag.profiles || {};
  const run = diag.run || {};

  renderHealth(health);

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
  setPill("#schedule-pill", schedule.installed ? `Автозапуск · ${schedule.next_run_time || "готов"}` : "Автозапуск выключен", schedule.installed ? "ok" : "warn");

  $("#dry-run-button").textContent = appState.dry_run ? "DRY RUN" : "LIVE";
  $("#dry-run-button").classList.toggle("danger", Boolean(appState.dry_run));

  setText("#worker-value", worker.running ? "Запущен" : "Стоп");
  setText("#worker-meta", `PID: ${text(worker.pid)} · старт: ${formatDate(worker.started_at)}`);
  setText("#telegram-value", telegram.running ? "Запущен" : "Стоп");
  setText("#telegram-meta", `PID: ${text(telegram.pid)} · ready: ${diag.telegram_ready ? "да" : "нет"}`);
  setText("#profile-value", text(appState.active_profile));
  setText("#profile-meta", `${profiles.total || 0} профилей · ${browser.cookies_exists ? "cookies есть" : "cookies нет"}`);
  setText("#messages-value", text(messages.unique_count || messages.count || 0, "0"));
  setText("#messages-meta", `дублей: ${messages.duplicates || messages.duplicate_count || 0}`);
  setText("#schedule-value", schedule.installed ? (schedule.next_run_time || "включён") : "выключен");
  setText("#schedule-card-meta", `${schedule.at_logon ? "вход" : "без входа"} · ${schedule.every_12_hours ? "12 ч" : "без 12 ч"}`);
  setText("#browser-size-value", formatBytes(browser.size_bytes));
  setText("#browser-size-meta", `${browser.backup_count || 0} backup · ${browser.cookies_exists ? "cookies есть" : "cookies нет"}`);

  renderHealthSignals(health.signals || []);
  renderRecommendations(health);
  $("#run-state").textContent = [
    `Статус   : ${text(run.status, "idle")}`,
    `Профиль  : ${text(appState.active_profile)}`,
    `Адресат  : ${text(run.current_target)}`,
    `Всего    : ${text(run.total_targets, 0)}`,
    `Успешно  : ${text(run.success_count, 0)}`,
    `Пропуск  : ${text(run.skipped_count, 0)}`,
    `Ошибок   : ${text(run.failed_count, 0)}`,
  ].join("\n");

  renderSelectors(diag);
  renderBrowser(browser);
  const selectedBotProfile = $("#bot-profile")?.value || "";
  if (selectedBotProfile && browser.profile_key && selectedBotProfile !== browser.profile_key) {
    loadSelectedBrowserProfile().catch((error) => toast(error.message));
  }
  renderSchedule(schedule);
  renderLogs(diag);
  if (!state.messageDirty) {
    renderMessagesStats(messages);
  }
  renderProfiles(diag);
  renderDiagnosticsPanel(diag);
  renderOverviewSummary(diag);
  renderPerformanceSummary(diag.performance || {});
}

function renderHealth(health) {
  const score = Math.max(0, Math.min(100, Number(health.score || 0)));
  const kind = healthKind(health.status);
  const panel = $(".health-panel");
  panel.className = `health-panel ${kind}`;
  $("#health-score").textContent = text(score, "0");
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

function renderProfiles(diag) {
  const profiles = ((diag.profiles && diag.profiles.items) || []).filter(Boolean);
  const profilesBox = $("#profiles-list");
  const targetsBox = $("#targets-list");
  if (!profilesBox || !targetsBox) return;

  if (!profiles.length) {
    profilesBox.innerHTML = `<div class="list-item">Профили не найдены</div>`;
    targetsBox.innerHTML = `<div class="list-item">Нет адресатов</div>`;
    return;
  }

  const activeProfile = profiles.find((profile) => profile.active) || profiles[0];
  if (!state.selectedProfileKey || !profiles.some((profile) => profile.key === state.selectedProfileKey)) {
    state.selectedProfileKey = activeProfile.key;
  }
  const selectedProfile = profiles.find((profile) => profile.key === state.selectedProfileKey) || activeProfile;

  profilesBox.innerHTML = profiles.map((profile) => {
    const selected = profile.key === selectedProfile.key;
    const targetCount = Number(profile.target_count || (profile.targets || []).length || 0);
    const flags = [
      profile.active ? `<span class="badge ok">активен</span>` : "",
      profile.enabled ? `<span class="badge ok">включён</span>` : `<span class="badge warn">выключен</span>`,
    ].filter(Boolean).join(" ");
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
          <button class="btn secondary" data-profile-select="${escapeHtml(profile.key)}">Открыть</button>
          <button class="btn secondary" data-profile-action="set_active" data-index="${Number(profile.index || 0)}">Сделать активным</button>
          <button class="btn secondary" data-profile-action="toggle" data-index="${Number(profile.index || 0)}">${profile.enabled ? "Выключить" : "Включить"}</button>
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
    const ready = Boolean(targetState.ready);
    const cooldown = Number(targetState.cooldown_left_h || 0);
    const cooldownLeft = targetState.cooldown_left_text || `${cooldown.toFixed(1)} ч`;
    const nextSend = targetState.next_send_at_text || "";
    const cooldownText = ready ? "можно отправить сейчас" : `отправка через ${cooldownLeft}`;
    const nextSendText = ready ? "" : ` · ${escapeHtml(nextSend)}`;
    const badge = ready ? `<span class="badge ok">готов</span>` : `<span class="badge warn">cooldown</span>`;
    const streak = Number(targetState.streak_count || 0);
    const stateFiles = targetState.state_files || {};
    const fileCount = (stateFiles.last_send || []).length + (stateFiles.stats || []).length;
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
        <div class="target-actions">
          <button class="btn secondary" data-target-action="reset_cooldown" data-profile-key="${escapeHtml(selectedProfile.key)}" data-target-name="${escapeHtml(target.name || target.url || "")}">Сброс cooldown</button>
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
  const runProgress = $("#run-progress");
  const runHistory = $("#run-history");
  if (!summary || !report) return;

  const cards = [
    ["Здоровье", `${health.score || 0}/100`, health.status || "info"],
    ["Авторизация", browser.cookies_exists && !browser.needs_recovery ? "готова" : "проверить", browser.cookies_exists ? "ok" : "warn"],
    ["Автозапуск", schedule.installed ? "включён" : "выключен", schedule.installed ? "ok" : "warn"],
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
      ["Backup профиля", `${browser.backup_count || 0} · ${formatBytes(browser.backup_bytes)}`],
    ];
    timeline.innerHTML = items.map(([label, value]) => `
      <div class="timeline-item">
        <div class="timeline-label">${escapeHtml(label)}</div>
        <div class="timeline-value">${escapeHtml(value)}</div>
      </div>
    `).join("");
  }

  if (runProgress) {
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

  if (runHistory) {
    const items = Array.isArray(diag.run_history) ? diag.run_history.slice(-6).reverse() : [];
    const visible = items.length ? items : [{ event: "empty", time: "--", reason: "Истории прогонов пока нет" }];
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

  const triggers = [
    schedule.at_logon ? "вход в Windows" : "",
    schedule.every_12_hours ? "каждые 12 ч" : "",
  ].filter(Boolean).join(" + ") || "выключен";
  const recommendations = (health.recommendations || []).slice(0, 5);
  report.textContent = [
    `Здоровье           : ${health.score || 0}/100 · ${health.summary || "—"}`,
    `Активный профиль  : ${stateData.active_profile || "—"}`,
    `Chromium cookies   : ${browser.cookies_exists ? "да" : "нет"}`,
    `Browser size       : ${formatBytes(browser.size_bytes)}`,
    `Backups            : ${browser.backup_count || 0} · ${formatBytes(browser.backup_bytes)}`,
    `Сообщения          : ${messages.unique_count || messages.count || 0} уникальных`,
    `Автозапуск         : ${schedule.installed ? "включён" : "выключен"} · ${triggers}`,
    `Следующий запуск   : ${schedule.next_run_time || "—"}`,
    "",
    "Что сделать",
    ...(recommendations.length ? recommendations.map((item) => `- ${item}`) : ["- Критичных действий сейчас нет."]),
  ].join("\n");
}

function renderPerformanceSummary(performance = {}) {
  const box = $("#performance-summary");
  if (!box) return;
  const latest = performance.latest || {};
  const worst = performance.worst || {};
  const events = Array.isArray(performance.events) ? performance.events : [];
  const status = performance.status || "ok";
  const statusKind = status === "critical" ? "bad" : status === "warning" ? "warn" : "ok";
  const statusLabel = status === "critical" ? "критично" : status === "warning" ? "есть задержки" : "спокойно";
  const latestMs = latest.elapsed_ms !== undefined ? `${Math.round(Number(latest.elapsed_ms || 0))} ms` : "--";
  const worstMs = worst.elapsed_ms !== undefined ? `${Math.round(Number(worst.elapsed_ms || 0))} ms` : "--";
  const rows = [
    ["Статус", statusLabel, statusKind],
    ["Последний slow", latestMs, statusKind],
    ["Худший", worstMs, statusKind],
    ["Событий", String(performance.slow_count || 0), Number(performance.warning_count || 0) ? "warn" : "ok"],
  ];
  const visibleEvents = events.length
    ? events
    : [{ method: "", path: "Медленных запросов пока нет", elapsed_ms: 0, time: "" }];

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
  `;
}

function renderBrowser(browser) {
  state.selectedBrowserProfile = browser || {};
  const rows = [
    ["Профиль", browser.profile_key],
    ["user_data", browser.exists ? "есть" : "нет"],
    ["Размер", formatBytes(browser.size_bytes)],
    ["Default", browser.default_profile_exists ? "есть" : "нет"],
    ["Local State", browser.local_state_exists ? "есть" : "нет"],
    ["Cookies", browser.cookies_exists ? "есть" : "нет"],
    ["Auth backoff", browser.auth_backoff_left ? `${Math.ceil(browser.auth_backoff_left / 60)} мин` : "нет"],
    ["Backup", `${browser.backup_count || 0} · ${formatBytes(browser.backup_bytes)}`],
    ["Auth backup", `${browser.auth_backup_count || 0} · ${formatBytes(browser.auth_backup_bytes)}`],
    ["Путь", browser.user_data_dir],
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
    ? "Удалить старые backup"
    : "Старых backup нет";
}

function renderBrowserStorage(browser) {
  const box = $("#browser-storage");
  const userData = Number(browser.size_bytes || 0);
  const backup = Number(browser.backup_bytes || 0);
  const total = Math.max(1, userData + backup);
  const rows = [
    ["Активный", userData, "active"],
    ["Backup", backup, "backup"],
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
    value.textContent = formatBytes(size);
    track.appendChild(fill);
    row.append(name, track, value);
    box.appendChild(row);
  }
}

function renderBackupList(backups) {
  const box = $("#backup-list");
  box.innerHTML = "";
  const deleteButton = $("#delete-selected-backups-button");
  if (deleteButton) deleteButton.disabled = true;
  const data = backups.length ? backups : [{ name: "Backup нет", size_bytes: 0, modified_at: null, disabled: true }];
  for (const backup of data.slice(0, 5)) {
    const item = document.createElement("div");
    item.className = `backup-item${backup.disabled ? " disabled" : ""}`;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "backup-check";
    checkbox.disabled = Boolean(backup.disabled);
    checkbox.dataset.backupKind = backup.kind || "";
    checkbox.dataset.backupName = backup.name || "";
    const name = document.createElement("div");
    name.className = "backup-name";
    name.textContent = backup.kind === "auth" ? `auth · ${backup.name}` : backup.name;
    const meta = document.createElement("div");
    meta.className = "backup-meta";
    meta.textContent = `${formatBytes(backup.size_bytes)} · ${formatDate(backup.modified_at)}`;
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
  })).filter((item) => item.kind && item.name);
}

function updateSelectedBackupButton() {
  const button = $("#delete-selected-backups-button");
  if (!button) return;
  const selected = selectedBackupItems();
  button.disabled = selected.length === 0;
  button.textContent = selected.length ? `Удалить выбранные · ${selected.length}` : "Удалить выбранные";
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
  $("#schedule-logon").checked = Boolean(schedule.at_logon);
  $("#schedule-12h").checked = Boolean(schedule.every_12_hours);
  $("#schedule-meta").textContent = `Следующий запуск: ${schedule.next_run_time || "—"} · последний результат: ${text(schedule.last_task_result)}`;
}

function renderLogs(diag) {
  if (state.activeView === "logs") return;
  const lines = [
    "Worker stdout",
    ...((diag.recent_worker_stdout || []).slice(-28)),
    "",
    "Launcher",
    ...((diag.recent_launcher_log || []).slice(-12)),
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
  $("#logs-meta").textContent = `${data.label || data.name} · показано ${data.shown}/${data.total} · ${data.path || ""}`;

  const header = [
    `===== ${data.label || data.name} =====`,
    `Фильтр: ${data.filter || "all"} · поиск: ${data.search || "—"} · строк: ${data.shown}/${data.total}`,
    "",
  ];
  const lines = (data.lines || []).map((item) => (
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
    if (!options.silent) {
      state.refreshQueued = true;
    }
    return;
  }
  state.refreshInFlight = true;
  const status = $("#refresh-state");
  if (status) {
    status.textContent = "обновление";
    status.classList.add("active");
  }
  try {
    const diag = await api(options.silent ? "/api/diagnostics" : "/api/diagnostics?fresh=1");
    state.lastRefreshAt = Date.now();
    renderDiagnostics(diag);
    if (state.activeView === "logs") {
      await loadLogs();
    }
    if (status) {
      status.textContent = `обновлено ${new Date().toLocaleTimeString()}`;
      status.classList.remove("active");
      flashChange(status);
    }
  } catch (error) {
    if (!options.silent) toast(error.message);
    if (status) {
      status.textContent = `ошибка: ${error.message}`;
      status.classList.remove("active");
    }
    throw error;
  } finally {
    state.refreshInFlight = false;
    if (state.refreshQueued) {
      state.refreshQueued = false;
      refresh({ silent: true }).catch((error) => toast(error.message));
    }
  }
}

async function runAction(action) {
  await api("/api/action", {
    method: "POST",
    body: JSON.stringify({ action }),
  });
  toast("Готово");
  await refresh();
}

function renderMaintenanceResult(title, result = {}) {
  const box = $("#maintenance-result");
  if (!box) return;
  const actions = Array.isArray(result.actions) ? result.actions : [];
  const lines = [
    title,
    `status: ${result.ok === false ? "warning" : "ok"}`,
    `profile: ${result.profile_key || "--"}`,
    `freed: ${formatBytes(result.freed_bytes || 0)}`,
    "",
    ...actions.map((item) => {
      const suffix = item.ok === false ? `error=${item.error || "unknown"}` : `freed=${formatBytes(item.freed_bytes || 0)}`;
      return `${item.name || "action"}: ${item.ok === false ? "skipped" : "ok"} · ${suffix}`;
    }),
  ];
  if (!actions.length && result.validation) {
    lines.push(`main_script: ${result.main_script_exists ? "ok" : "missing"}`);
    lines.push(`preflight: ${result.preflight && result.preflight.ok ? "ok" : "check"}`);
    lines.push(`worker: ${result.worker_running ? `running PID ${result.worker_pid}` : "stopped"}`);
    lines.push(`exit_code: ${result.exit_code ?? "--"}`);
  }
  if (result.name) lines.push(`file: ${result.name}`);
  if (result.profile_key) lines.push(`profile: ${result.profile_key}`);
  if (result.size_bytes !== undefined) lines.push(`size: ${formatBytes(result.size_bytes)}`);
  if (result.included_count !== undefined) lines.push(`files: ${result.included_count}`);
  if (result.error_count) lines.push(`warnings: ${result.error_count}`);
  if (result.path) lines.push(`path: ${result.path}`);
  if (result.error) lines.push(`error: ${result.error}`);
  if (Array.isArray(result.lines) && result.lines.length) {
    lines.push("", ...result.lines.slice(0, 10));
  }
  if (Array.isArray(result.error_lines) && result.error_lines.length) {
    lines.push("", ...result.error_lines.slice(0, 10));
  }
  if (result.exit_code !== undefined) lines.push(`exit_code: ${result.exit_code ?? "--"}`);
  if (result.duration_ms !== undefined) lines.push(`duration: ${result.duration_ms} ms`);
  box.textContent = lines.join("\n").trim();
  flashChange(box.closest(".panel") || box);
}

function parentPath(path) {
  const normalized = String(path || "").replaceAll("\\", "/");
  const index = normalized.lastIndexOf("/");
  return index > 0 ? normalized.slice(0, index) : normalized;
}

function activateView(view) {
  state.activeView = view;
  const navButton = $(`.nav-item[data-view="${view}"]`);
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item === navButton));
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `view-${view}`));
  $("#page-title").textContent = navButton ? navButton.textContent.trim() : "TikTok Heart";
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
    const button = event.target.closest("[data-copy-path]");
    if (!button) return;
    try {
      await navigator.clipboard.writeText(button.dataset.copyPath || "");
      toast("Путь скопирован");
    } catch {
      toast(button.dataset.copyPath || "Путь недоступен");
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
          index: Number(button.dataset.index || 0),
        }),
      });
      if (result && result.key) {
        state.selectedProfileKey = result.key;
      }
      toast("Профиль обновлён");
      await refresh();
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
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#refresh-button").addEventListener("click", () => refresh().catch((error) => toast(error.message)));
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
      toast("Backup создан");
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
    try {
      await api("/api/schedule", {
        method: "POST",
        body: JSON.stringify({
          enabled: $("#schedule-enabled").checked,
          at_logon: $("#schedule-logon").checked,
          every_12_hours: $("#schedule-12h").checked,
        }),
      });
      toast("Автозапуск сохранён");
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  });

  $("#worker-self-test-button").addEventListener("click", async () => {
    const button = $("#worker-self-test-button");
    button.disabled = true;
    try {
      const result = await api("/api/worker-self-test", {
        method: "POST",
        body: JSON.stringify({}),
      });
      renderMaintenanceResult("Worker self-test", result);
      toast(result.ok ? "Self-test пройден" : "Self-test требует внимания");
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#security-scan-button").addEventListener("click", async () => {
    const button = $("#security-scan-button");
    button.disabled = true;
    try {
      const result = await api("/api/security-scan", {
        method: "POST",
        body: JSON.stringify({ tracked_only: true }),
      });
      renderMaintenanceResult("Security scan", result);
      toast(result.ok ? "Security scan чистый" : "Security scan требует внимания");
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
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
      renderMaintenanceResult("Backup авторизации", result);
      const status = $("#auth-backup-result");
      if (status) {
        status.textContent = `${result.name || "backup"} · ${formatBytes(result.size_bytes || 0)} · файлов: ${result.included_count || 0}`;
        flashChange(status);
      }
      toast(`Backup авторизации создан · ${formatBytes(result.size_bytes || 0)}`);
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
      renderMaintenanceResult("Backup исходников", result);
      toast(`Backup исходников создан · ${formatBytes(result.size_bytes || 0)}`);
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
    button.disabled = true;
    try {
      const result = await api("/api/maintenance", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
        }),
      });
      renderMaintenanceResult("Maintenance", result);
      toast(`Обслуживание завершено · ${formatBytes(result.freed_bytes || 0)}`);
      await refresh();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  });

  $("#import-button").addEventListener("click", async () => {
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
      const cookieText = cookies.copied ? ` · cookies ${cookies.kept}/${cookies.before}` : "";
      const modeText = result.copy_mode === "tiktok_session" ? "TikTok session" : "полный профиль";
      toast(`Импортирован ${modeText} · ${formatBytes(result.size_bytes)}${cookieText}`);
      await refresh();
    } catch (error) {
      toast(error.message);
    }
  });

  $("#compact-browser-button").addEventListener("click", async () => {
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
    }
  });

  $("#prune-backups-button").addEventListener("click", async () => {
    try {
      const browser = state.selectedBrowserProfile || (state.diagnostics && state.diagnostics.browser_profile) || {};
      const backupCount = Number(browser.backup_count || 0);
      if (backupCount <= 1) {
        toast("Старых backup нет");
        return;
      }
      const confirmed = window.confirm(
        `Удалить старые backup для профиля ${$("#bot-profile").value}? Будет оставлена последняя backup-точка.`,
      );
      if (!confirmed) return;
      const result = await api("/api/prune-browser-backups", {
        method: "POST",
        body: JSON.stringify({
          bot_profile_key: $("#bot-profile").value,
          keep_latest: 1,
        }),
      });
      toast(`Backup очищены · удалено ${result.removed_count} · освобождено ${formatBytes(result.freed_bytes)}`);
      await loadSelectedBrowserProfile();
    } catch (error) {
      toast(error.message);
    }
  });

  $("#delete-selected-backups-button").addEventListener("click", async () => {
    const button = $("#delete-selected-backups-button");
    const items = selectedBackupItems();
    if (!items.length) {
      toast("Выбери backup для удаления");
      return;
    }
    const confirmed = window.confirm(
      `Удалить выбранные backup для профиля ${$("#bot-profile").value}? Количество: ${items.length}.`,
    );
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

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.autoRefresh) {
    refresh({ silent: true }).catch((error) => toast(error.message));
  }
});

function bindShortcuts() {
  document.addEventListener("keydown", async (event) => {
    if (event.key === "F5") {
      event.preventDefault();
      refresh().catch((error) => toast(error.message));
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
bindNavigation();
bindActions();
bindShortcuts();
refresh().catch((error) => toast(error.message));
startAutoRefresh();
