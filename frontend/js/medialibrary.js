/* ============================================================
   MediaLibrary — единая встроенная медиатека CRM (раздел МОДУЛЬ
   МЕДИАТЕКИ ТЗ).

   Один и тот же модуль (одна модалка, один API) используется и в
   обычном чате ("Галерея в чате"), и в кампаниях ("Галерея в
   кампаниях") — специально, чтобы не плодить две разные реализации
   выбора медиа (см. раздел АРХИТЕКТУРА ТЗ). Вызывающий код просто
   зовёт MediaLibrary.open({...}) и получает выбранный файл через
   колбэк onSelect.
   ============================================================ */
const MediaLibrary = (() => {
  const KIND_ICON = { photo: "🖼", video: "🎬", gif: "GIF", document: "📄" };
  const KIND_LABEL = { photo: "Фото", video: "Видео", gif: "GIF", document: "Документы" };
  const SORTS = [
    { value: "date_desc", label: "Сначала новые" },
    { value: "date_asc", label: "Сначала старые" },
    { value: "name_asc", label: "По имени А→Я" },
    { value: "size_desc", label: "По размеру ↓" },
  ];

  let items = [];
  let totalCount = 0;
  let totalSize = 0;
  let search = "";
  let kindFilter = "";
  let sort = "date_desc";
  let searchDebounce = null;

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
          <div class="medialib__grid" id="medialibGrid"></div>
          <div class="medialib__footer">
            <span id="medialibStats"></span>
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
    search = ""; kindFilter = ""; sort = "date_desc";
    $("medialibKind").value = ""; $("medialibSort").value = "date_desc";
    await load();
  }

  function close() {
    const modal = $("mediaLibraryModal");
    if (modal) modal.hidden = true;
  }

  async function load() {
    const grid = $("medialibGrid");
    if (!grid) return;
    grid.innerHTML = `<div class="empty-col">Загрузка…</div>`;
    let result;
    try {
      result = await API.listMedia({ search, kind: kindFilter, sort });
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

    renderGrid();
    renderStats();
  }

  function fmtDate(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" }) +
      " " + d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  }

  function renderStats() {
    $("medialibStats").textContent = `${totalCount} файлов · ${Utils.formatFileSize(totalSize)}`;
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
      return `
        <div class="medialib-tile" data-id="${m.id}" data-kind="${m.kind}">
          <div class="medialib-tile__thumb">
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
      tile.querySelector('[data-act="attach"]').addEventListener("click", () => selectMedia(media));
      tile.addEventListener("dblclick", () => selectMedia(media));
      tile.querySelector('[data-act="view"]').addEventListener("click", () => {
        MediaMessage.openLightbox(media.url, media.kind === "video" ? "video" : "photo");
      });
      tile.querySelector('[data-act="rename"]').addEventListener("click", () => renameMedia(media));
      tile.querySelector('[data-act="delete"]').addEventListener("click", () => deleteMedia(media));
      tile.querySelector('[data-act="info"]').addEventListener("click", () => showInfo(media));
    });
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
      await API.uploadMedia(files);
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
      await load();
    } catch (err) {
      Utils.toast(err.message || "Не удалось удалить файл");
    }
  }

  function showInfo(media) {
    alert(
      `${media.original_name}\n\n` +
      `Тип: ${KIND_LABEL[media.kind] || media.kind}\n` +
      `Размер: ${Utils.formatFileSize(media.size_bytes)}\n` +
      (media.width && media.height ? `Разрешение: ${media.width}×${media.height}\n` : "") +
      `Загружен: ${fmtDate(media.created_at)}\n` +
      `Отправлен всего раз: ${media.send_count}`
    );
  }

  return { open, close };
})();
