/* Точка входа: переключение видов и инициализация модулей. */
const App = (() => {
  const views = ["dashboard", "chat", "contacts", "kanban", "telegram", "campaigns"];

  function switchView(name) {
    views.forEach((v) => {
      document.getElementById(`view-${v}`).hidden = v !== name;
    });
    document.querySelectorAll(".railnav__btn[data-view]").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.view === name);
    });

    if (name === "dashboard") Dashboard.render();
    if (name === "chat") { ChatView.render(); Folders.load(); }
    if (name === "kanban") Kanban.render();
    if (name === "telegram") Telegram.render();
    if (name === "campaigns") Campaigns.render();
    if (name !== "contacts") Contacts.stopChatPolling();
    if (name !== "chat") ChatView.stopPolling();
    if (name !== "campaigns") Campaigns.stopPolling();
  }

  async function goToContact(id) {
    switchView("contacts");
    await Contacts.reload();
    Contacts.selectContact(id);
  }

  function wireNav() {
    document.querySelectorAll(".railnav__btn[data-view]").forEach((btn) => {
      btn.addEventListener("click", () => switchView(btn.dataset.view));
    });
    document.getElementById("btnAttention").addEventListener("click", () => switchView("dashboard"));

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
    await Contacts.init();
    await Dashboard.render();
    switchView("dashboard");
  }

  return { switchView, goToContact, init };
})();

document.addEventListener("DOMContentLoaded", App.init);
