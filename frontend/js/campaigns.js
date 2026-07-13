/* Кампании массовых рассылок — раздел КАМПАНИИ СООБЩЕНИЙ ТЗ.
 *
 * Один модуль отвечает за:
 *  - список кампаний карточками со статусом и прогрессом;
 *  - модалку создания/редактирования черновика (папки, фильтры, текст,
 *    переменные шаблона, изображение);
 *  - предпросмотр перед запуском (раздел ПРЕДПРОСМОТР);
 *  - запуск/паузу/продолжение и лёгкий поллинг прогресса, пока есть
 *    хотя бы одна RUNNING-кампания и открыт этот вид;
 *  - журнал выполнения кампании (раздел ЖУРНАЛ).
 */
const Campaigns = (() => {
  let campaigns = [];
  let pollTimer = null;
  let folders = [];
  let statuses = [];
  let tags = [];
  let campSelectedMedia = null; // {id, original_name, kind} — выбранное в медиатеке вложение для формы черновика
  let campHasLegacyImage = false; // вложение осталось от кампаний, созданных до появления медиатеки (только image_path, без media)

  const STATUS_LABELS = {
    draft: "Черновик",
    ready: "Готова к запуску",
    running: "Выполняется",
    paused: "Приостановлена",
    completed: "Завершена",
    completed_with_errors: "Завершена с ошибками",
  };

  // Быстрые шаблоны — предзаполняют модалку создания, чтобы пустой экран
  // не был тупиком, а сразу показывал типовые сценарии рассылок.
  const CAMPAIGN_TEMPLATES = [
    { id: "welcome",   title: "Приветствие новых контактов", desc: "Первое сообщение после знакомства",
      name: "Приветствие новых контактов", text: "Привет, {name}! Рад знакомству 🙂" },
    { id: "followup",  title: "Повторное касание", desc: "Напомнить о себе тем, кто давно не отвечал",
      name: "Повторное касание", text: "Привет, {name}! Давно не общались — как дела?" },
    { id: "broadcast", title: "Массовая рассылка", desc: "Одно сообщение всей выбранной аудитории",
      name: "Массовая рассылка", text: "Здравствуйте, {name}! У нас важная новость." },
    { id: "reminder",  title: "Напоминание клиентам", desc: "Мягкий пинг по назначенной встрече/договорённости",
      name: "Напоминание клиентам", text: "{name}, напоминаю о нашей договорённости — удобно сегодня продолжить?" },
  ];

  // Вкладки фильтра списка кампаний (не путать с фильтрами получателей внутри кампании)
  const LIST_FILTERS = [
    { id: "all",       label: "Все",          match: () => true },
    { id: "active",    label: "Активные",     match: (c) => c.status === "running" },
    { id: "planned",   label: "Запланированные", match: (c) => c.status === "draft" || c.status === "ready" },
    { id: "paused",    label: "Остановленные",   match: (c) => c.status === "paused" },
    { id: "completed", label: "Завершённые",     match: (c) => c.status === "completed" },
    { id: "errors",    label: "С ошибками",      match: (c) => c.status === "completed_with_errors" },
  ];
  let activeListFilter = "all";

  function $(id) { return document.getElementById(id); }

  async function render() {
    try {
      [campaigns, folders] = await Promise.all([API.listCampaigns(), API.listFolders()]);
    } catch (err) {
      campaigns = []; folders = [];
      Utils.toast(err.message || "Не удалось загрузить кампании");
    }
    renderList();
    wireOnce();
    startPollingIfNeeded();
  }

  function renderList() {
    const list = $("campaignList");
    if (!list) return;

    if (!campaigns.length) {
      list.innerHTML = `
        <div class="campaign-empty">
          <div class="campaign-empty__art" aria-hidden="true">
            <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path d="M3 11v2a2 2 0 0 0 2 2h1l3.5 4.5V6.5L6 11H5a2 2 0 0 0-2 2Z"/>
              <path d="M14 8.5a5 5 0 0 1 0 7"/><path d="M17.3 5.5a9 9 0 0 1 0 13"/>
            </svg>
          </div>
          <h3>Кампаний пока нет</h3>
          <p>Кампания — это массовая рассылка сообщений выбранной аудитории контактов, с фильтрами получателей и журналом отправки. Начните с шаблона или создайте кампанию с нуля.</p>
          <button class="btn btn--primary" id="campaignEmptyCreateBtn">+ Создать первую кампанию</button>
          <div class="campaign-templates">
            ${CAMPAIGN_TEMPLATES.map((t) => `
              <button type="button" class="campaign-template-card" data-template="${t.id}">
                <span class="campaign-template-card__title">${Utils.escapeHtml(t.title)}</span>
                <span class="campaign-template-card__desc">${Utils.escapeHtml(t.desc)}</span>
              </button>`).join("")}
          </div>
        </div>`;
      const createBtn = $("campaignEmptyCreateBtn");
      if (createBtn) createBtn.addEventListener("click", () => openEditModal(null));
      list.querySelectorAll(".campaign-template-card").forEach((btn) => {
        btn.addEventListener("click", () => {
          const tpl = CAMPAIGN_TEMPLATES.find((t) => t.id === btn.dataset.template);
          openEditModal(null, tpl);
        });
      });
      return;
    }

    const filtered = campaigns.filter(LIST_FILTERS.find((f) => f.id === activeListFilter).match);

    list.innerHTML = `
      <div class="campaign-filter-tabs" id="campaignFilterTabs">
        ${LIST_FILTERS.map((f) => {
          const count = campaigns.filter(f.match).length;
          return `<button type="button" class="campaign-filter-tab ${f.id === activeListFilter ? "is-active" : ""}" data-filter="${f.id}">${f.label} <span>${count}</span></button>`;
        }).join("")}
      </div>
      <div class="campaign-list__grid">
        ${filtered.length ? filtered.map(campaignCardHTML).join("") : `<div class="empty-col">Нет кампаний с этим статусом</div>`}
      </div>`;

    $("campaignFilterTabs").querySelectorAll(".campaign-filter-tab").forEach((btn) => {
      btn.addEventListener("click", () => { activeListFilter = btn.dataset.filter; renderList(); });
    });

    list.querySelectorAll(".campaign-card").forEach((card) => {
      const id = Number(card.dataset.campaignId);
      card.querySelectorAll("[data-action]").forEach((btn) => {
        btn.addEventListener("click", () => handleAction(btn.dataset.action, id));
      });
    });
  }

  function campaignCardHTML(c) {
      const pct = c.total_selected ? Math.round((c.processed_count / c.total_selected) * 100) : 0;
      return `
      <div class="panel campaign-card" data-campaign-id="${c.id}">
        <div class="campaign-card__head">
          <h3>${Utils.escapeHtml(c.name)}</h3>
          <span class="badge status-camp-${c.status}"><span class="dot"></span>${STATUS_LABELS[c.status] || c.status}</span>
        </div>
        <div class="campaign-card__meta">
          <span>Создана: ${new Date(c.created_at).toLocaleString("ru-RU")}</span>
          ${c.started_at ? `<span>Запущена: ${new Date(c.started_at).toLocaleString("ru-RU")}</span>` : ""}
        </div>
        ${c.total_selected ? `
          <div class="campaign-card__progress">
            <div class="campaign-card__bar"><div style="width:${pct}%"></div></div>
            <span>${c.processed_count}/${c.total_selected} · ✓ ${c.completed_count} · ✗ ${c.error_count}</span>
          </div>` : `<div class="campaign-card__meta"><span>Получателей ещё не посчитано</span></div>`}
        <div class="campaign-card__actions">
          ${c.status === "draft" || c.status === "ready" ? `
            <button class="btn" data-action="edit">Изменить</button>
            <button class="btn" data-action="preview">Предпросмотр</button>
            <button class="btn btn--primary" data-action="start">Запустить</button>
            <button class="btn btn--icon" data-action="delete" title="Удалить">🗑</button>` : ""}
          ${c.status === "running" ? `<button class="btn" data-action="pause">Пауза</button>` : ""}
          ${c.status === "paused" ? `
            <button class="btn btn--primary" data-action="resume">Продолжить</button>
            <button class="btn btn--icon" data-action="delete" title="Удалить">🗑</button>` : ""}
          ${["completed", "completed_with_errors"].includes(c.status) ? `
            <button class="btn btn--icon" data-action="delete" title="Удалить">🗑</button>` : ""}
          <button class="btn" data-action="logs">Журнал</button>
        </div>
      </div>`;
  }

  async function handleAction(action, id) {
    const campaign = campaigns.find((c) => c.id === id);
    try {
      if (action === "edit") return openEditModal(campaign);
      if (action === "preview") return openPreviewModal(id);
      if (action === "start") return openPreviewModal(id, true);
      if (action === "pause") { await API.pauseCampaign(id); return render(); }
      if (action === "resume") { await API.resumeCampaign(id); return render(); }
      if (action === "logs") return openLogsModal(id);
      if (action === "delete") {
        if (!confirm(`Удалить кампанию «${campaign.name}»?`)) return;
        await API.deleteCampaign(id);
        return render();
      }
    } catch (err) {
      Utils.toast(err.message || "Не удалось выполнить действие");
    }
  }

  function startPollingIfNeeded() {
    stopPolling();
    if (campaigns.some((c) => c.status === "running")) {
      pollTimer = setInterval(async () => {
        try { campaigns = await API.listCampaigns(); } catch (_) { return; }
        renderList();
        if (!campaigns.some((c) => c.status === "running")) stopPolling();
      }, 4000);
    }
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  function wireOnce() {
    const btn = $("btnNewCampaign");
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      btn.addEventListener("click", () => openEditModal(null));
    }
  }

  // ---- create / edit modal ------------------------------------------

  async function ensureFilterOptions() {
    if (!statuses.length) { try { statuses = await API.getStatuses(); } catch (_) { statuses = []; } }
    if (!tags.length) { try { tags = await API.getTags(); } catch (_) { tags = []; } }
  }

  function ensureEditModal() {
    if ($("campaignEditModal")) return;
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="modal-overlay" id="campaignEditModal" hidden>
        <div class="modal modal--wide">
          <div class="modal__head">
            <h3 id="campaignEditTitle">Новая кампания</h3>
            <button class="modal__close" data-close-modal>&times;</button>
          </div>
          <div class="form">
            <label>Название кампании<input type="text" id="campNameInput" maxlength="150" placeholder="Например: Напоминание о встрече"></label>

            <label>Папки (сегменты)
              <div class="campaign-folder-picker" id="campFolderPicker"></div>
            </label>

            <label>Текст сообщения
              <textarea id="campTextInput" rows="5" maxlength="4000" placeholder="Привет, {name}! 😊"></textarea>
            </label>
            <div class="campaign-vars">
              Переменные:
              <button type="button" class="campaign-var-btn" data-var="name">{name}</button>
              <button type="button" class="campaign-var-btn" data-var="username">{username}</button>
              <button type="button" class="campaign-var-btn" data-var="first_name">{first_name}</button>
            </div>

            <label>Изображение / видео (необязательно)
              <input type="file" id="campImageInput" accept="image/*,video/*">
            </label>
            <div class="campaign-image-row">
              <button type="button" class="btn" id="campPickMediaBtn">🖼 Выбрать из медиатеки</button>
              <button type="button" class="btn btn--icon" id="campClearMediaBtn" title="Убрать вложение" hidden>✕</button>
            </div>
            <div id="campImageStatus" class="campaign-image-status"></div>

            <fieldset class="campaign-filters">
              <legend>Фильтры получателей</legend>
              <label class="campaign-check"><input type="checkbox" id="campFilterActive"> Только активные диалоги</label>
              <label class="campaign-check"><input type="checkbox" id="campFilterArchive"> Исключить архив</label>
              <label class="campaign-check">Не отвечал
                <input type="number" id="campFilterDays" min="0" style="width:64px" placeholder="—"> дней
              </label>
              <label class="campaign-check">Стадия CRM
                <select id="campFilterStage"><option value="">Любая</option></select>
              </label>
              <label class="campaign-check">Теги
                <select id="campFilterTags" multiple size="3"></select>
              </label>
            </fieldset>

            <div class="form__actions">
              <button type="button" class="btn" data-close-modal>Отмена</button>
              <button type="button" class="btn btn--primary" id="campSaveBtn">Сохранить черновик</button>
            </div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(div.firstElementChild);
    $("campaignEditModal").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("campaignEditModal")) {
        $("campaignEditModal").hidden = true;
      }
    });
    $("campTextInput").addEventListener("input", () => {}); // no-op, для будущего live-счётчика символов
    document.body.querySelectorAll(".campaign-var-btn").forEach(() => {}); // делегирование ниже, в openEditModal
  }

  function renderImageStatus() {
    const status = $("campImageStatus");
    const clearBtn = $("campClearMediaBtn");
    if (campSelectedMedia) {
      const kindLabel = { photo: "фото", video: "видео", gif: "GIF", document: "документ" }[campSelectedMedia.kind] || campSelectedMedia.kind;
      status.textContent = `Вложение: ${campSelectedMedia.original_name} (${kindLabel})`;
      clearBtn.hidden = false;
    } else if (campHasLegacyImage) {
      status.textContent = "Вложение прикреплено";
      clearBtn.hidden = false;
    } else {
      status.textContent = "";
      clearBtn.hidden = true;
    }
  }

  async function openEditModal(campaign, template) {
    ensureEditModal();
    await ensureFilterOptions();
    const modal = $("campaignEditModal");
    $("campaignEditTitle").textContent = campaign ? "Изменить кампанию" : "Новая кампания";
    $("campNameInput").value = campaign ? campaign.name : (template ? template.name : "");
    $("campTextInput").value = campaign ? campaign.message_text : (template ? template.text : "");

    const picker = $("campFolderPicker");
    const selectedFolders = new Set(campaign ? campaign.folder_ids : []);
    picker.innerHTML = folders.length
      ? folders.map((f) => `
          <label class="campaign-check">
            <input type="checkbox" value="${f.id}" ${selectedFolders.has(f.id) ? "checked" : ""}>
            ${f.icon ? Utils.escapeHtml(f.icon) + " " : ""}${Utils.escapeHtml(f.name)} (${f.dialog_count})
          </label>`).join("")
      : `<p class="view__sub">Сначала создайте хотя бы одну папку в разделе «Чат»</p>`;

    const filters = campaign ? campaign.filters : {};
    $("campFilterActive").checked = !!filters.active_only;
    $("campFilterArchive").checked = !!filters.exclude_archived;
    $("campFilterDays").value = filters.not_replied_days ?? "";

    const stageSelect = $("campFilterStage");
    stageSelect.innerHTML = `<option value="">Любая</option>` +
      statuses.map((s) => `<option value="${s.value}" ${filters.crm_stage === s.value ? "selected" : ""}>${s.label}</option>`).join("");

    const tagSelect = $("campFilterTags");
    const selectedTagIds = new Set(filters.tag_ids || []);
    tagSelect.innerHTML = tags.map((t) => `<option value="${t.id}" ${selectedTagIds.has(t.id) ? "selected" : ""}>${Utils.escapeHtml(t.name)}</option>`).join("");

    campSelectedMedia = campaign && campaign.media ? campaign.media : null;
    campHasLegacyImage = !!(campaign && campaign.has_image && !campaign.media);
    $("campImageInput").value = "";
    renderImageStatus();

    $("campPickMediaBtn").onclick = () => {
      MediaLibrary.open({
        title: "Выбрать вложение кампании",
        onSelect: (media) => {
          campSelectedMedia = media;
          $("campImageInput").value = "";
          renderImageStatus();
        },
      });
    };
    $("campClearMediaBtn").onclick = async () => {
      campSelectedMedia = null;
      campHasLegacyImage = false;
      $("campImageInput").value = "";
      renderImageStatus();
      if (campaign && (campaign.media_id || campaign.has_image)) {
        try { await API.removeCampaignImage(campaign.id); } catch (err) { Utils.toast(err.message || "Не удалось убрать вложение"); }
      }
    };
    $("campImageInput").addEventListener("change", () => {
      if ($("campImageInput").files[0]) { campSelectedMedia = null; renderImageStatus(); }
    });

    // делегирование клика по кнопкам переменных — вставляем в текстовое поле
    modal.querySelectorAll(".campaign-var-btn").forEach((btn) => {
      btn.onclick = () => {
        const ta = $("campTextInput");
        const pos = ta.selectionStart ?? ta.value.length;
        ta.value = ta.value.slice(0, pos) + `{${btn.dataset.var}}` + ta.value.slice(pos);
        ta.focus();
      };
    });

    const saveBtn = $("campSaveBtn");
    const newSaveBtn = saveBtn.cloneNode(true);
    saveBtn.replaceWith(newSaveBtn);
    newSaveBtn.addEventListener("click", async () => {
      const name = $("campNameInput").value.trim();
      if (!name) { Utils.toast("Введите название кампании"); return; }
      const folderIds = [...picker.querySelectorAll("input[type=checkbox]:checked")].map((i) => Number(i.value));
      const payload = {
        name,
        message_text: $("campTextInput").value,
        folder_ids: folderIds,
        filters: {
          active_only: $("campFilterActive").checked,
          exclude_archived: $("campFilterArchive").checked,
          exclude_deleted: false,
          not_replied_days: $("campFilterDays").value ? Number($("campFilterDays").value) : null,
          crm_stage: stageSelect.value || null,
          tag_ids: [...tagSelect.selectedOptions].map((o) => Number(o.value)),
        },
      };
      try {
        let saved;
        if (campaign) saved = await API.updateCampaign(campaign.id, payload);
        else saved = await API.createCampaign(payload);

        const file = $("campImageInput").files[0];
        if (file) {
          await API.uploadCampaignImage(saved.id, file);
        } else if (campSelectedMedia) {
          await API.attachCampaignMedia(saved.id, campSelectedMedia.id);
        }

        modal.hidden = true;
        await render();
      } catch (err) {
        Utils.toast(err.message || "Не удалось сохранить кампанию");
      }
    });

    modal.hidden = false;
  }

  // ---- preview modal ---------------------------------------------------

  function ensurePreviewModal() {
    if ($("campaignPreviewModal")) return;
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="modal-overlay" id="campaignPreviewModal" hidden>
        <div class="modal">
          <div class="modal__head">
            <h3>Предпросмотр рассылки</h3>
            <button class="modal__close" data-close-modal>&times;</button>
          </div>
          <div id="campPreviewBody" class="campaign-preview-body"></div>
          <div class="form__actions">
            <button type="button" class="btn" data-close-modal>Закрыть</button>
            <button type="button" class="btn btn--primary" id="campConfirmStartBtn" hidden>Подтвердить и запустить</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(div.firstElementChild);
    $("campaignPreviewModal").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("campaignPreviewModal")) {
        $("campaignPreviewModal").hidden = true;
      }
    });
  }

  const EXCLUDE_REASON_LABELS = {
    not_active: "нет синхронизированной переписки",
    not_replied_days: "недостаточно дней без ответа",
    exclude_archived: "в архиве",
    crm_stage: "другая стадия CRM",
    tag_ids: "нет нужного тега",
  };

  async function openPreviewModal(id, forStart = false) {
    ensurePreviewModal();
    const modal = $("campaignPreviewModal");
    const body = $("campPreviewBody");
    body.innerHTML = `<p class="view__sub">Считаем получателей…</p>`;
    modal.hidden = false;
    let preview;
    try {
      preview = await API.previewCampaign(id);
    } catch (err) {
      body.innerHTML = `<p class="view__sub">${Utils.escapeHtml(err.message || "Не удалось построить предпросмотр")}</p>`;
      return;
    }
    const reasons = Object.entries(preview.excluded_reasons)
      .map(([k, v]) => `<li>${EXCLUDE_REASON_LABELS[k] || k}: ${v}</li>`).join("");
    let mediaUsageHtml = "";
    if (preview.media) {
      const sentCount = preview.media_usage.filter((u) => u.sent).length;
      const rows = preview.media_usage.slice(0, 30).map((u) => `
        <tr>
          <td>${u.telegram_id}</td>
          <td>${u.sent ? "✔ Уже отправлялось" : "Никогда не отправлялось"}</td>
          <td>${u.last_sent_at ? new Date(u.last_sent_at).toLocaleString("ru-RU") : "—"}</td>
        </tr>`).join("");
      mediaUsageHtml = `
        <p><b>Вложение:</b> ${Utils.escapeHtml(preview.media.original_name)}</p>
        <p><b>Уже получали это вложение:</b> ${sentCount} из ${preview.media_usage.length}</p>
        ${preview.media_usage.length ? `
          <details class="campaign-media-usage">
            <summary>История по получателям (${preview.media_usage.length})</summary>
            <table class="campaign-log-table">
              <thead><tr><th>Получатель</th><th>Статус</th><th>Последняя отправка</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
            ${preview.media_usage.length > 30 ? `<p class="view__sub">Показаны первые 30</p>` : ""}
          </details>` : ""}
      `;
    }
    body.innerHTML = `
      <p><b>Сегментов выбрано:</b> ${preview.folder_ids.length}</p>
      <p><b>Диалогов в сегментах:</b> ${preview.total_dialogs_in_segments}</p>
      <p><b>Пройдёт фильтры:</b> ${preview.total_after_filters}</p>
      <p><b>Исключено:</b> ${preview.excluded_count}${reasons ? `<ul>${reasons}</ul>` : ""}</p>
      <p><b>Изображение:</b> ${preview.has_image ? "есть" : "нет"}</p>
      ${mediaUsageHtml}
      <p><b>Текст:</b></p>
      <pre class="campaign-preview-text">${Utils.escapeHtml(preview.message_text || "")}</pre>
      ${preview.total_after_filters === 0 ? `<p class="view__sub">После фильтрации получателей не осталось — запуск невозможен.</p>` : ""}
    `;
    const confirmBtn = $("campConfirmStartBtn");
    confirmBtn.hidden = !forStart || preview.total_after_filters === 0;
    const newBtn = confirmBtn.cloneNode(true);
    confirmBtn.replaceWith(newBtn);
    if (forStart && preview.total_after_filters > 0) {
      newBtn.hidden = false;
      newBtn.addEventListener("click", async () => {
        try {
          await API.startCampaign(id);
          modal.hidden = true;
          await render();
        } catch (err) {
          Utils.toast(err.message || "Не удалось запустить кампанию");
        }
      });
    }
  }

  // ---- journal modal ----------------------------------------------------

  function ensureLogsModal() {
    if ($("campaignLogsModal")) return;
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="modal-overlay" id="campaignLogsModal" hidden>
        <div class="modal modal--wide">
          <div class="modal__head">
            <h3>Журнал кампании</h3>
            <button class="modal__close" data-close-modal>&times;</button>
          </div>
          <div id="campLogsBody" class="campaign-logs-body"></div>
        </div>
      </div>`;
    document.body.appendChild(div.firstElementChild);
    $("campaignLogsModal").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("campaignLogsModal")) {
        $("campaignLogsModal").hidden = true;
      }
    });
  }

  async function openLogsModal(id) {
    ensureLogsModal();
    const modal = $("campaignLogsModal");
    const body = $("campLogsBody");
    body.innerHTML = `<p class="view__sub">Загрузка…</p>`;
    modal.hidden = false;
    let logs;
    try {
      logs = await API.getCampaignLogs(id);
    } catch (err) {
      body.innerHTML = `<p class="view__sub">${Utils.escapeHtml(err.message || "Не удалось загрузить журнал")}</p>`;
      return;
    }
    if (!logs.length) {
      body.innerHTML = `<p class="view__sub">Пока нет обработанных диалогов</p>`;
      return;
    }
    body.innerHTML = `
      <table class="campaign-log-table">
        <thead><tr><th>Время</th><th>Получатель</th><th>Результат</th><th>Ошибка</th></tr></thead>
        <tbody>
          ${logs.map((l) => `
            <tr>
              <td>${new Date(l.processed_at).toLocaleString("ru-RU")}</td>
              <td>${l.telegram_id}</td>
              <td>${l.result === "sent" ? "✓ отправлено" : l.result === "error" ? "✗ ошибка" : "пропущено"}</td>
              <td>${l.error_text ? Utils.escapeHtml(l.error_text) : ""}</td>
            </tr>`).join("")}
        </tbody>
      </table>`;
  }

  return { render, stopPolling };
})();
