/* Тонкий слой поверх fetch() для общения с локальным backend. */
const API = (() => {
  const BASE = "/api";

  async function request(path, options = {}) {
    const res = await fetch(BASE + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || detail;
      } catch (_) {}
      throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
  }

  // Заброс запроса намеренной отменой (AbortController) — это не
  // ошибка сети, а нормальный результат смены диалога/размонтирования.
  // Вызывающий код должен проверять это и молча выходить, а не
  // показывать пользователю тост с ошибкой.
  function isAbortError(err) {
    return err && (err.name === "AbortError" || err.code === 20);
  }

  return {
    // contacts
    listContacts: (params = {}) => {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params).filter(([, v]) => v !== "" && v != null))
      ).toString();
      return request(`/contacts${qs ? "?" + qs : ""}`);
    },
    getContact: (id) => request(`/contacts/${id}`),
    createContact: (data) => request(`/contacts`, { method: "POST", body: JSON.stringify(data) }),
    updateContact: (id, data) => request(`/contacts/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    updateStatus: (id, status) => request(`/contacts/${id}/status`, { method: "PATCH", body: JSON.stringify({ status }) }),
    deleteContact: (id) => request(`/contacts/${id}`, { method: "DELETE" }),

    // interactions
    addInteraction: (contactId, data) =>
      request(`/contacts/${contactId}/interactions`, { method: "POST", body: JSON.stringify(data) }),
    deleteInteraction: (contactId, interactionId) =>
      request(`/contacts/${contactId}/interactions/${interactionId}`, { method: "DELETE" }),

    // dashboard / meta
    getDashboard: () => request(`/dashboard`),
    getAttention: () => request(`/attention`),
    getReminders: () => request(`/reminders`),
    getTags: () => request(`/tags`),
    getStatuses: () => request(`/statuses`),

    // contact intelligence (AI-анализ)
    analyzeContact: (id) => request(`/contacts/${id}/analyze`, { method: "POST" }),
    generateDeepReport: (id) => request(`/contacts/${id}/deep-report`, { method: "POST" }),
    getDeepReport: (id) => request(`/contacts/${id}/deep-report`),
    liveScore: (id, messages) =>
      request(`/contacts/${id}/live-score`, { method: "POST", body: JSON.stringify({ messages }) }),
    applySuggestedStatus: (id) => request(`/contacts/${id}/apply-suggested-status`, { method: "POST" }),
    getContactTimeline: (id) => request(`/contacts/${id}/timeline`),

    // telegram — auth & contacts
    tgStatus: () => request(`/telegram/status`),
    tgSendCode: (phone) => request(`/telegram/send-code`, { method: "POST", body: JSON.stringify({ phone }) }),
    tgSignIn: (payload) => request(`/telegram/sign-in`, { method: "POST", body: JSON.stringify(payload) }),
    tgLogout: () => request(`/telegram/logout`, { method: "POST" }),
    tgContacts: () => request(`/telegram/contacts`),
    tgImport: (payload) => request(`/telegram/import`, { method: "POST", body: JSON.stringify(payload) }),
    tgResolveUsername: (username) =>
      request(`/telegram/resolve`, { method: "POST", body: JSON.stringify({ username }) }),

    // telegram — messenger
    tgDialogs: (limit = 100, signal = null) => request(`/telegram/dialogs?limit=${limit}`, { signal }),
    tgPresence: (telegramId, signal = null) => request(`/telegram/presence/${telegramId}`, { signal }),
    tgAvatarUrl: (telegramId) => `/api/telegram/avatar/${telegramId}`,
    tgMediaUrl: (telegramId, messageId) => `/api/telegram/media/${telegramId}/${messageId}`,
    tgMarkRead: (telegramId) => request(`/telegram/read/${telegramId}`, { method: "POST" }),

    tgMessages: (telegramId, limit = 50, signal = null) => request(`/telegram/messages/${telegramId}?limit=${limit}`, { signal }),
    isAbortError,
    tgSendMessage: (telegramId, text, replyTo = null) =>
      request(`/telegram/messages/${telegramId}`, { method: "POST", body: JSON.stringify({ text, reply_to: replyTo }) }),
    tgEditMessage: (telegramId, messageId, text) =>
      request(`/telegram/messages/${telegramId}/${messageId}`, { method: "PATCH", body: JSON.stringify({ text }) }),
    tgDeleteMessage: (telegramId, messageId) =>
      request(`/telegram/messages/${telegramId}/${messageId}`, { method: "DELETE" }),
    tgPinMessage: (telegramId, messageId) =>
      request(`/telegram/messages/${telegramId}/${messageId}/pin`, { method: "POST" }),
    tgUnpinMessage: (telegramId) =>
      request(`/telegram/messages/${telegramId}/unpin`, { method: "POST" }),
    tgForwardMessage: (telegramId, messageId, toTelegramId) =>
      request(`/telegram/messages/${telegramId}/${messageId}/forward`, {
        method: "POST", body: JSON.stringify({ to_telegram_id: toTelegramId }),
      }),
    tgSendFile: async (telegramId, file, { caption = "", replyTo = null, voice = false } = {}) => {
      const form = new FormData();
      form.append("file", file, file.name || "voice.ogg");
      if (caption) form.append("caption", caption);
      if (replyTo) form.append("reply_to", replyTo);
      if (voice) form.append("voice", "true");
      const res = await fetch(`${BASE}/telegram/messages/${telegramId}/file`, { method: "POST", body: form });
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      return res.json();
    },

    // contacts — telegram link
    getContactByTelegramId: (telegramId) => request(`/contacts/by-telegram/${telegramId}`),
  };
})();

/* Небольшие общие утилиты, используемые во всех модулях интерфейса. */
const Utils = (() => {
  function escapeHtml(str) {
    if (str == null) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function initials(name) {
    if (!name) return "?";
    const parts = name.trim().split(/\s+/).slice(0, 2);
    return parts.map((p) => p[0]?.toUpperCase() || "").join("") || "?";
  }

  function formatDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  }

  function formatDateTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return (
      d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }) +
      " · " +
      d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })
    );
  }

  function daysAgo(iso) {
    if (!iso) return null;
    const diff = Date.now() - new Date(iso).getTime();
    return Math.floor(diff / 86400000);
  }

  function daysAgoLabel(iso) {
    const d = daysAgo(iso);
    if (d === null) return "нет данных";
    if (d <= 0) return "сегодня";
    if (d === 1) return "вчера";
    if (d < 5) return `${d} дня назад`;
    return `${d} дн. назад`;
  }

  function toast(message) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = message;
    el.classList.add("is-visible");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.remove("is-visible"), 2200);
  }

  function timeHHMM(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function dayLabel(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86400000);
    if (diffDays === 0) return "Сегодня";
    if (diffDays === 1) return "Вчера";
    if (diffDays < 7) return d.toLocaleDateString("ru-RU", { weekday: "long" });
    return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "long", year: diffDays > 300 ? "numeric" : undefined });
  }

  function presenceLabel(p) {
    if (!p) return "не в сети";
    if (p.typing) return "печатает…";
    if (p.online) return "в сети";
    if (p.last_seen_kind === "exact" && p.last_seen) {
      return "был(а) в сети " + new Date(p.last_seen).toLocaleString("ru-RU", {
        day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
      });
    }
    if (p.last_seen_kind === "recently") return "был(а) недавно";
    if (p.last_seen_kind === "last_week") return "был(а) на этой неделе";
    if (p.last_seen_kind === "last_month") return "был(а) в этом месяце";
    return "давно не в сети";
  }

  function formatFileSize(bytes) {
    if (!bytes && bytes !== 0) return "";
    if (bytes < 1024) return bytes + " Б";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " КБ";
    return (bytes / (1024 * 1024)).toFixed(1) + " МБ";
  }

  // Классы/цвета для AI-оценки интереса (0-100), см. Этап 9.
  function aiScoreBadge(score) {
    if (score == null) return { label: "Не анализировался", cls: "unknown" };
    if (score <= 20) return { label: "Холодная", cls: "cold" };
    if (score <= 50) return { label: "Тёплая", cls: "warm" };
    if (score <= 80) return { label: "Высокий интерес", cls: "hot" };
    return { label: "Очень высокий интерес", cls: "veryhot" };
  }

  // Небольшая цветная "таблетка" с направлением тренда интереса
  // (растёт/стабильно/затухает). Используется и в карточке контакта
  // (Contact Intelligence), и в живой оценке во время переписки —
  // вынесено сюда, чтобы не дублировать разметку в contacts.js/chatview.js.
  const TREND_ICONS = { up: "▲", down: "▼", flat: "▬", unknown: "" };
  function trendChipHTML(trend) {
    if (!trend || trend.direction === "unknown") return "";
    const icon = TREND_ICONS[trend.direction] || "";
    const deltaText = typeof trend.delta === "number" ? ` (${trend.delta > 0 ? "+" : ""}${trend.delta})` : "";
    return `<span class="trend-chip trend-chip--${trend.direction}" title="${escapeHtml(trend.label)}${deltaText}">${icon} ${escapeHtml(trend.label)}</span>`;
  }

  return {
    escapeHtml, initials, formatDate, formatDateTime, daysAgo, daysAgoLabel, toast,
    timeHHMM, dayLabel, presenceLabel, formatFileSize, aiScoreBadge, trendChipHTML,
  };
})();

