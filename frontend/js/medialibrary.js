/* ============================================================
   MediaLibrary — единая встроенная медиатека CRM (раздел МОДУЛЬ
   МЕДИАТЕКИ ТЗ).

   Один и тот же модуль (одна модалка, один API) используется и в
   обычном чате ("Галерея в чате"), и в кампаниях ("Галерея в
   кампаниях") — специально, чтобы не плодить две разные реализации
   выбора медиа (см. раздел АРХИТЕКТУРА ТЗ). Вызывающий код просто
   зовёт MediaLibrary.open({...}) и получает выбранный файл через
   колбэк onSelect.

   Раздел СТРУКТУРА МЕДИАТЕКИ / ИНТЕРФЕЙС ТЗ: боковая панель папок
   (создание/переименование/цвет/удаление), перенос файлов между
   папками через drag & drop или массовым выбором, сортировка по
   популярности использования. Папки медиатеки — отдельная сущность
   от папок диалогов (см. folders.js): один файл лежит максимум в
   одной папке за раз, как и диалог в своей папке.
   ============================================================ */
const MediaLibrary = (() => {
  const KIND_ICON = { photo: "🖼", video: "🎬", gif: "GIF", document: "📄" };
  const KIND_LABEL = { photo: "Фото", video: "Видео", gif: "GIF", document: "Документы" };
  const SORTS = [
    { value: "date_desc", label: "Сначала новые" },
    { value: "date_asc", label: "Сначала старые" },
    { value: "name_asc", label: "По имени А→Я" },
    { value: "size_desc", label: "По размеру ↓" },
    { value: "popular_desc", label: "По популярности" },
  ];
  const PALETTE = ["#6C8EF5", "#F5766C", "#F5B96C", "#6CF5A6", "#6CD9F5", "#B36CF5", "#F56CC7", "#9AA5B1"];
  const ALL = "__all__";
  const UNFILED = "__unfiled__";

  let items = [];
  let totalCount = 0;
  let totalSize = 0;
  let search = "";
  let kindFilter = "";
  let sort = "date_desc";
  let searchDebounce = null;

  let folders = [];
  let activeFolder = ALL; // ALL | UNFILED | folder id (number)
  let selected = new Set(); // выбранные media_id — раздел ИНТЕРФЕЙС ТЗ: массовый выбор

  // Контекст текущего открытия: кому будем отправлять / откуда проверять историю.
  let ctx = { dialogId: null, onSelect: null, title: "Медиатека" };
  let usageByMediaId = {}; // media_id -> {sent, last_sent_at} для текущего dialogId

  function $(id) { return document.getElementById(id); }

  function ensureModal() {
    if ($("mediaLibraryModal")) return;
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="modal-overlay" id="mediaLibraryModal" hidden>
        <div class="modal modal--wide medialib">
          <div class="modal__head">
            <h3 id="medialibTitle">Медиатека</h3>
            <button class="modal__close" data-close-modal>&times;</button>
          </div>
          <div class="medialib__body">
            <div class="medialib__sidebar" id="medialibSidebar"></div>
            <div class="medialib__main">
              <div class="medialib__toolbar">
                <input type="text" id="medialibSearch" placeholder="Поиск по названию…" class="medialib__search">
                <select id="medialibKind" class="medialib__select">
                  <option value="">Все типы</option>
                  <option value="photo">📷 Фото</option>
                  <option value="video">🎥 Видео</option>
                  <option value="gif">🖼 GIF</option>
                  <option value="document">📄 Документы</option>
                </select>
                <select id="medialibSort" class="medialib__select">
                  ${SORTS.map((s) => `<option value="${s.value}">${s.label}</option>`).join("")}
                </select>
                <label class="btn btn--primary medialib__upload-btn">
                  ⬆ Загрузить
                  <input type="file" id="medialibUploadInput" multiple hidden>
                </label>
              </div>
              <div class="medialib__bulkbar" id="medialibBulkbar" hidden></div>
              <div class="medialib__grid" id="medialibGrid"></div>
              <div class="medialib__footer">
                <span id="medialibStats"></span>
              </div>
            </div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(div.firstElementChild);

    $("mediaLibraryModal").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("mediaLibraryModal")) {
        close();
      }
    });

    $("medialibSearch").addEventListener("input", (e) => {
      clearTimeout(searchDebounce);
      const value = e.target.value;
      searchDebounce = setTimeout(() => { search = value; load(); }, 300);
    });
    $("medialibKind").addEventListener("change", (e) => { kindFilter = e.target.value; load(); });
    $("medialibSort").addEventListener("change", (e) => { sort = e.target.value; load(); });
    $("medialibUploadInput").addEventListener("change", async (e) => {
      const files = e.target.files;
      if (!files || !files.length) return;
      await upload(files);
      e.target.value = "";
    });
  }

  async function open(options) {
    ctx = {
      dialogId: options.dialogId || null,
      onSelect: options.onSelect || null,
      title: options.title || "Медиатека",
    };
    ensureModal();
    $("medialibTitle").textContent = ctx.title;
    $("mediaLibraryModal").hidden = false;
    $("medialibSearch").value = "";
    search = ""; kindFilter = ""; sort = "date_desc"; activeFolder = ALL;
    selected = new Set();
    $("medialibKind").value = ""; $("medialibSort").value = "date_desc";
    await loadFolders();
    await load();
  }

  function close() {
    const modal = $("mediaLibraryModal");
    if (modal) modal.hidden = true;
  }

  // ---- папки медиатеки: боковая панель --------------------------

  async function loadFolders() {
    try {
      folders = await API.listMediaFolders();
    } catch (_) {
      folders = [];
    }
    renderSidebar();
  }

  function currentFolderParams() {
    if (activeFolder === ALL) return {};
    if (activeFolder === UNFILED) return { unfiled: true };
    return { folder_id: activeFolder };
  }

  function renderSidebar() {
    const bar = $("medialibSidebar");
    if (!bar) return;
    const rows = folders.map((f) => `
      <div class="medialib-folder ${activeFolder === f.id ? "is-active" : ""}" data-folder-id="${f.id}">
        <button type="button" class="medialib-folder__btn" data-select-folder="${f.id}" style="--folder-color:${Utils.escapeHtml(f.color)}">
          <span class="medialib-folder__icon">${f.icon ? Utils.escapeHtml(f.icon) : "🗂"}</span>
          <span class="medialib-folder__name" title="${Utils.escapeHtml(f.name)}">${Utils.escapeHtml(f.name)}</span>
          <span class="medialib-folder__count">${f.file_count}</span>
        </button>
        <button type="button" class="medialib-folder__edit" data-edit-folder="${f.id}" title="Изменить папку">✎</button>
      </div>`).join("");

    bar.innerHTML = `
      <button type="button" class="medialib-folder medialib-folder--special ${activeFolder === ALL ? "is-active" : ""}" data-select-folder="${ALL}">
        <span class="medialib-folder__icon">🗂</span><span class="medialib-folder__name">Все файлы</span>
        <span class="medialib-folder__count">${totalCount}</span>
      </button>
      <button type="button" class="medialib-folder medialib-folder--special ${activeFolder === UNFILED ? "is-active" : ""}" data-select-folder="${UNFILED}">
        <span class="medialib-folder__icon">📥</span><span class="medialib-folder__name">Без папки</span>
      </button>
      <div class="medialib-sidebar__list">${rows}</div>
      <button type="button" class="medialib-folder medialib-folder--add" id="medialibAddFolder">＋ Новая папка</button>`;

    bar.querySelectorAll("[data-select-folder]").forEach((el) => {
      el.addEventListener("click", (e) => {
        if (e.target.closest("[data-edit-folder]")) return;
        const raw = el.dataset.selectFolder;
        activeFolder = (raw === ALL || raw === UNFILED) ? raw : Number(raw);
        renderSidebar();
        load();
      });
    });
    bar.querySelectorAll("[data-edit-folder]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const folder = folders.find((f) => f.id === Number(btn.dataset.editFolder));
        openFolderEditModal(folder);
      });
    });
    $("medialibAddFolder")?.addEventListener("click", () => openFolderEditModal(null));

    // drag & drop: перенос файла(ов) на папку в боковой панели
    bar.querySelectorAll(".medialib-folder[data-folder-id]").forEach((row) => {
      const folderId = Number(row.dataset.folderId);
      row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("is-dragover"); });
      row.addEventListener("dragleave", () => row.classList.remove("is-dragover"));
      row.addEventListener("drop", async (e) => {
        e.preventDefault();
        row.classList.remove("is-dragover");
        const ids = readDragIds(e);
        if (!ids.length) return;
        await moveToFolder(ids, folderId);
      });
    });
    const unfiledBtn = bar.querySelector('[data-select-folder="' + UNFILED + '"]');
    if (unfiledBtn) {
      unfiledBtn.addEventListener("dragover", (e) => { e.preventDefault(); unfiledBtn.classList.add("is-dragover"); });
      unfiledBtn.addEventListener("dragleave", () => unfiledBtn.classList.remove("is-dragover"));
      unfiledBtn.addEventListener("drop", async (e) => {
        e.preventDefault();
        unfiledBtn.classList.remove("is-dragover");
        const ids = readDragIds(e);
        if (!ids.length) return;
        await moveToFolder(ids, null);
      });
    }
  }

  function readDragIds(e) {
    try {
      const raw = e.dataTransfer.getData("application/x-media-ids");
      const ids = JSON.parse(raw || "[]");
      return Array.isArray(ids) ? ids : [];
    } catch (_) {
      return [];
    }
  }

  async function moveToFolder(mediaIds, folderId) {
    try {
      await API.moveMedia(mediaIds, folderId);
      selected = new Set();
      await loadFolders();
      await load();
    } catch (err) {
      Utils.toast(err.message || "Не удалось перенести файл(ы)");
    }
  }

  // ---- create / edit папки медиатеки -----------------------------

  function ensureFolderEditModal() {
    if ($("medialibFolderEditModal")) return;
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="modal-overlay" id="medialibFolderEditModal" hidden>
        <div class="modal">
          <div class="modal__head">
            <h3 id="medialibFolderEditTitle">Новая папка</h3>
            <button class="modal__close" data-close-modal>&times;</button>
          </div>
          <div class="form">
            <label>Название<input type="text" id="medialibFolderNameInput" maxlength="60" placeholder="Например: Избранное"></label>
            <label>Иконка (эмодзи)<input type="text" id="medialibFolderIconInput" maxlength="4" placeholder="🔥"></label>
            <label>Цвет
              <div class="folder-palette" id="medialibFolderPalette"></div>
            </label>
            <div class="form__actions form__actions--spread">
              <button type="button" class="btn btn--danger" id="medialibFolderDeleteBtn" hidden>Удалить папку</button>
              <div style="flex:1"></div>
              <button type="button" class="btn" data-close-modal>Отмена</button>
              <button type="button" class="btn btn--primary" id="medialibFolderSaveBtn">Сохранить</button>
            </div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(div.firstElementChild);
    $("medialibFolderEditModal").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("medialibFolderEditModal")) {
        $("medialibFolderEditModal").hidden = true;
      }
    });
  }

  function openFolderEditModal(folder) {
    ensureFolderEditModal();
    const modal = $("medialibFolderEditModal");
    let selectedColor = folder ? folder.color : PALETTE[0];
    $("medialibFolderEditTitle").textContent = folder ? "Изменить папку" : "Новая папка";
    $("medialibFolderNameInput").value = folder ? folder.name : "";
    $("medialibFolderIconInput").value = folder ? (folder.icon || "") : "";
    const palette = $("medialibFolderPalette");
    palette.innerHTML = PALETTE.map((c) =>
      `<button type="button" class="folder-swatch ${c === selectedColor ? "is-selected" : ""}" data-color="${c}" style="background:${c}"></button>`
    ).join("");
    palette.querySelectorAll(".folder-swatch").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectedColor = btn.dataset.color;
        palette.querySelectorAll(".folder-swatch").forEach((b) => b.classList.toggle("is-selected", b === btn));
      });
    });

    const delBtn = $("medialibFolderDeleteBtn");
    delBtn.hidden = !folder;
    const newDelBtn = delBtn.cloneNode(true);
    delBtn.replaceWith(newDelBtn);
    if (folder) {
      newDelBtn.addEventListener("click", async () => {
        if (!confirm(`Удалить папку «${folder.name}»? Файлы останутся, но выпадут из папки.`)) return;
        try {
          await API.deleteMediaFolder(folder.id);
          if (activeFolder === folder.id) activeFolder = ALL;
          modal.hidden = true;
          await loadFolders();
          await load();
        } catch (err) {
          Utils.toast(err.message || "Не удалось удалить папку");
        }
      });
    }

    const saveBtn = $("medialibFolderSaveBtn");
    const newSaveBtn = saveBtn.cloneNode(true);
    saveBtn.replaceWith(newSaveBtn);
    newSaveBtn.addEventListener("click", async () => {
      const name = $("medialibFolderNameInput").value.trim();
      if (!name) { Utils.toast("Введите название папки"); return; }
      const payload = { name, icon: $("medialibFolderIconInput").value.trim() || null, color: selectedColor };
      try {
        if (folder) await API.updateMediaFolder(folder.id, payload);
        else await API.createMediaFolder(payload);
        modal.hidden = true;
        await loadFolders();
      } catch (err) {
        Utils.toast(err.message || "Не удалось сохранить папку");
      }
    });

    modal.hidden = false;
  }

  // ---- грид файлов -------------------------------------------------

  async function load() {
    const grid = $("medialibGrid");
    if (!grid) return;
    grid.innerHTML = `<div class="empty-col">Загрузка…</div>`;
    let result;
    try {
      result = await API.listMedia({ search, kind: kindFilter, sort, ...currentFolderParams() });
    } catch (err) {
      grid.innerHTML = `<div class="empty-col">${Utils.escapeHtml(err.message || "Не удалось загрузить медиатеку")}</div>`;
      return;
    }
    items = result.items;
    totalCount = result.total_count;
    totalSize = result.total_size_bytes;

    usageByMediaId = {};
    if (ctx.dialogId && items.length) {
      try {
        const usage = await API.dialogMediaUsage(ctx.dialogId, items.map((m) => m.id));
        usage.forEach((u) => { usageByMediaId[u.media_id] = u; });
      } catch (_) { /* необязательная информация — молча пропускаем при ошибке */ }
    }

    // выделение может ссылаться на файлы, которых больше нет в текущей выборке
    const visibleIds = new Set(items.map((m) => m.id));
    selected = new Set([...selected].filter((id) => visibleIds.has(id)));

    renderGrid();
    renderStats();
    renderBulkbar();
  }

  function fmtDate(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" }) +
      " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function renderStats() {
    $("medialibStats").textContent = `${totalCount} файлов · ${Utils.formatFileSize(totalSize)}`;
  }

  function renderBulkbar() {
    const bar = $("medialibBulkbar");
    if (!bar) return;
    if (!selected.size) { bar.hidden = true; bar.innerHTML = ""; return; }
    bar.hidden = false;
    const folderOptions = folders.map((f) => `<option value="${f.id}">${Utils.escapeHtml(f.icon ? f.icon + " " : "")}${Utils.escapeHtml(f.name)}</option>`).join("");
    bar.innerHTML = `
      <span class="medialib__bulkbar-count">${selected.size} выбрано</span>
      <select id="medialibBulkMoveSelect" class="medialib__select">
        <option value="">Переместить в…</option>
        ${folderOptions}
        <option value="__none__">Убрать из папки</option>
      </select>
      <button type="button" class="btn btn--danger" id="medialibBulkDeleteBtn">Удалить</button>
      <button type="button" class="btn" id="medialibBulkClearBtn">Снять выделение</button>`;

    $("medialibBulkMoveSelect").addEventListener("change", async (e) => {
      const value = e.target.value;
      if (!value) return;
      const folderId = value === "__none__" ? null : Number(value);
      await moveToFolder([...selected], folderId);
    });
    $("medialibBulkDeleteBtn").addEventListener("click", async () => {
      if (!confirm(`Удалить ${selected.size} файл(ов) из медиатеки?`)) return;
      try {
        await API.bulkDeleteMedia([...selected]);
        selected = new Set();
        await loadFolders();
        await load();
      } catch (err) {
        Utils.toast(err.message || "Не удалось удалить файлы");
      }
    });
    $("medialibBulkClearBtn").addEventListener("click", () => {
      selected = new Set();
      renderGrid();
      renderBulkbar();
    });
  }

  function renderGrid() {
    const grid = $("medialibGrid");
    if (!items.length) {
      grid.innerHTML = `<div class="empty-col">Файлов пока нет — загрузите первый</div>`;
      return;
    }
    grid.innerHTML = items.map((m) => {
      const usage = usageByMediaId[m.id];
      let usageBadge = "";
      if (ctx.dialogId) {
        usageBadge = usage && usage.sent
          ? `<span class="medialib-tile__badge medialib-tile__badge--sent" title="Последняя отправка: ${fmtDate(usage.last_sent_at)}">✔ Уже отправлялось</span>`
          : `<span class="medialib-tile__badge medialib-tile__badge--new">Не отправлялось</span>`;
      }
      const thumbHtml = m.thumb_url
        ? `<img src="${m.thumb_url}" loading="lazy" alt="">`
        : `<div class="medialib-tile__icon">${KIND_ICON[m.kind] || "📄"}</div>`;
      const isSelected = selected.has(m.id);
      return `
        <div class="medialib-tile ${isSelected ? "is-selected" : ""}" data-id="${m.id}" data-kind="${m.kind}" draggable="true">
          <div class="medialib-tile__thumb">
            <button type="button" class="medialib-tile__check" data-act="select" title="Выбрать">${isSelected ? "✓" : ""}</button>
            ${thumbHtml}
            ${m.kind === "video" ? `<span class="medialib-tile__video-badge">▶</span>` : ""}
            ${usageBadge}
            <div class="medialib-tile__actions">
              <button type="button" class="medialib-tile__act" data-act="attach" title="Прикрепить">📤</button>
              <button type="button" class="medialib-tile__act" data-act="view" title="Просмотреть">👁</button>
              <button type="button" class="medialib-tile__act" data-act="rename" title="Переименовать">📝</button>
              <button type="button" class="medialib-tile__act" data-act="delete" title="Удалить">🗑</button>
              <button type="button" class="medialib-tile__act" data-act="info" title="Информация">ℹ</button>
            </div>
          </div>
          <div class="medialib-tile__meta">
            <span class="medialib-tile__name" title="${Utils.escapeHtml(m.original_name)}">${Utils.escapeHtml(m.original_name)}</span>
            <span class="medialib-tile__size">${Utils.formatFileSize(m.size_bytes)}</span>
          </div>
        </div>`;
    }).join("");

    grid.querySelectorAll(".medialib-tile").forEach((tile) => {
      const id = Number(tile.dataset.id);
      const media = items.find((m) => m.id === id);

      tile.querySelector('[data-act="select"]').addEventListener("click", (e) => {
        e.stopPropagation();
        toggleSelect(id);
      });
      tile.querySelector('[data-act="attach"]').addEventListener("click", () => selectMedia(media));
      tile.addEventListener("dblclick", () => selectMedia(media));
      tile.querySelector('[data-act="view"]').addEventListener("click", () => {
        MediaMessage.openLightbox(media.url, media.kind === "video" ? "video" : "photo");
      });
      tile.querySelector('[data-act="rename"]').addEventListener("click", () => renameMedia(media));
      tile.querySelector('[data-act="delete"]').addEventListener("click", () => deleteMedia(media));
      tile.querySelector('[data-act="info"]').addEventListener("click", () => showInfo(media));

      // Ctrl/Cmd+клик по превью — быстрое выделение без отправки (раздел ИНТЕРФЕЙС ТЗ: массовый выбор)
      tile.querySelector(".medialib-tile__thumb").addEventListener("click", (e) => {
        if (e.target.closest("[data-act]")) return;
        if (e.ctrlKey || e.metaKey) { toggleSelect(id); return; }
        selectMedia(media);
      });

      // drag & drop: перенос в папку боковой панели
      tile.addEventListener("dragstart", (e) => {
        const ids = selected.has(id) && selected.size > 1 ? [...selected] : [id];
        e.dataTransfer.setData("application/x-media-ids", JSON.stringify(ids));
        e.dataTransfer.effectAllowed = "move";
      });
    });
  }

  function toggleSelect(id) {
    if (selected.has(id)) selected.delete(id);
    else selected.add(id);
    renderGrid();
    renderBulkbar();
  }

  function selectMedia(media) {
    if (ctx.onSelect) ctx.onSelect(media);
    close();
  }

  async function upload(files) {
    const grid = $("medialibGrid");
    const prevHtml = grid.innerHTML;
    grid.innerHTML = `<div class="empty-col">Загрузка файлов…</div>`;
    try {
      const uploaded = await API.uploadMedia(files);
      // Если сейчас открыта конкретная папка — сразу кладём в неё новые
      // файлы, чтобы они не "терялись" в разделе "Все файлы" (раздел
      // СТРУКТУРА МЕДИАТЕКИ ТЗ: организация файлов по папкам).
      if (activeFolder !== ALL && activeFolder !== UNFILED && uploaded.length) {
        await API.moveMedia(uploaded.map((m) => m.id), activeFolder);
      }
      await loadFolders();
      await load();
    } catch (err) {
      Utils.toast(err.message || "Не удалось загрузить файл(ы)");
      grid.innerHTML = prevHtml;
    }
  }

  async function renameMedia(media) {
    const name = prompt("Новое название файла:", media.original_name);
    if (!name || name.trim() === media.original_name) return;
    try {
      await API.renameMedia(media.id, name.trim());
      await load();
    } catch (err) {
      Utils.toast(err.message || "Не удалось переименовать файл");
    }
  }

  async function deleteMedia(media) {
    if (!confirm(`Удалить «${media.original_name}» из медиатеки?`)) return;
    try {
      await API.deleteMedia(media.id);
      await loadFolders();
      await load();
    } catch (err) {
      Utils.toast(err.message || "Не удалось удалить файл");
    }
  }

  function showInfo(media) {
    const folder = folders.find((f) => f.id === media.folder_id);
    alert(
      `${media.original_name}\n\n` +
      `Тип: ${KIND_LABEL[media.kind] || media.kind}\n` +
      `Размер: ${Utils.formatFileSize(media.size_bytes)}\n` +
      (media.width && media.height ? `Разрешение: ${media.width}×${media.height}\n` : "") +
      `Папка: ${folder ? folder.name : "Без папки"}\n` +
      `Загружен: ${fmtDate(media.created_at)}\n` +
      `Отправлен всего раз: ${media.send_count}`
    );
  }

  return { open, close };
})();
