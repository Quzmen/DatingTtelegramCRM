/* Отрисовка главной панели (Dashboard). */
const Dashboard = (() => {
  function renderGreeting() {
    const el = document.getElementById("dashGreeting");
    if (!el) return;
    const h = new Date().getHours();
    const text =
      h < 5 ? "Доброй ночи" : h < 12 ? "Доброе утро" : h < 18 ? "Добрый день" : "Добрый вечер";
    el.textContent = text;
  }

  async function render() {
    renderGreeting();
    const [stats, reminders, recentContacts, campaigns] = await Promise.all([
      API.getDashboard(),
      API.getReminders(),
      API.listContacts({}).catch(() => []),
      API.listCampaigns().catch(() => []),
    ]);

    renderStatGrid(stats);
    renderStageBars(stats.by_status, stats.total_contacts);
    renderAttentionList(reminders);
    renderRecentContacts(recentContacts);
    renderCampaignsMini(campaigns);

    // badge on the rail nav icon
    const badge = document.getElementById("attentionBadge");
    if (stats.needs_attention > 0) {
      badge.hidden = false;
      badge.textContent = stats.needs_attention;
    } else {
      badge.hidden = true;
    }
  }

  function wireQuickActions() {
    const btnNewContact = document.getElementById("btnDashNewContact");
    const btnNewCampaign = document.getElementById("btnDashNewCampaign");
    const btnOpenAI = document.getElementById("btnDashOpenAI");
    if (btnNewContact) btnNewContact.addEventListener("click", () => (document.getElementById("modalNewContact").hidden = false));
    if (btnOpenAI) btnOpenAI.addEventListener("click", () => App.switchView("ai"));
    if (btnNewCampaign) btnNewCampaign.addEventListener("click", () => App.switchView("campaigns"));
  }

  function renderRecentContacts(list) {
    const el = document.getElementById("recentContactsList");
    if (!el) return;
    const items = (list || []).slice(0, 6);
    if (!items.length) {
      el.innerHTML = `<div class="empty-hint">Пока нет контактов — добавьте первого.</div>`;
      return;
    }
    el.innerHTML = items
      .map(
        (c) => `
      <div class="recent-row" data-id="${c.id}">
        <div class="recent-row__avatar">${Utils.escapeHtml((c.name || "?").slice(0, 1).toUpperCase())}</div>
        <div class="recent-row__main">
          <div class="recent-row__name">${Utils.escapeHtml(c.name)}</div>
          <div class="recent-row__status">${Utils.escapeHtml(c.status_label || c.status || "")}</div>
        </div>
      </div>`
      )
      .join("");
    el.querySelectorAll(".recent-row").forEach((row) => {
      row.addEventListener("click", () => App.goToContact(Number(row.dataset.id)));
    });
  }

  function renderCampaignsMini(list) {
    const el = document.getElementById("campaignsMini");
    if (!el) return;
    const items = list || [];
    if (!items.length) {
      el.innerHTML = `<div class="empty-hint">Кампаний ещё нет — запустите первую рассылку.</div>`;
      return;
    }
    const active = items.filter((c) => c.status === "running" || c.status === "in_progress").length;
    const sorted = [...items].sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0)).slice(0, 3);
    el.innerHTML = `
      <div class="campaigns-mini__summary">
        <span class="campaigns-mini__count">${active}</span> активных из ${items.length}
      </div>
      <div class="campaigns-mini__list">
        ${sorted
          .map(
            (c) => `
          <div class="campaigns-mini__row" data-id="${c.id}">
            <span class="campaigns-mini__name">${Utils.escapeHtml(c.name || "Без названия")}</span>
            <span class="campaigns-mini__status campaigns-mini__status--${c.status || "draft"}">${Utils.escapeHtml(c.status || "draft")}</span>
          </div>`
          )
          .join("")}
      </div>`;
    el.querySelectorAll(".campaigns-mini__row").forEach((row) => {
      row.addEventListener("click", () => App.switchView("campaigns"));
    });
  }

  function renderStatGrid(stats) {
    const grid = document.getElementById("statGrid");
    const cards = [
      { label: "Всего контактов", value: stats.total_contacts, cls: "" },
      { label: "Новые за неделю", value: stats.new_this_week, cls: "accent-primary" },
      { label: "Активные диалоги", value: stats.active_dialogues, cls: "accent-teal" },
      { label: "Требуют внимания", value: stats.needs_attention, cls: "accent-amber" },
    ];
    grid.innerHTML = cards
      .map(
        (c) => `
      <div class="stat-card">
        <div class="stat-card__label">${c.label}</div>
        <div class="stat-card__value ${c.cls}">${c.value}</div>
      </div>`
      )
      .join("");
  }

  function renderStageBars(byStatus, total) {
    const el = document.getElementById("stageBars");
    el.innerHTML = byStatus
      .map((s) => {
        const pct = total ? Math.round((s.count / total) * 100) : 0;
        return `
        <div class="stage-bar">
          <div class="stage-bar__label">${s.label}</div>
          <div class="stage-bar__track"><div class="stage-bar__fill" style="width:${pct}%"></div></div>
          <div class="stage-bar__count">${s.count}</div>
        </div>`;
      })
      .join("");
  }

  function renderAttentionList(list) {
    const el = document.getElementById("attentionList");
    if (!list.length) {
      el.innerHTML = `<div class="empty-hint">Все контакты в порядке — никто не забыт.</div>`;
      return;
    }
    el.innerHTML = list
      .slice(0, 8)
      .map(
        (r) => `
      <div class="attn-row" data-id="${r.contact_id}">
        <div>
          <div class="attn-row__name">${Utils.escapeHtml(r.name)}</div>
          <div class="attn-row__text">${Utils.escapeHtml(r.text)}</div>
        </div>
        <div class="attn-row__meta">${r.days_since_contact == null ? "" : (r.days_since_contact === 0 ? "сегодня" : r.days_since_contact === 1 ? "вчера" : r.days_since_contact + " дн. назад")}</div>
      </div>`
      )
      .join("");

    el.querySelectorAll(".attn-row").forEach((row) => {
      row.addEventListener("click", () => {
        App.goToContact(Number(row.dataset.id));
      });
    });
  }

  wireQuickActions();

  return { render };
})();
