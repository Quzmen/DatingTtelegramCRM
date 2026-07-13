/* Канбан-доска с перетаскиванием карточек между этапами. */
const Kanban = (() => {
  let draggedId = null;

  async function render() {
    const statuses = await API.getStatuses();
    const all = await API.listContacts({ sort: "-updated_at" });

    const board = document.getElementById("kanbanBoard");
    board.innerHTML = statuses
      .map((s) => {
        const colItems = all.filter((c) => c.status === s.value);
        return `
        <div class="kanban-col" data-status="${s.value}">
          <div class="kanban-col__head">
            <span class="dot" style="background:var(--primary);width:7px;height:7px;border-radius:50%;display:inline-block"></span>
            <span class="kanban-col__title">${s.label}</span>
            <span class="kanban-col__count">${colItems.length}</span>
          </div>
          <div class="kanban-col__items" data-status="${s.value}">
            ${colItems.map(cardHTML).join("")}
          </div>
        </div>`;
      })
      .join("");

    wireDragAndDrop();
    wireCardClicks();
  }

  function cardHTML(c) {
    return `
      <div class="kcard" draggable="true" data-id="${c.id}">
        <div class="kcard__top">
          <span class="kcard__name">${Utils.escapeHtml(c.name)}</span>
        </div>
        ${c.username ? `<div class="ccard__uname">${Utils.escapeHtml(c.username)}</div>` : ""}
        <div class="kcard__meta">
          <span class="kcard__interest">интерес ${c.interest_level}/10</span>
          <span class="badge status-${c.status}" style="padding:2px 6px"><span class="dot"></span></span>
        </div>
        ${c.next_task ? `<div class="kcard__task">→ ${Utils.escapeHtml(c.next_task)}</div>` : ""}
      </div>`;
  }

  function wireCardClicks() {
    document.querySelectorAll(".kcard").forEach((card) => {
      card.addEventListener("click", () => {
        if (card.classList.contains("is-dragging")) return;
        Contacts.goToContact(Number(card.dataset.id));
      });
    });
  }

  function wireDragAndDrop() {
    document.querySelectorAll(".kcard").forEach((card) => {
      card.addEventListener("dragstart", () => {
        draggedId = Number(card.dataset.id);
        card.classList.add("is-dragging");
      });
      card.addEventListener("dragend", () => {
        card.classList.remove("is-dragging");
      });
    });

    document.querySelectorAll(".kanban-col").forEach((col) => {
      col.addEventListener("dragover", (e) => {
        e.preventDefault();
        col.classList.add("is-dragover");
      });
      col.addEventListener("dragleave", () => col.classList.remove("is-dragover"));
      col.addEventListener("drop", async (e) => {
        e.preventDefault();
        col.classList.remove("is-dragover");
        if (draggedId == null) return;
        const newStatus = col.dataset.status;
        await API.updateStatus(draggedId, newStatus);
        Utils.toast("Статус обновлён");
        draggedId = null;
        await render();
        await Dashboard.render();
      });
    });
  }

  return { render };
})();
