/* ============================================================
   ChatView — мессенджер поверх Telegram (Telethon).
   Список диалогов, вложения, ответы, пересылка, редактирование,
   удаление, закрепление и статусы (онлайн / был(а) / печатает…)
   опрашиваются с backend, который сам общается с Telegram.
   ============================================================ */
const ChatView = (() => {

  const EMOJI = ["😀","😁","😂","🤣","😊","😉","😍","😘","😜","🤔","😎","🥳",
    "😢","😭","😡","😱","🤗","🙏","👍","👎","👏","🙌","🤝","💪",
    "❤️","🔥","✨","🎉","🎂","☕","🍕","🍺","🌹","🌟","💯","✅",
    "😴","🤩","🥰","😇","🤤","😏","🙄","😅","🤦","🤷","👋","💬"];

  // Временные диагностические логи (ЭТАП 1, п.3): SELECTED при выборе
  // диалога, LOADED после ответа API, RENDER перед отрисовкой — по ним
  // легко проверить в консоли, что все три значения dialog_id совпадают.
  // Включаются через localStorage.setItem('chatDebug','1') без правки кода.
  const DEBUG_CHAT = (() => {
    try { return localStorage.getItem("chatDebug") !== "0"; } catch (_) { return true; }
  })();

  let dialogs = [];
  let activeId = null;          // telegram_id активного (выбранного) диалога — selectedDialogId
  let activeDialog = null;
  // Сообщения хранятся строго по диалогам: dialog_id -> Message[].
  // Никогда не храним общий "плоский" массив сообщений — это и было
  // причиной показа переписки не того контакта при быстром переключении.
  let messagesByDialogId = {};
  let activeContact = null;     // CRM-контакт, если импортирован
  let listFilter = "all";
  let infoTab = "profile";
  let initialized = false;

  // Отмена устаревших запросов при смене диалога/частом опросе.
  let messagesAbortController = null;
  let dialogsAbortController = null;
  let lastDialogsFetchAt = 0;
  const DIALOGS_MIN_INTERVAL_MS = 30000;

  // Фикс "кружок куда-то уходит": опрос сообщений (messagesTimer, каждые
  // 3с) раньше всегда пересобирал innerHTML целиком, даже если в диалоге
  // ничего не изменилось. Это уничтожало и создавало заново <video>
  // круглых видеосообщений/голосовых — воспроизведение сбрасывалось на
  // начало каждые 3 секунды, что и выглядело как "кружок пропадает".
  // Теперь перед перерисовкой сверяем лёгкую сигнатуру списка сообщений
  // и, если она не изменилась, DOM вообще не трогаем.
  let lastRenderSignature = null;
  let lastRenderIdsKey = null;
  function messagesSignature(msgs) {
    return msgs.map((m) => `${m.id}:${m.edited ? 1 : 0}:${m.status || ""}:${m.pinned ? 1 : 0}`).join(",");
  }

  // Точечно обновляет только .msg-meta (галочки/закреп/"изменено") у уже
  // отрисованных строк, не трогая .bubble — там лежит видео/кружки/голосовые,
  // которые нельзя пересоздавать во время воспроизведения. См. комментарий
  // в renderThread() про баг "кружок съезжает вниз".
  function patchMessageMeta(box, msgs) {
    msgs.forEach((m) => {
      const row = box.querySelector(`.msg-row[data-msg-id="${m.id}"]`);
      if (!row) return;
      const metaEl = row.querySelector(".msg-meta");
      if (!metaEl) return;
      const readTick = m.out ? renderTicks(m.status) : "";
      metaEl.innerHTML = `${m.pinned ? '<span title="Закреплено">📌</span>' : ""}${m.edited ? "<span>изменено</span>" : ""}<span>${Utils.timeHHMM(m.date)}</span>${readTick}`;
    });
  }

  // ---- ЭТАП 1: скролл сообщений -----------------------------------
  let stickToBottom = true;
  let pendingNewCount = 0;
  let dialogSwitched = false; // true на первый рендер после выбора нового диалога — всегда скроллим вниз
  const BOTTOM_THRESHOLD_PX = 80;

  function isNearBottom(box) {
    return box.scrollHeight - box.scrollTop - box.clientHeight < BOTTOM_THRESHOLD_PX;
  }

  function scrollThreadToBottom(box) {
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        box.scrollTop = box.scrollHeight;
      });
    });
  }

  function updateNewMessagesButton() {
    const btn = $("threadNewMsgBtn");
    const box = $("threadMessages");
    if (!btn || !box) return;
    // Подстраховка: если по факту скролл уже внизу (например, событие
    // scroll не сработало при программном скролле), пересчитываем
    // состояние прямо здесь, а не доверяем только сохранённому флагу —
    // иначе кнопка может "залипнуть" видимой.
    if (box.childElementCount > 0 && isNearBottom(box)) {
      stickToBottom = true;
      pendingNewCount = 0;
    }
    if (!stickToBottom && pendingNewCount > 0) {
      btn.hidden = false;
      btn.style.display = "flex";
      btn.textContent = pendingNewCount === 1 ? "↓ Новое сообщение" : `↓ Новые сообщения (${pendingNewCount})`;
    } else {
      btn.hidden = true;
      btn.style.display = "none";
    }
  }

  function currentMessages() {
    return messagesByDialogId[activeId] || [];
  }

  let replyTo = null;           // {id, text}
  let editing = null;           // {id, originalText}
  let forwardMessageId = null;

  let dialogsTimer = null;
  let messagesTimer = null;
  let presenceTimer = null;
  let liveScoreTimer = null;

  let mediaRecorder = null;
  let recordedChunks = [];
  let recordStartedAt = 0;
  let recordTimerHandle = null;

  const $ = (id) => document.getElementById(id);

  // ---- helpers --------------------------------------------------
  function findDialog(id) {
    return dialogs.find((d) => d.telegram_id === id);
  }

  function avatarHtml(dialog, size) {
    const cls = size === "lg" ? "co-avatar" : "chitem__avatar";
    if (dialog.has_photo) {
      return `<div class="${cls}"><img src="${API.tgAvatarUrl(dialog.telegram_id)}" alt=""
        onerror="this.parentElement.textContent='${Utils.initials(dialog.name)}'"></div>`;
    }
    return `<div class="${cls}">${Utils.initials(dialog.name)}</div>`;
  }

  function previewText(d) {
    const prefix = d.last_message_out ? "Вы: " : "";
    const kindLabels = { photo: "📷 Фото", voice: "🎤 Голосовое сообщение", video: "📹 Видео", document: "📄 ", sticker: "🖼 Стикер" };
    if (d.last_message_kind && d.last_message_kind !== "text") {
      const label = kindLabels[d.last_message_kind] || "Вложение";
      return prefix + label;
    }
    return prefix + (d.last_message_text || "");
  }

  // ---- render: list ----------------------------------------------
  function renderList() {
    const q = ($("chatSearchInput").value || "").toLowerCase().trim();
    const container = $("chatListItems");
    let items = dialogs.filter((d) => {
      if (q && !d.name.toLowerCase().includes(q) && !(d.username || "").toLowerCase().includes(q)) return false;
      if (listFilter === "unread") return d.unread_count > 0;
      if (listFilter === "pinned") return d.pinned;
      if (typeof listFilter === "string" && listFilter.startsWith("folder:")) {
        const folderId = Number(listFilter.slice("folder:".length));
        return d.folder_id === folderId;
      }
      return true;
    });
    items = [...items].sort((a, b) => (b.pinned - a.pinned));

    if (!items.length) {
      container.innerHTML = `<div class="empty-col">${dialogs.length ? "Ничего не найдено" : "Пока нет диалогов"}</div>`;
    } else {
      container.innerHTML = items.map((d) => {
        const preview = d.typing ? "печатает…" : Utils.escapeHtml(previewText(d));
        return `
          <div class="chitem ${d.telegram_id === activeId ? "is-active" : ""} ${d.unread_count ? "has-unread" : ""}" data-chat-id="${d.telegram_id}">
            <div class="chitem__avatar">
              ${d.has_photo ? `<img src="${API.tgAvatarUrl(d.telegram_id)}" alt="" style="width:100%;height:100%;border-radius:50%;object-fit:cover;" onerror="this.style.display='none';this.parentElement.textContent='${Utils.initials(d.name)}'">` : Utils.initials(d.name)}
              ${d.online ? '<span class="chitem__online"></span>' : ""}
            </div>
            <div class="chitem__body">
              <div class="chitem__top">
                <span class="chitem__name">${Utils.escapeHtml(d.name)}</span>
                <span class="chitem__time">${d.last_message_date ? Utils.timeHHMM(d.last_message_date) : ""}</span>
              </div>
              <div class="chitem__row2">
                <span class="chitem__preview ${d.typing ? "is-typing" : ""}">${preview}</span>
                ${d.unread_count ? `<span class="chitem__unread">${d.unread_count}</span>` : ""}
              </div>
            </div>
            <button class="chitem__folderbtn" data-move-id="${d.telegram_id}" title="Переместить в папку">⋯</button>
          </div>`;
      }).join("");
    }

    container.querySelectorAll(".chitem").forEach((el) => {
      el.addEventListener("click", (e) => {
        if (e.target.closest(".chitem__folderbtn")) return;
        selectChat(Number(el.dataset.chatId));
      });
    });
    container.querySelectorAll(".chitem__folderbtn").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const rect = btn.getBoundingClientRect();
        document.dispatchEvent(new CustomEvent("chatlist:move-request", {
          detail: { telegramId: Number(btn.dataset.moveId), x: rect.left, y: rect.bottom },
        }));
      });
    });

    const totalUnread = dialogs.reduce((sum, d) => sum + (d.unread_count || 0), 0);
    const badge = $("chatUnreadBadge");
    badge.hidden = totalUnread === 0;
    badge.textContent = totalUnread;
  }

  // ---- read receipts ----------------------------------------------
  // status приходит с backend на основе read_outbox_max_id: "sent" — ещё
  // не прочитано собеседником (одна галочка), "read" — прочитано (две
  // синие галочки). Раньше здесь рисовалась только "прочитано" для
  // ЛЮБОГО исходящего сообщения, поэтому статус визуально никогда не
  // менялся — это и был баг ЭТАП 4.
  function renderTicks(status) {
    if (status === "read") {
      return `<svg class="tick-read" width="14" height="10" viewBox="0 0 16 11" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m1 5.5 3.2 3.5L10 2"/><path d="m6 5.5 3.2 3.5L15 2"/></svg>`;
    }
    // "sent" (или ещё неизвестно) — одна галочка, без цвета "прочитано".
    return `<svg class="tick-sent" width="14" height="10" viewBox="0 0 16 11" fill="none" stroke="currentColor" stroke-width="1.6"><path d="m6 5.5 3.2 3.5L15 2"/></svg>`;
  }

  // ---- render: thread ----------------------------------------------
  function bubbleContent(m) {
    let html = "";
    if (m.reply_to) {
      html += `<div class="bubble__reply">
        <span class="bubble__reply-name">Ответ</span>
        <span class="bubble__reply-text">${Utils.escapeHtml(m.reply_to.text || "Сообщение")}</span>
      </div>`;
    }
    if (m.media) {
      html += MediaMessage.render(m.media, API.tgMediaUrl(activeId, m.id), m.id);
    }
    if (m.text) html += Utils.escapeHtml(m.text);
    return html;
  }

  function messageActionsHtml(m) {
    const canEdit = m.out && m.text && !m.media;
    return `
      <div class="msg-actions">
        <button type="button" class="msg-action" data-act="reply" title="Ответить">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 8 4 12l5 4"/><path d="M4 12h10a6 6 0 0 1 6 6v1"/></svg>
        </button>
        <button type="button" class="msg-action" data-act="forward" title="Переслать">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m15 8 5 4-5 4"/><path d="M20 12H10a6 6 0 0 0-6 6v1"/></svg>
        </button>
        <button type="button" class="msg-action" data-act="pin" title="${m.pinned ? "Открепить" : "Закрепить"}">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="${m.pinned ? "currentColor" : "none"}" stroke="currentColor" stroke-width="2"><path d="M12 2v8m0 0-4 2 1 2h6l1-2-4-2Z"/><path d="M12 12v10"/></svg>
        </button>
        ${canEdit ? `<button type="button" class="msg-action" data-act="edit" title="Редактировать">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
        </button>` : ""}
        ${m.out ? `<button type="button" class="msg-action" data-act="delete" title="Удалить">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="m6 7 1 13h10l1-13"/></svg>
        </button>` : ""}
      </div>`;
  }

  function renderThread() {
    // Гейт по activeId (dialog_id), а не по activeDialog: activeDialog —
    // это просто карточка из списка диалогов, она может ещё не успеть
    // подъехать (например, сразу после перехода из CRM-карточки), но
    // если пользователь уже выбрал диалог, экран "выберите диалог слева"
    // показывать нельзя — это и есть баг из ЭТАП 1, п.5.
    // Переключаем и через hidden, и напрямую через inline style.display.
    // Inline style имеет наивысший приоритет в каскаде и не может быть
    // перебит никаким классом (.thread__empty{display:flex} и т.п.),
    // даже если в браузере закэширован старый CSS-файл — это защищает
    // от повторения бага "Выберите диалог слева поверх открытого чата".
    const threadEmptyEl = $("threadEmpty");
    const threadBodyEl = $("threadBody");
    threadEmptyEl.hidden = !!activeId;
    threadEmptyEl.style.display = activeId ? "none" : "";
    threadBodyEl.hidden = !activeId;
    threadBodyEl.style.display = activeId ? "" : "none";
    if (!activeId) return;

    if (activeDialog) {
      $("threadAvatar").innerHTML = activeDialog.has_photo
        ? `<img src="${API.tgAvatarUrl(activeDialog.telegram_id)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;" alt="">`
        : Utils.initials(activeDialog.name);
      $("threadName").textContent = activeDialog.name;
      updateStatusLine();
    } else {
      // Диалог выбран, но объект диалога из списка ещё не загружен —
      // как только refreshDialogs() найдёт его, renderThread() позовут снова.
      $("threadAvatar").innerHTML = "";
      $("threadName").textContent = "Загрузка…";
    }

    const box = $("threadMessages");
    const msgs = currentMessages();
    if (DEBUG_CHAT) console.log("RENDER:", activeId, msgs.length);

    // Если с прошлой отрисовки в этом диалоге ничего не поменялось (тот
    // же набор сообщений, те же статусы) — не пересобираем DOM. Это и
    // есть исправление "кружок куда-то уходит": именно полная пересборка
    // DOM каждые 3 секунды обрывала воспроизведение видео/кружков/голосовых.
    const idsKey = msgs.map((m) => m.id).join(",");
    const signature = `${activeId}::${idsKey}::${messagesSignature(msgs)}`;
    if (!dialogSwitched && box.childElementCount > 0) {
      if (signature === lastRenderSignature) {
        updateNewMessagesButton();
        return;
      }
      // Тот же набор сообщений в том же порядке — поменялись только
      // мета-поля (галочки "доставлено/прочитано", закреп, "изменено").
      // Раньше в этом случае всё равно шла полная пересборка box.innerHTML,
      // из-за чего <video> внутри кружков и голосовых пересоздавался с
      // нуля прямо во время воспроизведения — отсюда и "кружок съезжает
      // вниз"/дёргается: браузер на миг рисует новый video-элемент раньше,
      // чем к нему успевает примениться круглая маска. Если состав
      // сообщений не менялся, просто точечно обновляем блок с галочками
      // и таймингом, не трогая .bubble с медиа вообще.
      if (idsKey === lastRenderIdsKey) {
        patchMessageMeta(box, msgs);
        lastRenderSignature = signature;
        updateNewMessagesButton();
        return;
      }
    }
    lastRenderIdsKey = idsKey;
    lastRenderSignature = signature;

    // Запоминаем состояние скролла до перерисовки, чтобы решить, нужно
    // ли докручивать экран вниз после обновления DOM.
    const prevCount = box.dataset.msgCount ? Number(box.dataset.msgCount) : 0;
    const wasNearBottom = box.childElementCount === 0 ? true : isNearBottom(box);
    const addedCount = Math.max(0, msgs.length - prevCount);
    box.dataset.msgCount = String(msgs.length);

    let html = "";
    let lastDate = null, lastOut = null;
    msgs.forEach((m) => {
      const dLabel = m.date ? Utils.dayLabel(m.date) : "";
      if (dLabel && dLabel !== lastDate) {
        html += `<div class="msg-date">${dLabel}</div>`;
        lastDate = dLabel; lastOut = null;
      }
      const grouped = m.out === lastOut;
      lastOut = m.out;
      const readTick = m.out ? renderTicks(m.status) : "";
      html += `
        <div class="msg-row is-${m.out ? "out" : "in"} ${grouped ? "is-grouped" : ""}" data-msg-id="${m.id}">
          <div class="bubble">${bubbleContent(m)}</div>
          ${messageActionsHtml(m)}
          <div class="msg-meta">${m.pinned ? '<span title="Закреплено">📌</span>' : ""}${m.edited ? "<span>изменено</span>" : ""}<span>${Utils.timeHHMM(m.date)}</span>${readTick}</div>
        </div>`;
    });
    box.innerHTML = html || (msgs.length === 0 && !activeDialog
      ? `<div class="empty-col">Загрузка переписки…</div>`
      : `<div class="empty-col">Переписки пока нет — напишите первым.</div>`);

    // Если пользователь был внизу — докручиваем и после смены диалога
    // (dialogSwitched=true выставляется в selectChat). Если он читал
    // историю выше и пришли новые сообщения — не двигаем экран, а
    // показываем кнопку "Новые сообщения".
    if (dialogSwitched || wasNearBottom || stickToBottom) {
      stickToBottom = true;
      pendingNewCount = 0;
      scrollThreadToBottom(box);
    } else if (addedCount > 0) {
      pendingNewCount += addedCount;
    }
    dialogSwitched = false;
    updateNewMessagesButton();
    if (addedCount > 0) refreshLiveScore();

    box.querySelectorAll(".att-voice__play").forEach((btn) => {
      btn.addEventListener("click", () => toggleVoicePlayback(btn));
    });
    MediaMessage.wire(box);
    box.querySelectorAll(".msg-action").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const row = btn.closest(".msg-row");
        const id = Number(row.dataset.msgId);
        handleMessageAction(btn.dataset.act, id);
      });
    });
  }


  let currentAudio = null;
  function setVoiceIcon(wrap, playing) {
    const playIcon = wrap.querySelector(".att-voice__icon-play");
    const pauseIcon = wrap.querySelector(".att-voice__icon-pause");
    if (playIcon) playIcon.hidden = playing;
    if (pauseIcon) pauseIcon.hidden = !playing;
    wrap.classList.toggle("is-playing", playing);
  }
  function resetVoiceWave(wrap) {
    wrap.querySelectorAll(".att-voice__wave span").forEach((bar) => bar.classList.remove("is-played"));
  }
  function toggleVoicePlayback(btn) {
    const wrap = btn.closest(".att-voice");
    const url = wrap.dataset.voiceUrl;
    if (currentAudio && currentAudio._wrap === wrap && !currentAudio.paused) {
      currentAudio.pause();
      setVoiceIcon(wrap, false);
      return;
    }
    if (currentAudio) { currentAudio.pause(); if (currentAudio._wrap) setVoiceIcon(currentAudio._wrap, false); }
    const audio = new Audio(url);
    audio._wrap = wrap;
    const bars = wrap.querySelectorAll(".att-voice__wave span");
    audio.addEventListener("timeupdate", () => {
      if (!audio.duration || !bars.length) return;
      const activeCount = Math.round((audio.currentTime / audio.duration) * bars.length);
      bars.forEach((bar, i) => bar.classList.toggle("is-played", i < activeCount));
    });
    audio.addEventListener("ended", () => { setVoiceIcon(wrap, false); resetVoiceWave(wrap); });
    audio.addEventListener("pause", () => setVoiceIcon(wrap, false));
    audio.play().then(() => setVoiceIcon(wrap, true)).catch(() => Utils.toast("Не удалось воспроизвести голосовое сообщение"));
    currentAudio = audio;
  }

  function updateStatusLine() {
    const statusEl = $("threadStatus");
    if (!activeDialog) return;
    if (activeDialog.typing) { statusEl.textContent = "печатает…"; statusEl.className = "thread__head-status is-typing"; }
    else if (activeDialog.online) { statusEl.textContent = "в сети"; statusEl.className = "thread__head-status is-online"; }
    else { statusEl.textContent = Utils.presenceLabel(activeDialog); statusEl.className = "thread__head-status"; }
  }

  // ---- message actions ----------------------------------------------
  async function handleMessageAction(action, messageId) {
    const m = currentMessages().find((x) => x.id === messageId);
    if (!m) return;
    if (action === "reply") {
      setReply(m);
    } else if (action === "forward") {
      openForwardModal(messageId);
    } else if (action === "pin") {
      try {
        if (m.pinned) await API.tgUnpinMessage(activeId);
        else await API.tgPinMessage(activeId, messageId);
        await loadMessages({ silent: true });
      } catch (err) { Utils.toast(err.message || "Не удалось изменить закрепление"); }
    } else if (action === "edit") {
      startEdit(m);
    } else if (action === "delete") {
      if (!confirm("Удалить это сообщение?")) return;
      try {
        await API.tgDeleteMessage(activeId, messageId);
        messagesByDialogId[activeId] = currentMessages().filter((x) => x.id !== messageId);
        renderThread();
      } catch (err) { Utils.toast(err.message || "Не удалось удалить сообщение"); }
    }
  }

  function setReply(m) {
    editing = null;
    replyTo = { id: m.id, text: m.text || (m.media ? "Вложение" : "") };
    renderComposerBars();
    $("composerText").focus();
  }

  function startEdit(m) {
    replyTo = null;
    editing = { id: m.id, originalText: m.text };
    $("composerText").value = m.text;
    autosize($("composerText"));
    updateSendIcon();
    renderComposerBars();
    $("composerText").focus();
  }

  function cancelReplyOrEdit() {
    replyTo = null;
    editing = null;
    renderComposerBars();
  }

  function renderComposerBars() {
    let bar = $("composerContextBar");
    if (!bar) return;
    if (editing) {
      bar.hidden = false;
      bar.innerHTML = `<div class="composer-bar"><span class="composer-bar__label">Редактирование сообщения</span><button type="button" class="composer-bar__close" id="btnCancelContext">&times;</button></div>`;
    } else if (replyTo) {
      bar.hidden = false;
      bar.innerHTML = `<div class="composer-bar"><span class="composer-bar__label">Ответ: ${Utils.escapeHtml(replyTo.text.slice(0, 60))}</span><button type="button" class="composer-bar__close" id="btnCancelContext">&times;</button></div>`;
    } else {
      bar.hidden = true;
      bar.innerHTML = "";
    }
    const closeBtn = $("btnCancelContext");
    if (closeBtn) closeBtn.addEventListener("click", cancelReplyOrEdit);
  }

  // ---- forward modal ----------------------------------------------
  function openForwardModal(messageId) {
    forwardMessageId = messageId;
    const modal = $("modalForward");
    const list = $("forwardDialogList");
    list.innerHTML = dialogs.map((d) => `
      <div class="chitem" data-fwd-id="${d.telegram_id}">
        <div class="chitem__avatar">${Utils.initials(d.name)}</div>
        <div class="chitem__body"><div class="chitem__top"><span class="chitem__name">${Utils.escapeHtml(d.name)}</span></div></div>
      </div>`).join("");
    list.querySelectorAll("[data-fwd-id]").forEach((el) => {
      el.addEventListener("click", async () => {
        const toId = Number(el.dataset.fwdId);
        try {
          await API.tgForwardMessage(activeId, forwardMessageId, toId);
          Utils.toast("Сообщение переслано");
          modal.hidden = true;
        } catch (err) { Utils.toast(err.message || "Не удалось переслать сообщение"); }
      });
    });
    modal.hidden = false;
  }

  // ---- render: info panel ----------------------------------------------
  async function renderInfo() {
    const panel = $("infopanel");
    if (!activeDialog) { panel.classList.add("is-collapsed"); return; }

    try {
      activeContact = await API.getContactByTelegramId(activeDialog.telegram_id);
    } catch (_) {
      activeContact = null;
    }

    if (!activeContact) {
      $("paneProfile").innerHTML = `
        <div class="infopanel__head">
          <div class="co-avatar">${activeDialog.has_photo ? `<img src="${API.tgAvatarUrl(activeDialog.telegram_id)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;" alt="">` : Utils.initials(activeDialog.name)}</div>
          <div class="infopanel__head-name">${Utils.escapeHtml(activeDialog.name)}</div>
          <div class="infopanel__head-uname">${activeDialog.username ? "@" + Utils.escapeHtml(activeDialog.username) : ""}</div>
        </div>
        <div class="section-title">Контакт</div>
        <div class="info-rows">
          <div class="info-row"><span class="info-row__label">Телефон</span><span class="info-row__val">${Utils.escapeHtml(activeDialog.phone || "—")}</span></div>
        </div>
        <p style="font-size:12.5px;color:var(--ink-faint);margin-bottom:14px;">Этого человека пока нет в CRM — добавьте, чтобы вести статус, теги и историю.</p>
        <button class="btn btn--primary infopanel__save" id="btnQuickAdd" type="button">Добавить в CRM</button>`;
      $("btnQuickAdd").addEventListener("click", quickAddToCrm);
      $("paneHistoryTab").innerHTML = `<p style="font-size:12.5px;color:var(--ink-faint);">Добавьте контакт в CRM, чтобы вести историю взаимодействия.</p>`;
      return;
    }

    ensureProfilePaneMarkup();
    const msgCount = currentMessages().length;
    $("infoContactRows").innerHTML = `
      <div class="info-row"><span class="info-row__label">Телефон</span><span class="info-row__val">${Utils.escapeHtml(activeContact.phone || activeDialog.phone || "—")}</span></div>
      <div class="info-row"><span class="info-row__label">Дата знакомства</span><span class="info-row__val">${Utils.formatDate(activeContact.created_at)}</span></div>
      <div class="info-row"><span class="info-row__label">Источник</span><span class="info-row__val">${Utils.escapeHtml(activeContact.source || "—")}</span></div>
      <div class="info-row"><span class="info-row__label">Всего сообщений</span><span class="info-row__val">${msgCount || "—"}</span></div>
    `;
    $("infoStatus").value = activeContact.status;
    $("infoInterest").value = activeContact.interest_level;
    $("infoInterestVal").textContent = activeContact.interest_level;
    $("infoTags").innerHTML = (activeContact.tags || []).map((t) => `<span class="tag-chip">${Utils.escapeHtml(t.name)}</span>`).join("")
      || `<span style="color:var(--ink-faint);font-size:12px;">Тегов пока нет</span>`;
    $("infoTagsInput").value = (activeContact.tags || []).map((t) => t.name).join(", ");
    $("infoNotes").value = activeContact.notes || "";

    // Если ранее открывали контакт не из CRM, #infoHistory мог быть
    // уничтожен (см. ветку !activeContact выше) — восстанавливаем
    // разметку вкладки "История" перед тем как в неё писать.
    ensureHistoryPaneMarkup();
    $("infoHistory").innerHTML = (activeContact.interactions || []).map((i) => `
      <div class="tl-item"><div class="tl-item__date">${Utils.formatDateTime(i.occurred_at)}</div><div class="tl-item__note">${Utils.escapeHtml(i.note)}</div></div>
    `).join("") || `<div class="empty-col">Записей пока нет</div>`;

    refreshLiveScore(); // сразу считаем при открытии диалога, дальше — по таймеру (см. startPolling)
  }

  // ---- "живая" оценка интереса (см. AI_PROVIDER=local в analysis.py) --
  //
  // Считается по уже загруженным на клиенте сообщениям (currentMessages()) —
  // никакого похода в Telegram и никогда не дёргает Gemini, поэтому можно
  // пересчитывать в фоне часто, не думая о лимитах/цене. Backend всё равно
  // дополнительно троттлит через LIVE_SCORE_MIN_INTERVAL.
  async function refreshLiveScore() {
    if (!activeContact || !activeId || !$("liveScoreBox")) return;
    const contactId = activeContact.id;
    const msgs = currentMessages();
    if (!msgs.length) return;
    try {
      const payload = msgs.map((m) => ({ text: m.text, date: m.date, out: m.out }));
      const data = await API.liveScore(contactId, payload);
      // Пока запрос летел, могли переключить диалог/контакт — не рисуем чужие цифры.
      if (!activeContact || activeContact.id !== contactId || data.contact_id !== contactId) return;
      renderLiveScore(data);
    } catch (_) { /* тихо игнорируем — это лишь дополнительный индикатор, не основной поток */ }
  }

  function renderLiveScore(data) {
    const box = $("liveScoreBox");
    if (!box) return;
    const badge = Utils.aiScoreBadge(data.interest_score);
    box.innerHTML = `
      <span class="ai-badge ai-badge--${badge.cls}">${data.interest_score}<small>/100</small></span>
      <span class="live-score__cat">${Utils.escapeHtml(badge.label)}</span>
      ${Utils.trendChipHTML(data.trend)}
      ${data.status_change_suggested ? `<span class="live-score__hint">похоже на «${Utils.escapeHtml(data.suggested_status_label)}»</span>` : ""}
    `;
  }

  function ensureHistoryPaneMarkup() {
    if ($("infoHistory")) return;
    $("paneHistoryTab").innerHTML = `<div class="timeline" id="infoHistory"></div>`;
  }

  function ensureProfilePaneMarkup() {
    if ($("infoStatus")) {
      $("infoAvatar").innerHTML = activeDialog.has_photo
        ? `<img src="${API.tgAvatarUrl(activeDialog.telegram_id)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;" alt="">`
        : Utils.initials(activeDialog.name);
      $("infoName").textContent = activeDialog.name;
      $("infoUname").textContent = activeDialog.username ? "@" + activeDialog.username : "";
      return;
    }
    $("paneProfile").innerHTML = `
      <div class="infopanel__head">
        <div class="co-avatar" id="infoAvatar"></div>
        <div class="infopanel__head-name" id="infoName"></div>
        <div class="infopanel__head-uname" id="infoUname"></div>
      </div>
      <div class="section-title">Контакт</div>
      <div class="info-rows" id="infoContactRows"></div>
      <div class="section-title">Живой интерес <span class="live-score__dot" title="Обновляется автоматически по мере переписки, без нажатия кнопок"></span></div>
      <div class="live-score" id="liveScoreBox"><span class="live-score__empty">Считаю…</span></div>
      <div class="section-title">Статус и интерес</div>
      <div class="co-fieldgrid">
        <div class="co-field">
          <label>Статус
            <select id="infoStatus">
              <option value="new">Новый</option>
              <option value="warm">Тёплый</option>
              <option value="in_progress">В работе</option>
              <option value="meeting_scheduled">Встреча назначена</option>
              <option value="met">Встретились</option>
              <option value="archive">Архив</option>
            </select>
          </label>
        </div>
        <div class="co-field">
          <label>Интерес</label>
          <div class="interest-slider">
            <input type="range" id="infoInterest" min="1" max="10" value="5">
            <span class="interest-slider__val" id="infoInterestVal">5</span>
          </div>
        </div>
      </div>
      <div class="section-title">Теги</div>
      <div class="tag-editor" id="infoTags"></div>
      <input type="text" id="infoTagsInput" placeholder="теги через запятую" style="width:100%;margin-top:6px;padding:6px 10px;border:1px solid var(--border);border-radius:var(--radius-sm);font-family:var(--font-ui);">
      <div class="section-title">Заметки</div>
      <textarea class="infopanel__notes" id="infoNotes" rows="4" placeholder="Свободные заметки о контакте…"></textarea>
      <button class="btn btn--primary infopanel__save" id="infoSave" type="button">Сохранить изменения</button>
      <button class="btn infopanel__save" id="infoOpenCrmCard" type="button" style="margin-top:8px;">Открыть карточку CRM</button>`;
    $("infoAvatar").innerHTML = activeDialog.has_photo
      ? `<img src="${API.tgAvatarUrl(activeDialog.telegram_id)}" style="width:100%;height:100%;border-radius:50%;object-fit:cover;" alt="">`
      : Utils.initials(activeDialog.name);
    $("infoName").textContent = activeDialog.name;
    $("infoUname").textContent = activeDialog.username ? "@" + activeDialog.username : "";
    $("infoInterest").addEventListener("input", (e) => { $("infoInterestVal").textContent = e.target.value; });
    $("infoSave").addEventListener("click", saveContactInfo);
    $("infoOpenCrmCard").addEventListener("click", () => {
      if (activeContact) App.goToContact(activeContact.id);
    });
  }

  async function quickAddToCrm() {
    try {
      activeContact = await API.createContact({
        name: activeDialog.name,
        username: activeDialog.username || null,
        telegram_id: activeDialog.telegram_id,
        phone: activeDialog.phone || null,
        source: "Импорт из Telegram",
        status: "new",
      });
      Utils.toast("Добавлено в CRM");
      await renderInfo();
    } catch (err) {
      Utils.toast(err.message || "Не удалось добавить контакт");
    }
  }

  async function saveContactInfo() {
    if (!activeContact) return;
    const tags = $("infoTagsInput").value.split(",").map((t) => t.trim()).filter(Boolean);
    try {
      await API.updateContact(activeContact.id, {
        status: $("infoStatus").value,
        interest_level: Number($("infoInterest").value),
        notes: $("infoNotes").value,
        tags,
      });
      const btn = $("infoSave");
      const original = btn.textContent;
      btn.textContent = "Сохранено ✓";
      setTimeout(() => { btn.textContent = original; }, 1400);
      await renderInfo();
      await refreshDialogs({ silent: true });
    } catch (err) {
      Utils.toast(err.message || "Не удалось сохранить изменения");
    }
  }

  // ---- data loading ----------------------------------------------

  // Список диалогов не обновляется чаще, чем раз в 30 секунд в фоне —
  // тихие (polling) вызовы сверх этого просто игнорируются. Явные
  // (не silent) вызовы — например при первой загрузке — throttle не
  // применяется.
  async function refreshDialogs({ silent } = {}) {
    if (silent && Date.now() - lastDialogsFetchAt < DIALOGS_MIN_INTERVAL_MS) return;

    if (dialogsAbortController) dialogsAbortController.abort();
    const controller = new AbortController();
    dialogsAbortController = controller;

    try {
      const fresh = await API.tgDialogs(100, controller.signal);
      if (controller.signal.aborted) return;
      lastDialogsFetchAt = Date.now();
      dialogs = fresh;
      // Фикс "будто не отвечено, горит непрочитанное": mark_read уходит в
      // Telegram асинхронно (см. selectChat), и сервер не всегда успевает
      // обработать его до следующего фонового опроса диалогов. Из-за этого
      // fresh иногда содержит старый unread_count>0 для диалога, который
      // прямо сейчас открыт — и локально обнулённый счётчик "зажигался"
      // обратно. Открытый диалог всегда считаем прочитанным на клиенте.
      if (activeId) {
        const openDialog = dialogs.find((d) => d.telegram_id === activeId);
        if (openDialog) openDialog.unread_count = 0;
      }
      Cache.dialogs.replaceAll(fresh); // Этап 10: кешируем список диалогов на диск
      if (activeId) {
        const hadDialog = !!activeDialog;
        activeDialog = findDialog(activeId) || activeDialog;
        if (!hadDialog && activeDialog) { renderThread(); renderInfo(); } // диалог только что "нашёлся" — перерисовать шапку и панель справа
        else updateStatusLine();
      }
      renderList();
    } catch (err) {
      if (API.isAbortError(err)) return;
      if (!silent) $("chatListItems").innerHTML = `<div class="empty-col">${Utils.escapeHtml(err.message || "Не удалось загрузить диалоги. Войдите в Telegram на вкладке «Telegram».")}</div>`;
    }
  }

  // Загружает сообщения СТРОГО для конкретного dialog_id.
  //
  // Защита от гонки запросов реализована в двух независимых слоях:
  // 1) AbortController отменяет предыдущий незавершённый запрос при
  //    каждом новом вызове (переключение диалога, следующий тик опроса).
  // 2) Даже если отменённый запрос всё же успел вернуть ответ, перед
  //    записью в состояние мы проверяем, что activeId не сменился, и
  //    что каждое сообщение действительно принадлежит запрошенному
  //    dialog_id (message.dialog_id === requestedDialogId). Всё, что
  //    не совпало, отбрасывается и никогда не попадает на экран.
  async function loadMessages({ silent } = {}) {
    if (!activeId) return;
    const requestedDialogId = activeId;
    const box = $("threadMessages");
    const hasCached = !!(messagesByDialogId[requestedDialogId] && messagesByDialogId[requestedDialogId].length);
    // Есть кеш для этого диалога — показываем его сразу (уже сделано в
    // renderThread() при selectChat) и обновляем в фоне без "мигания"
    // лоадера. Лоадер уместен только когда данных ещё вообще нет.
    if (!silent && box && !hasCached) box.innerHTML = `<div class="empty-col">Загрузка переписки…</div>`;

    if (messagesAbortController) messagesAbortController.abort();
    const controller = new AbortController();
    messagesAbortController = controller;

    try {
      const fresh = await API.tgMessages(requestedDialogId, 50, controller.signal);
      if (activeId !== requestedDialogId) return; // диалог уже сменился, ответ устарел
      const belongsToDialog = fresh.filter((m) => m.dialog_id === requestedDialogId);
      if (DEBUG_CHAT) console.log("LOADED:", requestedDialogId, belongsToDialog.length);
      messagesByDialogId[requestedDialogId] = belongsToDialog;
      renderThread();
      Cache.messages.set(requestedDialogId, belongsToDialog); // Этап 10: сохраняем переписку на диск
    } catch (err) {
      if (API.isAbortError(err)) return;
      if (activeId !== requestedDialogId) return;
      if (!silent && box && !hasCached) box.innerHTML = `<div class="empty-col">${Utils.escapeHtml(err.message || "Не удалось загрузить переписку")}</div>`;
    }
  }

  async function refreshPresence() {
    if (!activeId) return;
    const requestedDialogId = activeId;
    try {
      const p = await API.tgPresence(requestedDialogId);
      if (activeId !== requestedDialogId || !activeDialog) return; // диалог уже сменился
      Object.assign(activeDialog, p);
      updateStatusLine();
    } catch (_) { /* тихо игнорируем — статус не критичен */ }
  }

  function stopPolling() {
    clearInterval(dialogsTimer); dialogsTimer = null;
    clearInterval(messagesTimer); messagesTimer = null;
    clearInterval(presenceTimer); presenceTimer = null;
    clearInterval(liveScoreTimer); liveScoreTimer = null;
  }

  function startPolling() {
    stopPolling();
    dialogsTimer = setInterval(() => refreshDialogs({ silent: true }), DIALOGS_MIN_INTERVAL_MS);
    messagesTimer = setInterval(() => { if (activeId) loadMessages({ silent: true }); }, 3000);
    presenceTimer = setInterval(refreshPresence, 2500);
    // Отдельный, более редкий таймер: пересчитывает "живой" интерес по уже
    // загруженным сообщениям (см. refreshLiveScore). Backend всё равно сам
    // троттлит через LIVE_SCORE_MIN_INTERVAL, здесь интервал держим с тем
    // же порядком величины, чтобы не дёргать API впустую между пересчётами.
    liveScoreTimer = setInterval(() => refreshLiveScore(), 5000);
  }

  // ---- interactions ----------------------------------------------
  async function selectChat(id) {
    if (DEBUG_CHAT) console.log("SELECTED:", id);
    if (messagesAbortController) { messagesAbortController.abort(); messagesAbortController = null; }
    activeId = id;
    Cache.setLastSync("lastOpenDialogId", id); // Этап 10: чтобы при следующем запуске открылся тот же чат
    activeDialog = findDialog(id);
    if (activeDialog) activeDialog.unread_count = 0;
    cancelReplyOrEdit();
    $("infopanel").classList.remove("is-collapsed");
    // Новый диалог всегда открывается внизу, независимо от того, где
    // пользователь был прокручен в предыдущем диалоге.
    stickToBottom = true;
    pendingNewCount = 0;
    dialogSwitched = true;
    $("threadMessages").dataset.msgCount = "0";
    renderList();
    // Рендерим сразу с сообщениями нового диалога (пусто, если ещё
    // не загружались) — старые сообщения предыдущего контакта не
    // должны оставаться на экране ни на кадр, пока летит запрос.
    renderThread();
    await loadMessages();
    await renderInfo();
    try { await API.tgMarkRead(id); } catch (_) {}
  }

  async function sendMessage() {
    const textarea = $("composerText");
    const text = textarea.value.trim();
    if (!activeId || !text) return;

    if (editing) {
      const id = editing.id;
      textarea.value = "";
      autosize(textarea); updateSendIcon();
      editing = null; renderComposerBars();
      try {
        await API.tgEditMessage(activeId, id, text);
        await loadMessages({ silent: true });
      } catch (err) { Utils.toast(err.message || "Не удалось отредактировать сообщение"); }
      return;
    }

    const replyId = replyTo ? replyTo.id : null;
    textarea.value = "";
    autosize(textarea); updateSendIcon();
    replyTo = null; renderComposerBars();
    try {
      await API.tgSendMessage(activeId, text, replyId);
      await loadMessages({ silent: true });
    } catch (err) { Utils.toast(err.message || "Не удалось отправить сообщение"); }
  }

  async function sendFile(file, opts = {}) {
    if (!activeId) return;
    try {
      await API.tgSendFile(activeId, file, opts);
      await loadMessages({ silent: true });
    } catch (err) {
      Utils.toast(err.message || "Не удалось отправить файл");
    }
  }

  function autosize(el) {
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  }

  function updateSendIcon() {
    const hasText = $("composerText").value.trim().length > 0;
    const sendBtn = $("composerSend");
    const voiceBtn = $("composerVoice");
    sendBtn.hidden = !hasText;
    sendBtn.style.display = hasText ? "flex" : "none";
    voiceBtn.hidden = hasText;
    voiceBtn.style.display = hasText ? "none" : "flex";
  }

  // ---- emoji picker ----------------------------------------------
  function toggleEmojiPicker() {
    let pop = $("emojiPopover");
    if (pop) { pop.remove(); return; }
    pop = document.createElement("div");
    pop.id = "emojiPopover";
    pop.className = "emoji-popover";
    pop.innerHTML = EMOJI.map((e) => `<button type="button" class="emoji-popover__btn">${e}</button>`).join("");
    $("composerForm").appendChild(pop);
    pop.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        const textarea = $("composerText");
        const start = textarea.selectionStart || textarea.value.length;
        const end = textarea.selectionEnd || textarea.value.length;
        textarea.value = textarea.value.slice(0, start) + btn.textContent + textarea.value.slice(end);
        textarea.focus();
        textarea.selectionStart = textarea.selectionEnd = start + btn.textContent.length;
        autosize(textarea); updateSendIcon();
      });
    });
    document.addEventListener("click", function closeOnOutside(e) {
      if (!pop.contains(e.target) && e.target.id !== "btnEmoji") {
        pop.remove();
        document.removeEventListener("click", closeOnOutside);
      }
    }, { capture: true });
  }

  // ---- voice recording ----------------------------------------------
  async function startRecording() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      Utils.toast("Запись голоса не поддерживается этим браузером");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recordedChunks = [];
      mediaRecorder = new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
      mediaRecorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(recordedChunks, { type: "audio/webm" });
        const file = new File([blob], "voice-message.webm", { type: "audio/webm" });
        sendFile(file, { voice: true, replyTo: replyTo ? replyTo.id : null });
        replyTo = null; renderComposerBars();
      };
      mediaRecorder.start();
      recordStartedAt = Date.now();
      showRecordingUi(true);
      recordTimerHandle = setInterval(updateRecordingTimer, 250);
    } catch (err) {
      Utils.toast("Нет доступа к микрофону");
    }
  }

  function stopRecording(cancel) {
    if (!mediaRecorder) return;
    if (cancel) recordedChunks = [];
    mediaRecorder.stop();
    mediaRecorder = null;
    clearInterval(recordTimerHandle);
    showRecordingUi(false);
    updateSendIcon();
  }

  function updateRecordingTimer() {
    const el = $("recordTimer");
    if (!el) return;
    const sec = Math.floor((Date.now() - recordStartedAt) / 1000);
    el.textContent = `${String(Math.floor(sec / 60)).padStart(1, "0")}:${String(sec % 60).padStart(2, "0")}`;
  }

  function showRecordingUi(active) {
    $("composerField").hidden = active;
    $("btnEmoji").hidden = active;
    $("btnAttach").hidden = active;
    $("composerVoice").hidden = active;
    let bar = $("recordingBar");
    if (active) {
      if (!bar) {
        bar = document.createElement("div");
        bar.id = "recordingBar";
        bar.className = "recording-bar";
        bar.innerHTML = `<span class="recording-bar__dot"></span><span id="recordTimer">0:00</span><span class="recording-bar__hint">Запись голосового…</span>
          <button type="button" class="btn" id="btnCancelRecord">Отмена</button>`;
        $("composerForm").insertBefore(bar, $("composerSend"));
        $("btnCancelRecord").addEventListener("click", () => stopRecording(true));
      }
    } else if (bar) {
      bar.remove();
    }
  }

  // ---- wiring ----------------------------------------------
  function wire() {
    $("chatSearchInput").addEventListener("input", renderList);

    document.querySelectorAll(".chatlist__tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        listFilter = btn.dataset.chatfilter;
        document.querySelectorAll(".chatlist__tab").forEach((b) => b.classList.toggle("is-active", b === btn));
        document.querySelectorAll(".folderpill").forEach((b) => b.classList.remove("is-active"));
        renderList();
      });
    });

    document.querySelectorAll(".infopanel__tabs .co-tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        infoTab = btn.dataset.infotab;
        document.querySelectorAll(".infopanel__tabs .co-tab").forEach((b) => b.classList.toggle("is-active", b === btn));
        $("paneProfile").hidden = infoTab !== "profile";
        $("paneHistoryTab").hidden = infoTab !== "history";
      });
    });

    $("threadInfoToggle").addEventListener("click", () => {
      $("infopanel").classList.toggle("is-collapsed");
    });

    // ЭТАП 1: если пользователь сам прокрутил вверх — новые сообщения
    // больше не должны дёргать экран, только копить счётчик.
    const threadMessagesBox = $("threadMessages");
    threadMessagesBox.addEventListener("scroll", () => {
      stickToBottom = isNearBottom(threadMessagesBox);
      if (stickToBottom) { pendingNewCount = 0; updateNewMessagesButton(); }
    });
    const newMsgBtn = $("threadNewMsgBtn");
    if (newMsgBtn) {
      newMsgBtn.addEventListener("click", () => {
        stickToBottom = true;
        pendingNewCount = 0;
        scrollThreadToBottom(threadMessagesBox);
        updateNewMessagesButton();
      });
    }

    const textarea = $("composerText");
    textarea.addEventListener("input", () => { autosize(textarea); updateSendIcon(); });
    // Вставка скриншотов/картинок из буфера обмена (Ctrl+V) —
    // отправляем как обычное вложение, как приложенный через скрепку файл.
    textarea.addEventListener("paste", (e) => {
      const items = e.clipboardData && e.clipboardData.items;
      if (!items) return;
      const imageItem = Array.from(items).find((it) => it.type && it.type.startsWith("image/"));
      if (!imageItem) return; // обычный текст — стандартная вставка, ничего не перехватываем
      e.preventDefault();
      const file = imageItem.getAsFile();
      if (!file) return;
      const ext = (file.type.split("/")[1] || "png").replace("jpeg", "jpg");
      const named = new File([file], `screenshot-${Date.now()}.${ext}`, { type: file.type });
      sendFile(named, { replyTo: replyTo ? replyTo.id : null });
      replyTo = null; renderComposerBars();
    });
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        $("composerForm").requestSubmit();
      }
      if (e.key === "Escape" && (replyTo || editing)) cancelReplyOrEdit();
    });
    $("composerForm").addEventListener("submit", (e) => {
      e.preventDefault();
      sendMessage();
    });

    $("btnEmoji").addEventListener("click", (e) => { e.stopPropagation(); toggleEmojiPicker(); });

    $("btnAttach").addEventListener("click", () => $("fileAttachInput").click());
    $("fileAttachInput").addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (!file) return;
      sendFile(file, { replyTo: replyTo ? replyTo.id : null });
      replyTo = null; renderComposerBars();
      e.target.value = "";
    });

    let micHeld = false;
    $("composerVoice").addEventListener("click", () => {
      if (!micHeld) { micHeld = true; startRecording(); }
      else { micHeld = false; stopRecording(false); }
    });

    $("modalForward").addEventListener("click", (e) => {
      if (e.target.dataset.closeModal !== undefined || e.target === $("modalForward")) {
        $("modalForward").hidden = true;
      }
    });
    const forwardSearch = $("forwardSearchInput");
    if (forwardSearch) {
      forwardSearch.addEventListener("input", () => {
        const q = forwardSearch.value.toLowerCase();
        $("forwardDialogList").querySelectorAll(".chitem").forEach((el) => {
          const name = el.querySelector(".chitem__name").textContent.toLowerCase();
          el.style.display = name.includes(q) ? "" : "none";
        });
      });
    }
  }

  async function render() {
    if (!initialized) { wire(); initialized = true; }

    // Этап 10: мгновенно показываем закешированные диалоги и, если
    // получится, сразу открываем тот же чат, что был открыт в прошлый
    // раз — человек не должен видеть пустой экран, пока летит сеть.
    if (!dialogs.length) {
      const cachedDialogs = await Cache.dialogs.getAll();
      if (cachedDialogs.length) {
        dialogs = cachedDialogs;
        renderList();
        if (!activeId) {
          const lastId = await Cache.getLastSync("lastOpenDialogId");
          const restoreTo = lastId && findDialog(lastId) ? lastId : (dialogs.find((d) => d.pinned) || dialogs[0]).telegram_id;
          activeId = restoreTo;
          activeDialog = findDialog(restoreTo);
          const cachedMessages = await Cache.messages.get(restoreTo);
          if (cachedMessages) messagesByDialogId[restoreTo] = cachedMessages;
          dialogSwitched = true;
          renderThread();
        }
      }
    }

    await refreshDialogs();
    if (!activeId && dialogs.length) {
      await selectChat((dialogs.find((d) => d.pinned) || dialogs[0]).telegram_id);
    } else if (activeId) {
      renderList();
      renderThread();
      loadMessages({ silent: true }); // догружаем свежие сообщения в фоне поверх кеша
    } else {
      renderList();
      renderThread();
    }
    updateSendIcon();
    startPolling();
  }

  // Используется folders.js: переключение фильтра списка диалогов на
  // конкретную папку (или обратно на "Все"/"Непрочитанные"/"Избранное")
  // без необходимости знать о внутреннем состоянии этого модуля.
  function setListFilter(value) {
    listFilter = value;
    renderList();
  }

  return {
    render, stopPolling, openDialog: selectChat,
    setListFilter,
    refreshDialogs,
  };
})();
