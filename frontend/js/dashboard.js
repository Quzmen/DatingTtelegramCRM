/* Отрисовка главной панели (Dashboard). */
const Dashboard = (() => {
  async function render() {
    const [stats, reminders] = await Promise.all([API.getDashboard(), API.getReminders()]);

    renderStatGrid(stats);
    renderStageBars(stats.by_status, stats.total_contacts);
    renderAttentionList(reminders);

    // badge on the rail nav icon
    const badge = document.getElementById("attentionBadge");
    if (stats.needs_attention > 0) {
      badge.hidden = false;
      badge.textContent = stats.needs_attention;
    } else {
      badge.hidden = true;
    }
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

  return { render };
})();
