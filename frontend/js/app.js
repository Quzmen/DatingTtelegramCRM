/* Точка входа: переключение видов и инициализация модулей. */
const App = (() => {
  const views = ["dashboard", "chat", "contacts", "telegram", "campaigns", "ai"];
  let contactsMode = "table"; // "table" | "kanban" — CRM sub-view, not a top-level nav item

  function switchView(name) {
    views.forEach((v) => {
      document.getElementById(`view-${v}`).hidden = v !== name;
    });
    document.querySelectorAll("[data-view].railnav__btn, [data-view].railnav__mark").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.view === name);
    });

    if (name === "dashboard") Dashboard.render();
    if (name === "chat") { ChatView.render(); Folders.load(); }
    if (name === "contacts" && contactsMode === "kanban") Kanban.render();
    if (name === "telegram") Telegram.render();
    if (name === "campaigns") Campaigns.render();
    if (name === "ai") AIInsights.render();
    if (name !== "contacts") Contacts.stopChatPolling();
    if (name !== "chat") ChatView.stopPolling();
    if (name !== "campaigns") Campaigns.stopPolling();
  }

  // ---- CRM sub-view toggle: Table (contacts list + detail) vs Kanban ----
  function setContactsMode(mode) {
    contactsMode = mode;
    document.getElementById("contactsBodyTable").hidden = mode !== "table";
    document.getElementById("contactsBodyKanban").hidden = mode !== "kanban";
    document.querySelectorAll("#contactsModeToggle .seg-toggle__btn").forEach((btn) => {
      const active = btn.dataset.mode === mode;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    if (mode === "kanban") Kanban.render();
  }

  function wireContactsModeToggle() {
    document.querySelectorAll("#contactsModeToggle .seg-toggle__btn").forEach((btn) => {
      btn.addEventListener("click", () => setContactsMode(btn.dataset.mode));
    });
  }

  async function goToContact(id) {
    switchView("contacts");
    setContactsMode("table");
    await Contacts.reload();
    Contacts.selectContact(id);
  }

  function wireNav() {
    document.querySelectorAll("[data-view].railnav__btn, [data-view].railnav__mark").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.dataset.view));
    });
    document.getElementById("btnAttention").addEventListener("click", () => switchView("dashboard"));
    wireContactsModeToggle();

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        document.getElementById("modalNewContact").hidden = true;
        document.getElementById("modalForward").hidden = true;
      }
    });
  }

  async function init() {
    wireNav();
    Folders.wire();
    AIInsights.wire();
    await Contacts.init();
    await Dashboard.render();
    switchView("dashboard");
  }

  return { switchView, goToContact, init, setContactsMode };
})();

document.addEventListener("DOMContentLoaded", () => {
  // Точка входа — Auth.init() (см. js/auth.js): она сама решает,
  // показать ли экран логина, или (если cookie crm_session ещё
  // валиден — автологин) сразу запустить App.init().
  //
  // ВАЖНО: auth.js подключён в index.html раньше app.js, так что Auth
  // на этот момент уже гарантированно объявлен — вызываем его напрямую.
  // (Раньше здесь была проверка `if (window.Auth)`, которая всегда была
  // ложной: `const Auth = (() => {...})()` на верхнем уровне обычного
  // <script> создаёт имя в глобальной лексической области видимости,
  // но НЕ свойство объекта window — поэтому проверка молча проваливалась
  // и код всегда уходил в App.init() напрямую, минуя логин.)
  Auth.init();
});
