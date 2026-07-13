/* Папки (сегменты) диалогов — панель над списком чатов в мессенджере.
 *
 * Отвечает за:
 *  - рендер строки папок с иконкой/цветом/счётчиком поверх списка диалогов;
 *  - создание / переименование / смену цвета и иконки / удаление папки;
 *  - смену порядка (стрелками вверх/вниз в модалке управления папками);
 *  - перенос одного диалога в папку (кнопка "⋯" на элементе списка,
 *    событие "chatlist:move-request" из chatview.js).
 *
 * Сознательно не тянет drag-and-drop — для CRM с несколькими десятками
 * папок хватает стрелок в модалке, а простая реализация меньше ломается.
 */
const Folders = (() => {
  let folders = [];
  let activeFolderId = null; // null => один из статичных табов (Все/Непрочитанные/Избранное)

  const PALETTE = ["#6C8EF5", "#F5766C", "#F5B96C", "#6CF5A6", "#6CD9F5", "#B36CF5", "#F56CC7", "#9AA5B1"];

  function $(id) { return document.getElementById(id); }

  async function load() {
    try {
      folders = await API.listFolders();
    } catch (err) {
      folders = [];
    }
    renderBar();
  }

  function renderBar() {
    const bar = $("chatFolders");
    if (!bar) return;
    bar.innerHTML = folders.map((f) => `
      <button class="folderpill ${f.id === activeFolderId ? "is-active" : ""}" data-folder-id="${f.id}"
        style="--folder-color:${Utils.escapeHtml(f.color)}">
        ${f.icon ? `<span class="folderpill__icon">${Utils.escapeHtml(f.icon)}</span>` : ""}
        <span class="folderpill__name">${Utils.escapeHtml(f.name)}</span>
        <span class="folderpill__count">${f.dialog_count}</span>
      </button>`).join("")
      + `<button class="folderpill folderpill--manage" id="folderManageBtn" title="Управление папками">⚙</button>`
      + `<button class="folderpill folderpill--add" id="folderAddBtn" title="Новая папка">＋</button>`;

    bar.querySelectorAll(".folderpill[data-folder-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = Number(btn.dataset.folderId);
        const turningOn = activeFolderId !== id;
        activeFolderId = turningOn ? id : null;
        bar.querySelectorAll(".folderpill[data-folder-id]").forEach((b) => b.classList.toggle("is-active", Number(b.dataset.folderId) === activeFolderId));
        if (activeFolderId === null) {
          document.querySelector('.chatlist__tab[data-chatfilter="all"]')?.classList.add("is-active");
          document.querySelectorAll('.chatlist__tab').forEach((b) => { if (b.dataset.chatfilter !== "all") b.classList.remove("is-active"); });
          ChatView.setListFilter("all");
        } else {
          document.querySelectorAll(".chatlist__tab").forEach((b) => b.classList.remove("is-active"));
          ChatView.setListFilter(`folder:${activeFolderId}`);
        }
      });
    });
    $("folderAddBtn")?.addEventListener("click", () => openEditModal(null));
    $("folderManageBtn")?.addEventListener("click", openManageModal);
  }

  // ---- create / edit modal ----------------------------------------
  function ensureEditModal() {
    if ($("folderEditModal")) return;
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="modal-overlay" id="folderEditModal" hidden>
        <div class="modal">
          <div class="modal__head">
            <h3 id="folderEditTitle">Новая папка</h3>
            <button class="modal__close" data-close-modal>&times;</button>
          </div>
          <div class="form">
            <label>Название<input type="text" id="folderNameInput" maxlength="60" placeholder="Например: Приоритет"></label>
            <label>Иконка (эмодзи)<input type="text" id="folderIconInput" maxlength="4" placeholder="🔥"></label>
            <label>Цвет
              <div class="folder-palette" id="folderPalette"></div>
            </label>
            <div class="form__actions">
              <button type="button" class="btn" data-close-modal>Отмена</button>
              <button type="button" class="btn btn--primary" id="folderSaveBtn">Сохранить</button>
            </div>
          </div>
        </div>
      </div>`;
    document.body.appendChild(div.firstElementChild);
    $("folderEditModal").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("folderEditModal")) {
        $("folderEditModal").hidden = true;
      }
    });
  }

  function openEditModal(folder) {
    ensureEditModal();
    const modal = $("folderEditModal");
    let selectedColor = folder ? folder.color : PALETTE[0];
    $("folderEditTitle").textContent = folder ? "Изменить папку" : "Новая папка";
    $("folderNameInput").value = folder ? folder.name : "";
    $("folderIconInput").value = folder ? (folder.icon || "") : "";
    const palette = $("folderPalette");
    palette.innerHTML = PALETTE.map((c) =>
      `<button type="button" class="folder-swatch ${c === selectedColor ? "is-selected" : ""}" data-color="${c}" style="background:${c}"></button>`
    ).join("");
    palette.querySelectorAll(".folder-swatch").forEach((btn) => {
      btn.addEventListener("click", () => {
        selectedColor = btn.dataset.color;
        palette.querySelectorAll(".folder-swatch").forEach((b) => b.classList.toggle("is-selected", b === btn));
      });
    });

    const saveBtn = $("folderSaveBtn");
    const newSaveBtn = saveBtn.cloneNode(true); // сбрасываем предыдущий обработчик click
    saveBtn.replaceWith(newSaveBtn);
    newSaveBtn.addEventListener("click", async () => {
      const name = $("folderNameInput").value.trim();
      if (!name) { Utils.toast("Введите название папки"); return; }
      const payload = { name, icon: $("folderIconInput").value.trim() || null, color: selectedColor };
      try {
        if (folder) await API.updateFolder(folder.id, payload);
        else await API.createFolder(payload);
        modal.hidden = true;
        await load();
        renderManageList();
      } catch (err) {
        Utils.toast(err.message || "Не удалось сохранить папку");
      }
    });

    modal.hidden = false;
  }

  // ---- manage modal (reorder / delete) -----------------------------
  function ensureManageModal() {
    if ($("folderManageModal")) return;
    const div = document.createElement("div");
    div.innerHTML = `
      <div class="modal-overlay" id="folderManageModal" hidden>
        <div class="modal">
          <div class="modal__head">
            <h3>Управление папками</h3>
            <button class="modal__close" data-close-modal>&times;</button>
          </div>
          <div id="folderManageList" class="folder-manage-list"></div>
          <div class="form__actions">
            <button type="button" class="btn" data-close-modal>Готово</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(div.firstElementChild);
    $("folderManageModal").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("folderManageModal")) {
        $("folderManageModal").hidden = true;
      }
    });
  }

  function renderManageList() {
    const list = $("folderManageList");
    if (!list) return;
    if (!folders.length) {
      list.innerHTML = `<div class="empty-col">Пока нет папок</div>`;
      return;
    }
    list.innerHTML = folders.map((f, i) => `
      <div class="folder-manage-row" data-folder-id="${f.id}">
        <span class="folder-manage-row__icon" style="color:${Utils.escapeHtml(f.color)}">${Utils.escapeHtml(f.icon || "•")}</span>
        <span class="folder-manage-row__name">${Utils.escapeHtml(f.name)}</span>
        <span class="folder-manage-row__count">${f.dialog_count}</span>
        <button class="folder-manage-row__btn" data-move="up" ${i === 0 ? "disabled" : ""} title="Выше">↑</button>
        <button class="folder-manage-row__btn" data-move="down" ${i === folders.length - 1 ? "disabled" : ""} title="Ниже">↓</button>
        <button class="folder-manage-row__btn" data-edit title="Изменить">✎</button>
        <button class="folder-manage-row__btn" data-delete title="Удалить">🗑</button>
      </div>`).join("");

    list.querySelectorAll(".folder-manage-row").forEach((row) => {
      const id = Number(row.dataset.folderId);
      const folder = folders.find((f) => f.id === id);
      row.querySelector('[data-move="up"]')?.addEventListener("click", () => reorder(id, -1));
      row.querySelector('[data-move="down"]')?.addEventListener("click", () => reorder(id, 1));
      row.querySelector("[data-edit]")?.addEventListener("click", () => openEditModal(folder));
      row.querySelector("[data-delete]")?.addEventListener("click", () => removeFolder(folder));
    });
  }

  async function reorder(folderId, delta) {
    const idx = folders.findIndex((f) => f.id === folderId);
    const swapWith = idx + delta;
    if (swapWith < 0 || swapWith >= folders.length) return;
    [folders[idx], folders[swapWith]] = [folders[swapWith], folders[idx]];
    renderManageList();
    renderBar();
    try {
      await API.reorderFolders(folders.map((f) => f.id));
    } catch (err) {
      Utils.toast("Не удалось изменить порядок папок");
      await load();
      renderManageList();
    }
  }

  async function removeFolder(folder) {
    if (!folder) return;
    if (!confirm(`Удалить папку «${folder.name}»? Диалоги останутся, но выпадут из папки.`)) return;
    try {
      await API.deleteFolder(folder.id);
      if (activeFolderId === folder.id) {
        activeFolderId = null;
        ChatView.setListFilter("all");
      }
      await load();
      renderManageList();
    } catch (err) {
      Utils.toast(err.message || "Не удалось удалить папку");
    }
  }

  function openManageModal() {
    ensureManageModal();
    renderManageList();
    $("folderManageModal").hidden = false;
  }

  // ---- move-to-folder popover on a single dialog -------------------
  function ensureMovePopover() {
    if ($("folderMovePopover")) return;
    const div = document.createElement("div");
    div.innerHTML = `<div class="folder-move-popover" id="folderMovePopover" hidden></div>`;
    document.body.appendChild(div.firstElementChild);
  }

  function openMovePopover(telegramId, x, y) {
    ensureMovePopover();
    const pop = $("folderMovePopover");
    const items = folders.map((f) =>
      `<button class="folder-move-popover__item" data-folder-id="${f.id}">
        ${f.icon ? Utils.escapeHtml(f.icon) + " " : ""}${Utils.escapeHtml(f.name)}
      </button>`
    ).join("");
    pop.innerHTML = (folders.length ? items : `<div class="folder-move-popover__empty">Нет папок — создайте в панели выше</div>`)
      + `<button class="folder-move-popover__item folder-move-popover__item--clear" data-folder-id="">Убрать из папки</button>`;
    pop.style.left = `${x}px`;
    pop.style.top = `${y}px`;
    pop.hidden = false;

    pop.querySelectorAll("[data-folder-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const raw = btn.dataset.folderId;
        const folderId = raw === "" ? null : Number(raw);
        pop.hidden = true;
        try {
          await API.assignDialogsToFolder([telegramId], folderId);
          await load();
          await ChatView.refreshDialogs({ silent: true });
        } catch (err) {
          Utils.toast(err.message || "Не удалось перенести диалог");
        }
      });
    });

    const closeOnOutside = (e) => {
      if (!pop.contains(e.target)) {
        pop.hidden = true;
        document.removeEventListener("click", closeOnOutside, true);
      }
    };
    setTimeout(() => document.addEventListener("click", closeOnOutside, true), 0);
  }

  function wire() {
    document.addEventListener("chatlist:move-request", (e) => {
      openMovePopover(e.detail.telegramId, e.detail.x, e.detail.y);
    });
  }

  return { load, wire };
})();
