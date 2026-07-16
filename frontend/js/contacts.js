/* Список контактов, карточка контакта, история взаимодействий. */
const Contacts = (() => {
  let statuses = [];      // [{value, label}]
  let items = [];         // currently loaded list
  let activeId = null;
  let filters = { search: "", status: "", tag: "", min_interest: null, max_interest: null };
  let searchDebounce = null;
  let chatPollTimer = null;
  let activeChatTelegramId = null;
  const CC_EMOJI = ["😀","😁","😂","🤣","😊","😉","😍","😘","😜","🤔","😎","🥳",
    "😢","😭","😡","😱","🤗","🙏","👍","👎","👏","🙌","🤝","💪",
    "❤️","🔥","✨","🎉","🎂","☕","🍕","🍺","🌹","🌟","💯","✅"];
  let ccMediaRecorder = null;
  let ccRecordedChunks = [];
  let ccRecordStartedAt = 0;
  let ccRecordTimerHandle = null;

  const RING_R = 17;
  const RING_C = 2 * Math.PI * RING_R;

  // Единственно допустимые теги — простые статусы знакомства, без свободного ввода.
  const FIXED_TAGS = [
    { name: "Холодная", cls: "cold" },
    { name: "Тёплая", cls: "warm" },
    { name: "Горячая", cls: "hot" },
    { name: "Встреча", cls: "meeting" },
    { name: "Архив", cls: "archive" },
  ];

  // Совпадает с models.STATUS_ORDER/STATUS_LABELS на backend — нужен,
  // чтобы отрисовать список офлайн, до первого ответа /api/statuses.
  const FALLBACK_STATUSES = [
    { value: "new", label: "Новый" },
    { value: "warm", label: "Тёплый" },
    { value: "in_progress", label: "В работе" },
    { value: "meeting_scheduled", label: "Назначена встреча" },
    { value: "met", label: "Встречались" },
    { value: "archive", label: "Архив" },
  ];

  function interestBadge(level) {
    if (level <= 3) return { label: "Холодная", cls: "cold" };
    if (level <= 6) return { label: "Тёплая", cls: "warm" };
    return { label: "Горячая", cls: "hot" };
  }

  async function init() {
    // Этап 10: показываем закешированный список контактов мгновенно,
    // не дожидаясь ответа сети — пустого экрана быть не должно.
    const cached = await Cache.contacts.getAll();
    if (cached.length) {
      statuses = FALLBACK_STATUSES;
      items = cached.sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
      renderList();
    }

    try {
      statuses = await API.getStatuses();
    } catch (_) {
      statuses = statuses.length ? statuses : FALLBACK_STATUSES;
    }

    fillStatusSelect(document.getElementById("filterStatus"), statuses, true);
    fillStatusSelect(document.getElementById("newContactStatus"), statuses, false);
    fillTagSelect();

    wireToolbar();
    wireModal();

    await reload();
  }

  function fillStatusSelect(select, list, withAllOption) {
    const extra = withAllOption ? "" : "";
    select.innerHTML =
      (withAllOption ? `<option value="">Все статусы</option>` : "") +
      list.map((s) => `<option value="${s.value}">${s.label}</option>`).join("");
  }

  function fillTagSelect() {
    const select = document.getElementById("filterTag");
    select.innerHTML =
      `<option value="">Все теги</option>` +
      FIXED_TAGS.map((t) => `<option value="${t.name}">${t.name}</option>`).join("");
  }

  function wireToolbar() {
    document.getElementById("searchInput").addEventListener("input", (e) => {
      clearTimeout(searchDebounce);
      const val = e.target.value;
      searchDebounce = setTimeout(() => {
        filters.search = val;
        reload();
      }, 250);
    });

    document.getElementById("filterStatus").addEventListener("change", (e) => {
      filters.status = e.target.value;
      reload();
    });

    document.getElementById("filterTag").addEventListener("change", (e) => {
      filters.tag = e.target.value;
      reload();
    });

    document.getElementById("filterInterest").addEventListener("change", (e) => {
      const val = e.target.value;
      if (!val) {
        filters.min_interest = null;
        filters.max_interest = null;
      } else {
        const [min, max] = val.split("-").map(Number);
        filters.min_interest = min;
        filters.max_interest = max;
      }
      reload();
    });

    document.getElementById("btnNewContact").addEventListener("click", openNewContactModal);
  }

  function wireModal() {
    const range = document.getElementById("newInterestRange");
    const val = document.getElementById("newInterestVal");
    range.addEventListener("input", () => (val.textContent = range.value));

    document.querySelectorAll("[data-close-modal]").forEach((btn) =>
      btn.addEventListener("click", closeModal)
    );

    document.getElementById("newContactTags").addEventListener("click", (e) => {
      const btn = e.target.closest(".tag-toggle");
      if (btn) btn.classList.toggle("is-active");
    });

    document.getElementById("formNewContact").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      const payload = {
        name: fd.get("name").trim(),
        username: cleanUsername(fd.get("username")),
        photo_url: fd.get("photo_url") || null,
        source: fd.get("source") || null,
        status: fd.get("status"),
        interest_level: Number(fd.get("interest_level")),
        notes: fd.get("notes") || null,
        tags: [...document.querySelectorAll("#newContactTags .tag-toggle.is-active")].map((b) => b.dataset.tag),
      };
      if (!payload.name) return;

      const created = await API.createContact(payload);
      closeModal();
      e.target.reset();
      document.getElementById("newInterestVal").textContent = "5";
      document.querySelectorAll("#newContactTags .tag-toggle").forEach((b) => b.classList.remove("is-active"));
      Utils.toast("Контакт создан");
      await reload();
      selectContact(created.id);
    });
  }

  function cleanUsername(v) {
    if (!v) return null;
    v = v.trim();
    if (!v) return null;
    return v.startsWith("@") ? v : "@" + v;
  }

  function openNewContactModal() {
    document.getElementById("modalNewContact").hidden = false;
  }
  function closeModal() {
    document.getElementById("modalNewContact").hidden = true;
  }

  async function reload() {
    const isDefaultView = !filters.search && !filters.status && !filters.tag && filters.min_interest == null && filters.max_interest == null;
    try {
      items = await API.listContacts(filters);
    } catch (err) {
      // Сети/backend нет — остаёмся с тем, что уже показано из кеша,
      // и просто сообщаем об этом, не роняя интерфейс.
      Utils.toast("Нет соединения с сервером — показаны сохранённые данные");
      return;
    }
    renderList();
    if (isDefaultView) {
      Cache.contacts.replaceAll(items);
      Cache.setLastSync("contactsSyncedAt", Date.now());
    }
  }

  function statusLabel(value) {
    return statuses.find((s) => s.value === value)?.label || value;
  }

  function avatarRingSVG(interest, sizePx = 38) {
    const pct = Math.max(0, Math.min(10, interest)) / 10;
    const offset = RING_C * (1 - pct);
    const color = interest >= 8 ? "var(--teal)" : interest >= 4 ? "var(--primary)" : "var(--ink-faint)";
    return `
      <svg width="${sizePx}" height="${sizePx}" viewBox="0 0 38 38">
        <circle cx="19" cy="19" r="${RING_R}" fill="none" stroke="var(--border)" stroke-width="2.4"/>
        <circle cx="19" cy="19" r="${RING_R}" fill="none" stroke="${color}" stroke-width="2.4"
          stroke-linecap="round" stroke-dasharray="${RING_C}" stroke-dashoffset="${offset}"/>
      </svg>`;
  }

  function avatarInner(contact) {
    if (contact.photo_url) {
      return `<img class="avatar-ring__img" src="${Utils.escapeHtml(contact.photo_url)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar-ring__fallback',textContent:'${Utils.initials(contact.name)}'}))">`;
    }
    // Для контактов, импортированных из Telegram, отдельного photo_url в
    // CRM обычно нет — вместо initials-заглушки пробуем настоящее фото
    // профиля через тот же кэширующий эндпоинт, что уже использует
    // вкладка "Чат" (см. avatarHtml() в chatview.js). Если фото нет —
    // онлайн-эндпоинт вернёт 404, onerror тихо откатится на initials.
    if (contact.telegram_id) {
      return `<img class="avatar-ring__img" src="${API.tgAvatarUrl(contact.telegram_id)}" alt="" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'avatar-ring__fallback',textContent:'${Utils.initials(contact.name)}'}))">`;
    }
    return `<div class="avatar-ring__fallback">${Utils.initials(contact.name)}</div>`;
  }

  function renderList() {
    const el = document.getElementById("contactItems");
    if (!items.length) {
      el.innerHTML = `<div class="empty-col">Ничего не найдено.<br>Измените фильтры или добавьте контакт.</div>`;
      return;
    }
    el.innerHTML = items
      .map(
        (c) => `
      <div class="ccard ${c.id === activeId ? "is-active" : ""}" data-id="${c.id}" tabindex="0">
        <div class="avatar-ring">${avatarRingSVG(c.interest_level)}<div style="position:absolute">${avatarInner(c)}</div></div>
        <div class="ccard__body">
          <div class="ccard__top">
            <span class="ccard__name">${Utils.escapeHtml(c.name)}</span>
          </div>
          <div class="ccard__uname">${Utils.escapeHtml(c.username || "\u2014")}</div>
          <div class="ccard__row2">
            <span class="badge status-${c.status}"><span class="dot"></span>${statusLabel(c.status)}</span>
            ${c.analyzed_at ? `<span class="badge ai-badge ai-badge--${Utils.aiScoreBadge(c.interest_score).cls}" title="AI-оценка интереса">${c.interest_score}</span>` : ""}
          </div>
        </div>
      </div>`
      )
      .join("");

    el.querySelectorAll(".ccard").forEach((card) => {
      card.addEventListener("click", () => selectContact(Number(card.dataset.id)));
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter") selectContact(Number(card.dataset.id));
      });
    });
  }

  async function selectContact(id) {
    stopChatPolling();
    activeId = id;
    document.querySelectorAll(".ccard").forEach((c) => c.classList.toggle("is-active", Number(c.dataset.id) === id));
    document.getElementById("contactsBodyTable").classList.add("has-open-detail");

    const contact = await API.getContact(id);
    renderDetail(contact);

    document.getElementById("codetailEmpty").hidden = true;
    const body = document.getElementById("codetailBody");
    body.hidden = false;
  }

  function backToContactList() {
    activeId = null;
    document.getElementById("contactsBodyTable").classList.remove("has-open-detail");
    document.querySelectorAll(".ccard").forEach((c) => c.classList.remove("is-active"));
  }

  function renderDetail(c) {
    const body = document.getElementById("codetailBody");
    body.innerHTML = `
      <section class="co-main">
        <div class="co-head">
          <div class="co-avatar">${
            c.photo_url
              ? `<img src="${Utils.escapeHtml(c.photo_url)}" alt="" style="width:100%;height:100%;border-radius:50%;object-fit:cover" onerror="this.parentElement.textContent='${Utils.initials(c.name)}'">`
              : c.telegram_id
                ? `<img src="${API.tgAvatarUrl(c.telegram_id)}" alt="" style="width:100%;height:100%;border-radius:50%;object-fit:cover" onerror="this.parentElement.textContent='${Utils.initials(c.name)}'">`
                : Utils.initials(c.name)
          }</div>
          <div>
            <div class="co-head__name">${Utils.escapeHtml(c.name)}</div>
            <div class="co-head__uname">${Utils.escapeHtml(c.username || "без username")}</div>
          </div>
          <div class="co-head__actions">
            <button class="btn btn--icon co-head__back" id="btnBackToContactList" title="К списку контактов">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
            </button>
            <button class="btn btn--danger" id="btnDeleteContact">Удалить</button>
          </div>
        </div>

        ${aiPanelHTML(c)}

        ${deepReportPanelHTML(c)}

        <div class="co-fieldgrid">
          <div class="co-field">
            <label>Имя</label>
            <input type="text" id="f-name" value="${Utils.escapeHtml(c.name)}">
          </div>
          <div class="co-field">
            <label>Username</label>
            <input type="text" id="f-username" value="${Utils.escapeHtml(c.username || "")}">
          </div>
          <div class="co-field">
            <label>Дата добавления</label>
            <input type="text" value="${Utils.formatDate(c.created_at)}" disabled>
          </div>
          <div class="co-field">
            <label>Источник знакомства</label>
            <input type="text" id="f-source" value="${Utils.escapeHtml(c.source || "")}">
          </div>
          <div class="co-field">
            <label>Статус</label>
            <select id="f-status">${statuses.map((s) => `<option value="${s.value}" ${s.value === c.status ? "selected" : ""}>${s.label}</option>`).join("")}</select>
          </div>
          <div class="co-field">
            <label>Интерес</label>
            <div class="interest-slider">
              <input type="range" min="1" max="10" id="f-interest" value="${c.interest_level}">
              <span class="interest-slider__val" id="f-interest-val">${c.interest_level}</span>
              <span class="interest-badge interest-badge--${interestBadge(c.interest_level).cls}" id="f-interest-badge">${interestBadge(c.interest_level).label}</span>
            </div>
          </div>
          <div class="co-field span2">
            <label>Последний контакт</label>
            <input type="text" id="f-last-contact" value="Загрузка…" disabled>
          </div>
          <div class="co-field span2">
            <label>Следующая задача</label>
            <input type="text" id="f-next-task" value="${Utils.escapeHtml(c.next_task || "")}" placeholder="Например: написать первым">
          </div>
          <div class="co-field span2">
            <label>Теги</label>
            <div class="tag-toggle-row" id="tagToggleRow">
              ${FIXED_TAGS.map((t) => `<button type="button" class="tag-toggle tag-toggle--${t.cls} ${c.tags.some((x) => x.name === t.name) ? "is-active" : ""}" data-tag="${t.name}">${t.name}</button>`).join("")}
            </div>
          </div>
          <div class="co-field span2">
            <label>Заметки</label>
            <textarea id="f-notes" rows="3" placeholder="Свободные заметки о контакте">${Utils.escapeHtml(c.notes || "")}</textarea>
          </div>
        </div>

        <div class="form__actions" style="justify-content:flex-start">
          <button class="btn btn--primary" id="btnSaveContact">Сохранить изменения</button>
        </div>
      </section>

      <aside class="co-side">
        <div class="co-side__tabs">
          <button class="co-tab ${c.telegram_id ? "is-active" : ""}" data-tab="chat">Чат</button>
          <button class="co-tab ${c.telegram_id ? "" : "is-active"}" data-tab="history">История</button>
        </div>

        <div class="co-tabpane" id="paneChat" ${c.telegram_id ? "" : "hidden"}>
          ${chatPaneHTML(c)}
        </div>

        <div class="co-tabpane" id="paneHistory" ${c.telegram_id ? "hidden" : ""}>
          ${historyPaneHTML(c)}
        </div>
      </aside>
    `;

    wireDetailEvents(c);
    updateLastContactDisplay(c);
  }

  async function updateLastContactDisplay(c) {
    const el = document.getElementById("f-last-contact");
    if (!el) return;
    const fallback = () => {
      el.value = c.last_contact_at
        ? `${Utils.formatDate(c.last_contact_at)} \u00b7 ${Utils.daysAgoLabel(c.last_contact_at)}`
        : "нет данных";
    };
    if (!c.telegram_id) { fallback(); return; }
    try {
      const messages = await API.tgMessages(c.telegram_id, 1);
      if (document.getElementById("f-last-contact") !== el) return; // карточка уже сменилась
      if (!messages.length) { el.value = "переписки ещё нет"; return; }
      const last = messages[messages.length - 1];
      const who = last.out ? "Вы написали" : `${c.name} написал(а)`;
      el.value = `${Utils.formatDate(last.date)} \u00b7 ${who} последним`;
    } catch (err) {
      fallback();
    }
  }

  const AI_SOURCE_LABELS = {
    gemini: "Gemini",
    local: "локально",
  };

  function aiPanelHTML(c) {
    const badge = Utils.aiScoreBadge(c.analyzed_at ? c.interest_score : null);
    const hasSuggestion = c.analyzed_at && c.suggested_status && c.suggested_status !== c.status;
    const sourceLabel = AI_SOURCE_LABELS[c.ai_source] || "локально";
    const isLlm = c.analyzed_at && c.ai_source && c.ai_source !== "local";
    return `
      <div class="ai-panel">
        <div class="ai-panel__head">
          <div class="ai-panel__title">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3v3M12 18v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M3 12h3M18 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/><circle cx="12" cy="12" r="4"/></svg>
            Contact Intelligence
            ${c.analyzed_at ? `<span class="ai-panel__source" title="${isLlm ? "Summary и следующее действие сгенерированы внешним API" : "Посчитано локально, без внешних сервисов"}">${sourceLabel}</span>` : ""}
          </div>
          <button class="btn btn--icon" id="btnAnalyzeContact" title="Обновить AI-анализ" type="button">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg>
          </button>
        </div>

        ${c.analyzed_at ? `
        <div class="ai-panel__score">
          <span class="ai-badge ai-badge--${badge.cls} ai-badge--lg">${c.interest_score}<small>/100</small></span>
          <div class="ai-panel__score-meta">
            <div class="ai-panel__category">${Utils.escapeHtml(badge.label)} ${Utils.trendChipHTML(c.trend)}</div>
            <div class="ai-panel__analyzed">Анализ обновлён: ${Utils.formatDateTime(c.analyzed_at)}</div>
          </div>
        </div>
        <div class="ai-panel__row"><span class="ai-panel__label">Следующее действие</span><span class="ai-panel__value">${Utils.escapeHtml(c.next_action || "\u2014")}</span></div>
        <div class="ai-panel__summary">${Utils.escapeHtml(c.ai_summary || "")}</div>
        ${hasSuggestion ? `
        <div class="ai-panel__suggestion">
          Предложенный статус: <b>${statusLabel(c.suggested_status)}</b>
          <button class="btn btn--primary btn--sm" id="btnApplySuggestedStatus" type="button">Применить</button>
        </div>` : ""}
        ${c.suggested_reply ? `
        <div class="ai-panel__reply">
          <div class="ai-panel__label">Черновик ответа</div>
          <div class="ai-panel__reply-text">${Utils.escapeHtml(c.suggested_reply)}</div>
          <button class="btn btn--sm" id="btnCopySuggestedReply" type="button">Скопировать</button>
        </div>` : ""}
        ` : `
        <div class="ai-panel__empty">Анализ ещё не запускался. Нажмите на значок обновления, чтобы оценить интерес по переписке.</div>
        `}
      </div>`;
  }

  async function runAnalysis(contactId) {
    const btn = document.getElementById("btnAnalyzeContact");
    if (btn) btn.classList.add("is-spinning");
    try {
      await API.analyzeContact(contactId);
      const fresh = await API.getContact(contactId);
      if (activeId === contactId) renderDetail(fresh);
      Utils.toast("Анализ обновлён");
    } catch (err) {
      Utils.toast(err.message || "Не удалось выполнить анализ");
    } finally {
      if (btn) btn.classList.remove("is-spinning");
    }
  }

  // ---------- Глубокий AI-отчёт (требует Gemini) ----------
  // Состояние держим отдельно от объекта контакта (оно не приходит вместе
  // с обычным ContactOut — слишком тяжёлое, чтобы гонять его при каждом
  // списке), и обновляем точечно один DOM-узел #deepReportBox, а не
  // перерисовываем всю карточку — так быстрее и не сбрасывает фокус/скролл.
  const deepReportState = {}; // contactId -> { loading, data, attempted }

  function drBar(label, split) {
    return `
      <div class="dr-bar">
        <div class="dr-bar__label">${Utils.escapeHtml(label)}</div>
        <div class="dr-bar__track"><div class="dr-bar__fill" style="width:${split.her}%"></div></div>
        <div class="dr-bar__nums"><span>Я: ${split.me}%</span><span>Собеседник: ${split.her}%</span></div>
      </div>`;
  }

  function drList(title, items, tone) {
    if (!items || !items.length) return "";
    return `
      <div class="dr-list dr-list--${tone}">
        <div class="dr-list__title">${Utils.escapeHtml(title)}</div>
        <ul>${items.map((i) => `<li>${Utils.escapeHtml(i)}</li>`).join("")}</ul>
      </div>`;
  }

  function deepReportInner(c, st) {
    const head = (rightBtn) => `
      <div class="ai-panel__head">
        <div class="ai-panel__title">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/></svg>
          Глубокий AI-отчёт <span class="ai-panel__source">Gemini</span>
        </div>
        ${rightBtn || ""}
      </div>`;

    if (st.loading) {
      return `${head("")}<div class="ai-panel__empty">Gemini разбирает переписку — обычно занимает 5-15 секунд…</div>`;
    }

    if (st.data) {
      const r = st.data;
      const refreshBtn = `<button class="btn btn--icon" id="btnDeepReportRefresh" title="Пересчитать" type="button">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5"/></svg>
      </button>`;
      return `
        ${head(refreshBtn)}
        <div class="ai-panel__score">
          <span class="ai-badge ai-badge--${Utils.aiScoreBadge(r.interest_score).cls} ai-badge--lg">${r.interest_score}<small>/100</small></span>
          <div class="ai-panel__score-meta">
            <div class="ai-panel__category">${Utils.escapeHtml(r.category || "")}${r.trend ? " · " + Utils.escapeHtml(r.trend) : ""}</div>
            <div class="ai-panel__analyzed">Отчёт от ${Utils.formatDateTime(r.generated_at)}</div>
          </div>
        </div>
        <div class="dr-bars">
          ${drBar("Инициатива", r.initiative)}
          ${drBar("Вложенность в общение", r.investment)}
          ${drBar("Кто тащит диалог", r.conversation_driver)}
        </div>
        <div class="dr-probs">
          <span class="dr-prob" title="Вероятность встречи, если пригласить сейчас">Встреча: <b>${r.meeting_probability}%</b></span>
          <span class="dr-prob" title="Насколько уместно сейчас предложить встречу">Уместно позвать: <b>${r.date_invite_probability}%</b></span>
          <span class="dr-prob" title="Вероятность, что собеседник перестанет отвечать">Риск игнора: <b>${r.ghost_probability}%</b></span>
          <span class="dr-prob" title="Насколько сильно вы давите (частые сообщения, повторные вопросы)">Ваше давление: <b>${r.pressure_score}%</b></span>
        </div>
        ${drList("Зелёные флаги", r.green_flags, "good")}
        ${drList("Красные флаги", r.red_flags, "bad")}
        ${drList("Ваши ошибки в последних сообщениях", r.mistakes, "bad")}
        ${drList("Рекомендации", r.improvements, "good")}
        <div class="ai-panel__row"><span class="ai-panel__label">Следующий шаг</span><span class="ai-panel__value">${Utils.escapeHtml(r.next_action || "\u2014")}</span></div>
        ${r.reasoning ? `<div class="ai-panel__summary">${Utils.escapeHtml(r.reasoning)}</div>` : ""}
      `;
    }

    return `
      ${head("")}
      <div class="ai-panel__empty">Развёрнутый разбор: инициатива, вложенность, флирт, красные/зелёные флаги, ваши ошибки и рекомендации — по всей переписке.</div>
      <button class="btn btn--primary btn--sm" id="btnDeepReportRun" type="button">Сгенерировать отчёт</button>
    `;
  }

  function deepReportPanelHTML(c) {
    if (!c.telegram_id) return "";
    const st = deepReportState[c.id] || { loading: false, data: null, attempted: false };
    return `<div class="ai-panel" id="deepReportBox">${deepReportInner(c, st)}</div>`;
  }

  function patchDeepReportBox(c) {
    const box = document.getElementById("deepReportBox");
    if (!box) return;
    const st = deepReportState[c.id] || { loading: false, data: null, attempted: false };
    box.innerHTML = deepReportInner(c, st);
    wireDeepReportButtons(c);
  }

  function wireDeepReportButtons(c) {
    const btnRun = document.getElementById("btnDeepReportRun");
    if (btnRun) btnRun.addEventListener("click", () => runDeepReport(c));
    const btnRefresh = document.getElementById("btnDeepReportRefresh");
    if (btnRefresh) btnRefresh.addEventListener("click", () => runDeepReport(c));
  }

  async function runDeepReport(c) {
    deepReportState[c.id] = { ...(deepReportState[c.id] || {}), loading: true, attempted: true };
    patchDeepReportBox(c);
    try {
      const data = await API.generateDeepReport(c.id);
      deepReportState[c.id] = { loading: false, data, attempted: true };
    } catch (err) {
      deepReportState[c.id] = { loading: false, data: (deepReportState[c.id] || {}).data || null, attempted: true };
      Utils.toast(err.message || "Не удалось построить отчёт");
    }
    if (activeId === c.id) patchDeepReportBox(c);
  }

  // При первом открытии карточки в этой сессии тихо проверяем, нет ли уже
  // сохранённого отчёта (GET, бесплатно, без обращения к Gemini) — чтобы
  // после перезагрузки страницы не пропадал последний посчитанный отчёт.
  async function loadSavedDeepReportIfNeeded(c) {
    if (!c.telegram_id) return;
    if (deepReportState[c.id] && deepReportState[c.id].attempted) return;
    deepReportState[c.id] = { loading: false, data: null, attempted: true };
    try {
      const data = await API.getDeepReport(c.id);
      deepReportState[c.id] = { loading: false, data, attempted: true };
      if (activeId === c.id) patchDeepReportBox(c);
    } catch (err) {
      // 404 = отчёт ещё не запускался — это нормальное состояние, не ошибка.
    }
  }

  async function applySuggestedStatus(contactId) {
    try {
      const fresh = await API.applySuggestedStatus(contactId);
      if (activeId === contactId) renderDetail(fresh);
      await reload();
      Utils.toast("Статус обновлён");
    } catch (err) {
      Utils.toast(err.message || "Не удалось обновить статус");
    }
  }

  function chatPaneHTML(c) {
    if (!c.telegram_id) {
      return `
        <div class="chat-link">
          <p class="tg-hint">Контакт не привязан к Telegram-аккаунту — переписка недоступна.</p>
          ${
            c.username
              ? `<button class="btn btn--primary" id="btnLinkTelegram">Привязать по ${Utils.escapeHtml(c.username)}</button>`
              : `<p class="tg-hint">Укажите username и сохраните контакт, чтобы привязать переписку.</p>`
          }
        </div>`;
    }
    return `
      <div class="chat">
        <header class="thread__head">
          <div class="thread__head-avatar">${avatarInner(c)}</div>
          <div class="thread__head-info">
            <div class="thread__head-name">${Utils.escapeHtml(c.name)}</div>
            <div class="thread__head-status" id="ccStatus">${Utils.escapeHtml(c.username || "")}</div>
          </div>
          <div class="thread__head-actions">
            <button class="btn btn--icon" id="btnOpenInChat" type="button" title="Открыть в разделе «Чат»">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 5.5h16a1 1 0 0 1 1 1V16a1 1 0 0 1-1 1H9l-4.5 4V17H4a1 1 0 0 1-1-1V6.5a1 1 0 0 1 1-1Z"/></svg>
            </button>
          </div>
        </header>
        <div class="thread__messages" id="chatMessages"><div class="chat__empty">Загрузка переписки…</div></div>
        <div class="composer-context-bar" id="ccContextBar" hidden></div>
        <form class="composer" id="ccComposerForm">
          <input type="file" id="ccFileInput" hidden>
          <button type="button" class="composer__btn" id="ccBtnEmoji" title="Эмодзи">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M8.5 14c1 1.3 2.2 2 3.5 2s2.5-.7 3.5-2"/><circle cx="9" cy="9.5" r=".9" fill="currentColor" stroke="none"/><circle cx="15" cy="9.5" r=".9" fill="currentColor" stroke="none"/></svg>
          </button>
          <button type="button" class="composer__btn" id="ccBtnAttach" title="Прикрепить файл">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M20 11.5 12.4 19a4.5 4.5 0 0 1-6.4-6.4l7.6-7.5a3 3 0 0 1 4.3 4.2L10.3 17a1.5 1.5 0 0 1-2.2-2.1l6.8-6.8"/></svg>
          </button>
          <button type="button" class="composer__btn composer__btn--gallery" id="ccBtnGallery" title="Медиатека">
            <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3.5" y="4.5" width="17" height="15" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.6" fill="currentColor" stroke="none"/><path d="M5 17l4.5-5 3.5 3.8 2.5-2.8L20 17"/></svg>
          </button>
          <div class="composer__field" id="ccComposerField">
            <textarea id="chatText" placeholder="Написать сообщение…" rows="1"></textarea>
          </div>
          <button type="submit" class="composer__send" id="ccComposerSend" title="Отправить">
            <svg id="ccIconSend" width="17" height="17" viewBox="0 0 24 24" fill="currentColor"><path d="M3.4 20.6 21 12 3.4 3.4 3 10l12 2-12 2 .4 6.6Z"/></svg>
            <svg id="ccIconMic" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" hidden><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/></svg>
          </button>
        </form>
      </div>`;
  }

  function historyPaneHTML(c) {
    return `
      <div class="timeline" id="timeline">
        ${
          c.interactions
            .map(
              (i) => `
          <div class="tl-item type-${i.event_type}">
            <div class="tl-item__date">${Utils.formatDateTime(i.occurred_at)}</div>
            <div class="tl-item__note">${Utils.escapeHtml(i.note)}</div>
            <button class="tl-item__del" data-del-interaction="${i.id}">удалить</button>
          </div>`
            )
            .join("") || `<div class="empty-hint">Пока нет записей.</div>`
        }
      </div>

      <div class="add-note">
        <textarea id="newNoteText" placeholder="Что произошло? Например: 'Хорошо пообщались, договорились о встрече'"></textarea>
        <div class="add-note__row">
          <select id="newNoteType">
            <option value="note">Заметка</option>
            <option value="message">Переписка</option>
            <option value="meeting">Встреча</option>
          </select>
          <button class="btn btn--primary" id="btnAddNote">Добавить запись</button>
        </div>
      </div>`;
  }

  function stopChatPolling() {
    if (chatPollTimer) {
      clearInterval(chatPollTimer);
      chatPollTimer = null;
    }
    if (ccMessagesAbortController) {
      ccMessagesAbortController.abort();
      ccMessagesAbortController = null;
    }
    activeChatTelegramId = null;
  }

  function ccBubbleContent(m, telegramId) {
    let html = "";
    if (m.media) {
      html += MediaMessage.render(m.media, API.tgMediaUrl(telegramId, m.id), m.id);
    }
    if (m.text) html += Utils.escapeHtml(m.text);
    return html;
  }

  let ccCurrentAudio = null;
  function ccSetVoiceIcon(wrap, playing) {
    const playIcon = wrap.querySelector(".att-voice__icon-play");
    const pauseIcon = wrap.querySelector(".att-voice__icon-pause");
    if (playIcon) playIcon.hidden = playing;
    if (pauseIcon) pauseIcon.hidden = !playing;
  }
  function ccTogglePlay(btn) {
    const wrap = btn.closest(".att-voice");
    const url = wrap.dataset.voiceUrl;
    if (ccCurrentAudio && ccCurrentAudio._wrap === wrap && !ccCurrentAudio.paused) {
      ccCurrentAudio.pause();
      ccSetVoiceIcon(wrap, false);
      return;
    }
    if (ccCurrentAudio) { ccCurrentAudio.pause(); if (ccCurrentAudio._wrap) ccSetVoiceIcon(ccCurrentAudio._wrap, false); }
    const audio = new Audio(url);
    audio._wrap = wrap;
    const bars = wrap.querySelectorAll(".att-voice__wave span");
    audio.addEventListener("timeupdate", () => {
      if (!audio.duration || !bars.length) return;
      const activeCount = Math.round((audio.currentTime / audio.duration) * bars.length);
      bars.forEach((bar, i) => bar.classList.toggle("is-played", i < activeCount));
    });
    audio.addEventListener("ended", () => { ccSetVoiceIcon(wrap, false); bars.forEach((bar) => bar.classList.remove("is-played")); });
    audio.addEventListener("pause", () => ccSetVoiceIcon(wrap, false));
    audio.play().then(() => ccSetVoiceIcon(wrap, true)).catch(() => Utils.toast("Не удалось воспроизвести голосовое сообщение"));
    ccCurrentAudio = audio;
  }

  // Фикс "кружок куда-то уходит" (см. тот же приём в chatview.js): опрос
  // встроенного чата (chatPollTimer, каждые 4с) пересобирал innerHTML
  // целиком при каждом тике, что обрывало воспроизведение видео/кружков/
  // голосовых. Сигнатура позволяет пропустить пересборку, если состав и
  // статусы сообщений не изменились.
  let ccLastSignature = null;
  function ccMessagesSignature(messages) {
    return messages.map((m) => `${m.id}:${m.edited ? 1 : 0}:${m.status || ""}`).join(",");
  }

  function renderMessages(messages, telegramId) {
    const container = document.getElementById("chatMessages");
    if (!container) return;
    if (!messages.length) {
      container.innerHTML = `<div class="chat__empty">Переписки пока нет — напишите первым.</div>`;
      return;
    }
    const signature = `${telegramId}::${ccMessagesSignature(messages)}`;
    if (container.childElementCount > 0 && signature === ccLastSignature) return;
    ccLastSignature = signature;
    let html = "";
    let lastDate = null, lastOut = null;
    messages.forEach((m) => {
      const dLabel = m.date ? Utils.dayLabel(m.date) : "";
      if (dLabel && dLabel !== lastDate) {
        html += `<div class="msg-date">${dLabel}</div>`;
        lastDate = dLabel; lastOut = null;
      }
      const grouped = m.out === lastOut;
      lastOut = m.out;
      const readTick = m.out ? (m.status === "read"
        ? `<svg class="tick-read" width="14" height="10" viewBox="0 0 16 11" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m1 5.5 3.2 3.5L10 2"/><path d="m6 5.5 3.2 3.5L15 2"/></svg>`
        : `<svg class="tick-sent" width="14" height="10" viewBox="0 0 16 11" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m6 5.5 3.2 3.5L15 2"/></svg>`) : "";
      html += `
        <div class="msg-row is-${m.out ? "out" : "in"} ${grouped ? "is-grouped" : ""}">
          <div class="bubble">${ccBubbleContent(m, telegramId)}</div>
          <div class="msg-meta">${m.edited ? "<span>изменено</span>" : ""}<span>${Utils.timeHHMM(m.date)}</span>${readTick}</div>
        </div>`;
    });
    container.innerHTML = html;
    container.scrollTop = container.scrollHeight;
    container.querySelectorAll(".att-voice__play").forEach((btn) => btn.addEventListener("click", () => ccTogglePlay(btn)));
    MediaMessage.wire(container);
  }

  async function refreshPresence(c) {
    const statusEl = document.getElementById("ccStatus");
    if (!statusEl) return;
    try {
      const p = await API.tgPresence(c.telegram_id);
      if (p.typing) { statusEl.textContent = "печатает…"; statusEl.className = "thread__head-status is-typing"; }
      else if (p.online) { statusEl.textContent = "в сети"; statusEl.className = "thread__head-status is-online"; }
      else { statusEl.textContent = Utils.presenceLabel(p); statusEl.className = "thread__head-status"; }
    } catch (_) { /* статус не критичен */ }
  }

  let ccMessagesAbortController = null;

  async function loadAndRenderMessages(c, { silent } = {}) {
    if (silent && API.isBackedOff("/telegram/messages")) return;
    const requestedDialogId = c.telegram_id;
    activeChatTelegramId = requestedDialogId;
    const container = document.getElementById("chatMessages");
    if (!container) return;
    if (!silent) container.innerHTML = `<div class="chat__empty">Загрузка переписки…</div>`;

    if (ccMessagesAbortController) ccMessagesAbortController.abort();
    const controller = new AbortController();
    ccMessagesAbortController = controller;

    try {
      const fresh = await API.tgMessages(requestedDialogId, 50, controller.signal);
      // Двойная защита: диалог мог смениться, пока ответ летел, а
      // сами сообщения обязаны принадлежать именно этому dialog_id.
      if (activeChatTelegramId !== requestedDialogId) return;
      const belongsToDialog = fresh.filter((m) => m.dialog_id === requestedDialogId);
      renderMessages(belongsToDialog, requestedDialogId);
      refreshPresence(c);
    } catch (err) {
      if (API.isAbortError(err)) return;
      if (activeChatTelegramId !== requestedDialogId) return;
      container.innerHTML = `<div class="chat__empty">${Utils.escapeHtml(err.message || "Не удалось загрузить переписку")}</div>`;
      return;
    }
    stopChatPolling();
    activeChatTelegramId = requestedDialogId;
    chatPollTimer = setInterval(() => loadAndRenderMessages(c, { silent: true }), 4000);
  }

  function ccAutosize(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 110) + "px";
  }

  function ccUpdateSendIcon() {
    const hasText = document.getElementById("chatText").value.trim().length > 0;
    document.getElementById("ccIconSend").hidden = !hasText;
    document.getElementById("ccIconMic").hidden = hasText;
  }

  function wireChatComposer(c) {
    const textarea = document.getElementById("chatText");
    const form = document.getElementById("ccComposerForm");

    textarea.addEventListener("input", () => { ccAutosize(textarea); ccUpdateSendIcon(); });
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        form.requestSubmit();
      }
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = textarea.value.trim();
      if (!text) return;
      textarea.value = "";
      ccAutosize(textarea); ccUpdateSendIcon();
      try {
        await API.tgSendMessage(c.telegram_id, text);
        await loadAndRenderMessages(c, { silent: true });
        API.updateContact(c.id, { last_contact_at: new Date().toISOString() }).catch(() => {});
      } catch (err) {
        Utils.toast(err.message || "Не удалось отправить сообщение");
      }
    });

    // ---- emoji picker ----
    document.getElementById("ccBtnEmoji").addEventListener("click", (e) => {
      e.stopPropagation();
      let pop = document.getElementById("ccEmojiPopover");
      if (pop) { pop.remove(); return; }
      pop = document.createElement("div");
      pop.id = "ccEmojiPopover";
      pop.className = "emoji-popover";
      pop.innerHTML = CC_EMOJI.map((em) => `<button type="button" class="emoji-popover__btn">${em}</button>`).join("");
      form.appendChild(pop);
      pop.querySelectorAll("button").forEach((btn) => {
        btn.addEventListener("click", () => {
          const start = textarea.selectionStart || textarea.value.length;
          const end = textarea.selectionEnd || textarea.value.length;
          textarea.value = textarea.value.slice(0, start) + btn.textContent + textarea.value.slice(end);
          textarea.focus();
          textarea.selectionStart = textarea.selectionEnd = start + btn.textContent.length;
          ccAutosize(textarea); ccUpdateSendIcon();
        });
      });
      document.addEventListener("click", function closeOnOutside(ev) {
        if (!pop.contains(ev.target) && ev.target.id !== "ccBtnEmoji") {
          pop.remove();
          document.removeEventListener("click", closeOnOutside);
        }
      }, { capture: true });
    });

    // ---- file attach ----
    document.getElementById("ccBtnAttach").addEventListener("click", () => document.getElementById("ccFileInput").click());
    document.getElementById("ccFileInput").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      e.target.value = "";
      try {
        await API.tgSendFile(c.telegram_id, file, {});
        await loadAndRenderMessages(c, { silent: true });
      } catch (err) {
        Utils.toast(err.message || "Не удалось отправить файл");
      }
    });

    // ---- медиатека (та же галерея, что и в разделе «Чат») ----
    document.getElementById("ccBtnGallery").addEventListener("click", () => {
      MediaLibrary.open({
        dialogId: c.telegram_id,
        title: "Медиатека",
        onSelect: async (media) => {
          try {
            await API.tgSendMediaFile(c.telegram_id, media.id, {});
            await loadAndRenderMessages(c, { silent: true });
          } catch (err) {
            Utils.toast(err.message || "Не удалось отправить файл");
          }
        },
      });
    });

    // ---- voice recording ----
    let micHeld = false;
    document.getElementById("ccComposerSend").addEventListener("click", (e) => {
      if (document.getElementById("ccIconMic").hidden) return; // текст — обычная отправка формой
      e.preventDefault();
      if (!micHeld) { micHeld = true; ccStartRecording(c); }
      else { micHeld = false; ccStopRecording(false); }
    });

    ccUpdateSendIcon();
  }

  async function ccStartRecording(c) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      Utils.toast("Запись голоса не поддерживается этим браузером");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      ccRecordedChunks = [];
      ccMediaRecorder = new MediaRecorder(stream);
      ccMediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) ccRecordedChunks.push(e.data); };
      ccMediaRecorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(ccRecordedChunks, { type: "audio/webm" });
        const file = new File([blob], "voice-message.webm", { type: "audio/webm" });
        try {
          await API.tgSendFile(c.telegram_id, file, { voice: true });
          await loadAndRenderMessages(c, { silent: true });
        } catch (err) {
          Utils.toast(err.message || "Не удалось отправить голосовое сообщение");
        }
      };
      ccMediaRecorder.start();
      ccRecordStartedAt = Date.now();
      ccShowRecordingUi(true);
      ccRecordTimerHandle = setInterval(ccUpdateRecordTimer, 250);
    } catch (err) {
      Utils.toast("Нет доступа к микрофону");
    }
  }

  function ccStopRecording(cancel) {
    if (!ccMediaRecorder) return;
    if (cancel) ccRecordedChunks = [];
    ccMediaRecorder.stop();
    ccMediaRecorder = null;
    clearInterval(ccRecordTimerHandle);
    ccShowRecordingUi(false);
  }

  function ccUpdateRecordTimer() {
    const el = document.getElementById("ccRecordTimer");
    if (!el) return;
    const sec = Math.floor((Date.now() - ccRecordStartedAt) / 1000);
    el.textContent = `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  }

  function ccShowRecordingUi(active) {
    document.getElementById("ccComposerField").hidden = active;
    document.getElementById("ccBtnEmoji").hidden = active;
    document.getElementById("ccBtnAttach").hidden = active;
    document.getElementById("ccBtnGallery").hidden = active;
    let bar = document.getElementById("ccRecordingBar");
    if (active) {
      if (!bar) {
        bar = document.createElement("div");
        bar.id = "ccRecordingBar";
        bar.className = "recording-bar";
        bar.innerHTML = `<span class="recording-bar__dot"></span><span id="ccRecordTimer">0:00</span><span class="recording-bar__hint">Запись голосового…</span>
          <button type="button" class="btn" id="ccBtnCancelRecord">Отмена</button>`;
        document.getElementById("ccComposerForm").insertBefore(bar, document.getElementById("ccComposerSend"));
        document.getElementById("ccBtnCancelRecord").addEventListener("click", () => ccStopRecording(true));
      }
    } else if (bar) {
      bar.remove();
    }
  }

  function wireDetailEvents(c) {
    const btnAnalyze = document.getElementById("btnAnalyzeContact");
    if (btnAnalyze) btnAnalyze.addEventListener("click", () => runAnalysis(c.id));

    wireDeepReportButtons(c);
    loadSavedDeepReportIfNeeded(c);

    const btnApplySuggested = document.getElementById("btnApplySuggestedStatus");
    if (btnApplySuggested) btnApplySuggested.addEventListener("click", () => applySuggestedStatus(c.id));

    const btnCopyReply = document.getElementById("btnCopySuggestedReply");
    if (btnCopyReply) {
      btnCopyReply.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(c.suggested_reply || "");
          Utils.toast("Черновик скопирован");
        } catch (err) {
          Utils.toast("Не удалось скопировать");
        }
      });
    }

    const interestRange = document.getElementById("f-interest");
    interestRange.addEventListener("input", () => {
      document.getElementById("f-interest-val").textContent = interestRange.value;
      const badge = interestBadge(Number(interestRange.value));
      const badgeEl = document.getElementById("f-interest-badge");
      badgeEl.textContent = badge.label;
      badgeEl.className = `interest-badge interest-badge--${badge.cls}`;
    });

    document.getElementById("btnSaveContact").addEventListener("click", async () => {
      const payload = {
        name: document.getElementById("f-name").value.trim(),
        username: cleanUsername(document.getElementById("f-username").value),
        source: document.getElementById("f-source").value || null,
        status: document.getElementById("f-status").value,
        interest_level: Number(interestRange.value),
        next_task: document.getElementById("f-next-task").value || null,
        notes: document.getElementById("f-notes").value || null,
      };
      await API.updateContact(c.id, payload);
      Utils.toast("Изменения сохранены");
      await reload();
      await Dashboard.render();
      const fresh = await API.getContact(c.id);
      renderDetail(fresh);
    });

    document.getElementById("btnBackToContactList").addEventListener("click", backToContactList);

    document.getElementById("btnDeleteContact").addEventListener("click", async () => {
      if (!confirm(`Удалить контакт «${c.name}»? Это действие необратимо.`)) return;
      stopChatPolling();
      await API.deleteContact(c.id);
      activeId = null;
      document.getElementById("contactsBodyTable").classList.remove("has-open-detail");
      document.getElementById("codetailBody").hidden = true;
      document.getElementById("codetailEmpty").hidden = false;
      Utils.toast("Контакт удалён");
      await reload();
      await Dashboard.render();
    });

    document.getElementById("btnAddNote").addEventListener("click", async () => {
      const text = document.getElementById("newNoteText").value.trim();
      if (!text) return;
      const type = document.getElementById("newNoteType").value;
      await API.addInteraction(c.id, { note: text, event_type: type });
      Utils.toast("Запись добавлена");
      const fresh = await API.getContact(c.id);
      renderDetail(fresh);
      await reload();
      await Dashboard.render();
    });

    document.getElementById("timeline").addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-del-interaction]");
      if (!btn) return;
      await API.deleteInteraction(c.id, Number(btn.dataset.delInteraction));
      const fresh = await API.getContact(c.id);
      renderDetail(fresh);
    });

    // ---- tags: click a fixed chip to toggle it on/off, saves immediately ----
    document.getElementById("tagToggleRow").addEventListener("click", async (e) => {
      const btn = e.target.closest(".tag-toggle");
      if (!btn) return;
      const wasActive = btn.classList.contains("is-active");
      btn.classList.toggle("is-active");
      btn.disabled = true;
      const activeTags = [...document.querySelectorAll("#tagToggleRow .tag-toggle.is-active")].map((b) => b.dataset.tag);
      try {
        await API.updateContact(c.id, { tags: activeTags });
        c.tags = activeTags.map((name) => ({ id: 0, name }));
        await reload();
      } catch (err) {
        btn.classList.toggle("is-active", wasActive);
        Utils.toast(err.message || "Не удалось обновить тег");
      } finally {
        btn.disabled = false;
      }
    });

    // ---- side panel: tabs ----
    document.querySelectorAll(".co-tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".co-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
        document.getElementById("paneChat").hidden = tab.dataset.tab !== "chat";
        document.getElementById("paneHistory").hidden = tab.dataset.tab !== "history";
        if (tab.dataset.tab === "chat" && c.telegram_id) {
          loadAndRenderMessages(c);
        } else {
          stopChatPolling();
        }
      });
    });

    // ---- side panel: chat ----
    if (c.telegram_id) {
      loadAndRenderMessages(c);
      wireChatComposer(c);
      const openInChatBtn = document.getElementById("btnOpenInChat");
      if (openInChatBtn) {
        openInChatBtn.addEventListener("click", () => {
          stopChatPolling();
          App.switchView("chat");
          ChatView.openDialog(c.telegram_id);
        });
      }
    } else {
      const linkBtn = document.getElementById("btnLinkTelegram");
      if (linkBtn) {
        linkBtn.addEventListener("click", async () => {
          linkBtn.disabled = true;
          linkBtn.textContent = "Ищем…";
          try {
            const found = await API.tgResolveUsername(c.username);
            await API.updateContact(c.id, { telegram_id: found.telegram_id });
            Utils.toast("Контакт привязан к Telegram");
            await reload();
            const fresh = await API.getContact(c.id);
            renderDetail(fresh);
          } catch (err) {
            Utils.toast(err.message || "Не удалось привязать контакт");
            linkBtn.disabled = false;
            linkBtn.textContent = `Привязать по ${c.username}`;
          }
        });
      }
    }
  }

  async function goToContact(id) {
    await reload();
    App.switchView("contacts");
    App.setContactsMode("table");
    selectContact(id);
  }

  return {
    init, reload, selectContact, goToContact, stopChatPolling,
    get items() { return items; }, get statuses() { return statuses; },
  };
})();
