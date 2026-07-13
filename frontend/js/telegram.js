/* Экран "Telegram": вход в аккаунт по номеру телефона (Telegram API,
   логика на сервере через Telethon) и импорт контактов из него в CRM. */
const Telegram = (() => {
  let step = "phone";      // phone -> code -> (password) -> done
  let phone = "";
  let tgContacts = [];
  let selected = new Set();

  const authPanel = () => document.getElementById("tgAuthPanel");
  const importPanel = () => document.getElementById("tgImportPanel");
  const listEl = () => document.getElementById("tgContactList");

  // ---------- auth panel rendering ----------

  function renderPhoneStep() {
    authPanel().innerHTML = `
      <h2 class="panel__title">Вход в Telegram</h2>
      <p class="tg-hint">Введите номер телефона аккаунта, контакты которого нужно подключить к CRM.</p>
      <form class="form" id="tgPhoneForm">
        <label>Номер телефона<input type="tel" name="phone" placeholder="+7 900 000-00-00" required></label>
        <div class="form__actions">
          <button type="submit" class="btn btn--primary">Отправить код</button>
        </div>
      </form>
    `;
    document.getElementById("tgPhoneForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const value = new FormData(e.target).get("phone").trim();
      if (!value) return;
      const btn = e.target.querySelector("button");
      btn.disabled = true;
      try {
        await API.tgSendCode(value);
        phone = value;
        step = "code";
        renderCodeStep();
      } catch (err) {
        Utils.toast(err.message || "Не удалось отправить код");
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderCodeStep() {
    authPanel().innerHTML = `
      <h2 class="panel__title">Код из Telegram</h2>
      <p class="tg-hint">Код отправлен в приложение Telegram на номер ${Utils.escapeHtml(phone)}.</p>
      <form class="form" id="tgCodeForm">
        <label>Код подтверждения<input type="text" name="code" inputmode="numeric" placeholder="12345" required autofocus></label>
        <div class="form__actions">
          <button type="button" class="btn" id="tgBackToPhone">Назад</button>
          <button type="submit" class="btn btn--primary">Войти</button>
        </div>
      </form>
    `;
    document.getElementById("tgBackToPhone").addEventListener("click", () => {
      step = "phone";
      renderPhoneStep();
    });
    document.getElementById("tgCodeForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const code = new FormData(e.target).get("code").trim();
      const btn = e.target.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        const result = await API.tgSignIn({ phone, code });
        if (result.needs_password) {
          step = "password";
          renderPasswordStep();
        } else {
          Utils.toast("Аккаунт подключён");
          await render();
        }
      } catch (err) {
        Utils.toast(err.message || "Не удалось войти");
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderPasswordStep() {
    authPanel().innerHTML = `
      <h2 class="panel__title">Двухфакторный пароль</h2>
      <p class="tg-hint">На аккаунте включена облачная защита пароля — введите его, как в самом Telegram.</p>
      <form class="form" id="tgPasswordForm">
        <label>Пароль<input type="password" name="password" required autofocus></label>
        <div class="form__actions">
          <button type="submit" class="btn btn--primary">Войти</button>
        </div>
      </form>
    `;
    document.getElementById("tgPasswordForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const password = new FormData(e.target).get("password");
      const btn = e.target.querySelector("button");
      btn.disabled = true;
      try {
        await API.tgSignIn({ phone, code: "", password });
        Utils.toast("Аккаунт подключён");
        await render();
      } catch (err) {
        Utils.toast(err.message || "Неверный пароль");
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderConnected(user) {
    authPanel().innerHTML = `
      <h2 class="panel__title">Telegram подключён</h2>
      <div class="tg-account">
        <div class="avatar-ring__fallback">${Utils.escapeHtml(Utils.initials(user.name))}</div>
        <div>
          <div class="tg-account__name">${Utils.escapeHtml(user.name)}</div>
          <div class="tg-account__uname">${user.username ? "@" + Utils.escapeHtml(user.username) : Utils.escapeHtml(user.phone || "")}</div>
        </div>
        <button class="btn btn--danger" id="tgLogoutBtn" style="margin-left:auto">Отключить</button>
      </div>
    `;
    document.getElementById("tgLogoutBtn").addEventListener("click", async () => {
      if (!confirm("Отключить аккаунт Telegram от CRM?")) return;
      await API.tgLogout();
      step = "phone";
      tgContacts = [];
      selected.clear();
      Utils.toast("Аккаунт отключён");
      await render();
    });
  }

  // ---------- contacts import panel ----------

  function renderContactList() {
    if (!tgContacts.length) {
      listEl().innerHTML = `<div class="empty-col">В этом аккаунте не найдено контактов</div>`;
      return;
    }
    listEl().innerHTML = tgContacts
      .map((c) => {
        const disabled = c.already_imported;
        const checked = disabled || selected.has(c.telegram_id);
        return `
        <label class="tg-contact-row ${disabled ? "is-imported" : ""}">
          <input type="checkbox" data-id="${c.telegram_id}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
          <span class="avatar-ring__fallback avatar-ring__fallback--sm">${Utils.escapeHtml(Utils.initials(c.name))}</span>
          <span class="tg-contact-row__body">
            <span class="tg-contact-row__name">${Utils.escapeHtml(c.name)}</span>
            <span class="tg-contact-row__uname">${c.username ? "@" + Utils.escapeHtml(c.username) : (c.phone || "")}</span>
          </span>
          ${disabled ? '<span class="badge status-met">уже в CRM</span>' : ""}
        </label>`;
      })
      .join("");

    listEl().querySelectorAll("input[type=checkbox]:not(:disabled)").forEach((box) => {
      box.addEventListener("change", () => {
        const id = Number(box.dataset.id);
        if (box.checked) selected.add(id);
        else selected.delete(id);
      });
    });
  }

  async function loadContacts() {
    listEl().innerHTML = `<div class="empty-col">Загрузка контактов…</div>`;
    try {
      tgContacts = await API.tgContacts();
      selected = new Set(tgContacts.filter((c) => !c.already_imported).map((c) => c.telegram_id));
      renderContactList();
    } catch (err) {
      listEl().innerHTML = `<div class="empty-col">${Utils.escapeHtml(err.message || "Не удалось загрузить контакты")}</div>`;
    } finally {
      renderStatsAndActions();
    }
  }

  // ---------- status strip + quick actions ----------

  function renderStatsAndActions() {
    const statGrid = document.getElementById("tgStatGrid");
    const actions = document.getElementById("tgQuickActions");
    if (!statGrid || !actions) return;

    if (step !== "done") {
      statGrid.hidden = true;
      actions.hidden = true;
      return;
    }

    const importedCount = tgContacts.filter((c) => c.already_imported).length;
    const notImportedCount = tgContacts.length - importedCount;
    const crmTotal = (typeof Contacts !== "undefined" && Contacts.items) ? Contacts.items.length : importedCount;

    statGrid.hidden = false;
    statGrid.innerHTML = `
      <div class="stat-card">
        <div class="stat-card__label">Статус подключения</div>
        <div class="stat-card__value accent-teal" style="font-size:18px">Подключён</div>
      </div>
      <div class="stat-card">
        <div class="stat-card__label">Контактов в Telegram</div>
        <div class="stat-card__value">${tgContacts.length}</div>
      </div>
      <div class="stat-card">
        <div class="stat-card__label">Импортировано в CRM</div>
        <div class="stat-card__value accent-primary">${importedCount}</div>
      </div>
      <div class="stat-card">
        <div class="stat-card__label">Всего контактов в CRM</div>
        <div class="stat-card__value">${crmTotal}</div>
      </div>`;

    actions.hidden = false;
    actions.innerHTML = `
      <button type="button" class="tg-quick-action" id="tgActionImportAll" ${notImportedCount ? "" : "disabled"}>
        <span class="tg-quick-action__icon">⬇</span>Импортировать ${notImportedCount ? `${notImportedCount} новых` : "контакты"}
      </button>
      <button type="button" class="tg-quick-action" id="tgActionOpenCrm">
        <span class="tg-quick-action__icon">☰</span>Открыть CRM
      </button>
      <button type="button" class="tg-quick-action" id="tgActionNewCampaign">
        <span class="tg-quick-action__icon">✎</span>Создать кампанию
      </button>
      <button type="button" class="tg-quick-action" id="tgActionSync">
        <span class="tg-quick-action__icon">↻</span>Синхронизировать
      </button>`;

    document.getElementById("tgActionImportAll").addEventListener("click", async () => {
      tgContacts.filter((c) => !c.already_imported).forEach((c) => selected.add(c.telegram_id));
      renderContactList();
      document.getElementById("btnTgImport").click();
    });
    document.getElementById("tgActionOpenCrm").addEventListener("click", () => App.switchView("contacts"));
    document.getElementById("tgActionNewCampaign").addEventListener("click", () => App.switchView("campaigns"));
    document.getElementById("tgActionSync").addEventListener("click", () => loadContacts());
  }

  function wireImportPanelOnce() {
    if (importPanel().dataset.wired) return;
    importPanel().dataset.wired = "1";

    document.getElementById("btnTgSelectAll").addEventListener("click", () => {
      const allSelected = tgContacts.every((c) => c.already_imported || selected.has(c.telegram_id));
      tgContacts.forEach((c) => {
        if (c.already_imported) return;
        if (allSelected) selected.delete(c.telegram_id);
        else selected.add(c.telegram_id);
      });
      renderContactList();
    });

    document.getElementById("btnTgImport").addEventListener("click", async () => {
      if (!selected.size) {
        Utils.toast("Выберите хотя бы один контакт");
        return;
      }
      const btn = document.getElementById("btnTgImport");
      btn.disabled = true;
      try {
        const result = await API.tgImport({ telegram_ids: [...selected] });
        Utils.toast(`Импортировано: ${result.imported}, пропущено: ${result.skipped}`);
        await loadContacts();
        if (typeof Contacts !== "undefined") await Contacts.reload();
      } catch (err) {
        Utils.toast(err.message || "Не удалось импортировать контакты");
      } finally {
        btn.disabled = false;
      }
    });
  }

  function renderError(message) {
    authPanel().innerHTML = `
      <h2 class="panel__title">Telegram недоступен</h2>
      <p class="tg-hint tg-hint--error">${Utils.escapeHtml(message || "Не удалось связаться с сервером")}</p>
      <div class="form__actions" style="justify-content:flex-start">
        <button class="btn btn--primary" id="tgRetryBtn">Повторить попытку</button>
      </div>
    `;
    document.getElementById("tgRetryBtn").addEventListener("click", render);
  }

  // ---------- entry point ----------

  async function render() {
    authPanel().innerHTML = `<p class="tg-hint">Проверка статуса аккаунта…</p>`;
    importPanel().hidden = true;

    let status;
    try {
      status = await API.tgStatus();
    } catch (err) {
      renderError(err.message);
      renderStatsAndActions();
      return;
    }

    if (!status.authorized) {
      if (step === "phone") renderPhoneStep();
      else if (step === "code") renderCodeStep();
      else if (step === "password") renderPasswordStep();
      renderStatsAndActions();
      return;
    }

    step = "done";
    renderConnected(status.user);
    importPanel().hidden = false;
    wireImportPanelOnce();
    await loadContacts();
  }

  return { render };
})();
