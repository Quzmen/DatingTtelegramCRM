/* AI Workspace (Personal AI Operating System) — memory, patterns,
   timeline, recommendations and decision-tree, recomposed as a
   single scannable workspace instead of a tab-switched settings
   page. Data model / API untouched — this only changes how it's
   presented. Does not show or infer anything about contacts'
   behaviour (see contacts.js / Contact Intelligence for that,
   deliberately separate). */
const AIInsights = (() => {

  const ICONS = {
    spark: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/></svg>`,
    calendar: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4M16 3v4M3 10h18"/></svg>`,
    handshake: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 12l3 3 6-6"/><circle cx="12" cy="12" r="9"/></svg>`,
    flag: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3v18"/><path d="M5 4h11l-2 4 2 4H5"/></svg>`,
    heart: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20s-7-4.4-9.5-8.7C.6 8 2 4.5 5.6 4A5 5 0 0 1 12 7a5 5 0 0 1 6.4-3c3.6.5 5 4 3.1 7.3C19 15.6 12 20 12 20z"/></svg>`,
    info: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/></svg>`,
    bulb: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.5.4.9 1 .9 1.7V16h5.4v-.5c0-.7.4-1.3.9-1.7A6 6 0 0 0 12 3z"/></svg>`,
    check: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12l5 5L20 6"/></svg>`,
    chevron: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>`,
    refresh: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg>`,
    branch: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="12" r="2.5"/><path d="M6 8.5V15.5M8 6.8l7.5 4M8 17.2l7.5-4"/></svg>`,
  };

  const KIND_META = {
    event:      { label: "Событие",        color: "var(--primary)", tint: "var(--primary-tint)", icon: ICONS.calendar },
    commitment: { label: "Договорённость", color: "var(--rose)",    tint: "var(--rose-tint)",    icon: ICONS.handshake },
    plan:       { label: "План",           color: "var(--amber)",   tint: "var(--amber-tint)",   icon: ICONS.flag },
    preference: { label: "Предпочтение",   color: "var(--purple)",  tint: "var(--purple-tint)",  icon: ICONS.heart },
    fact:       { label: "Факт",           color: "var(--slate)",   tint: "var(--slate-tint)",   icon: ICONS.info },
  };

  let wired = false;
  let allMemory = [];
  let activeFilter = "all";
  let cachedDecisions = null;

  /* ---------------------------------------------------------------
     Wiring
     --------------------------------------------------------------- */
  function wire() {
    if (wired) return;
    wired = true;

    const input = document.getElementById("aiMemoryInput");
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 160) + "px";
    });

    document.getElementById("aiMemoryForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      const submitBtn = document.getElementById("aiMemorySubmit");
      submitBtn.disabled = true;
      try {
        const result = await API.aiExtractMemory(text);
        input.value = "";
        input.style.height = "auto";
        renderConflicts(result.conflicts);
        await Promise.all([loadOverview(), loadMemory(), loadTimeline()]);
      } catch (err) {
        Utils.toast(err.message || "Не удалось разобрать запись", "error");
      } finally {
        submitBtn.disabled = false;
      }
    });

    document.getElementById("aiDecisionForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const situation = document.getElementById("aiDecisionSituation").value.trim();
      const options = document.getElementById("aiDecisionOptions").value
        .split("\n").map((s) => s.trim()).filter(Boolean);
      if (!situation || options.length === 0) {
        Utils.toast("Опишите ситуацию и хотя бы один вариант", "error");
        return;
      }
      try {
        await API.aiCreateDecision(situation, options);
        document.getElementById("aiDecisionForm").reset();
        cachedDecisions = null;
        await loadDecisions();
      } catch (err) {
        Utils.toast(err.message || "Не удалось построить разбор решения", "error");
      }
    });

    document.getElementById("btnRefreshPatterns").addEventListener("click", async () => {
      const btn = document.getElementById("btnRefreshPatterns");
      btn.classList.add("is-spinning");
      btn.disabled = true;
      try {
        const patterns = await API.aiRefreshPatterns();
        renderPatterns(patterns);
        Utils.toast(patterns.length ? "Паттерны обновлены" : "Обновлено — пока недостаточно данных");
      } catch (err) {
        Utils.toast(err.message || "Не удалось пересчитать паттерны", "error");
      } finally {
        btn.classList.remove("is-spinning");
        btn.disabled = false;
      }
    });

    // Decisions drawer open/close
    document.getElementById("btnOpenDecision").addEventListener("click", openDrawer);
    document.getElementById("btnOpenDecisionFab").addEventListener("click", openDrawer);
    document.getElementById("btnCloseDecision").addEventListener("click", closeDrawer);
    document.getElementById("aiDecisionBackdrop").addEventListener("click", closeDrawer);

    // Memory filter chips (delegated, chips are re-rendered but container is stable)
    document.getElementById("aiwMemoryFilters").addEventListener("click", (e) => {
      const chip = e.target.closest("[data-filter]");
      if (!chip) return;
      activeFilter = chip.dataset.filter;
      renderMemoryFilters();
      renderMemoryList();
    });

    // Memory list delegation: expand, checkbox, delete
    document.getElementById("aiMemoryList").addEventListener("click", async (e) => {
      const del = e.target.closest("[data-ai-delete]");
      if (del) {
        e.stopPropagation();
        await API.aiDeleteMemory(Number(del.dataset.aiDelete));
        allMemory = allMemory.filter((m) => m.id !== Number(del.dataset.aiDelete));
        renderMemoryList();
        loadOverview();
        return;
      }
      const check = e.target.closest("[data-ai-done]");
      if (check) {
        e.stopPropagation();
        return; // handled by change listener below
      }
      const card = e.target.closest(".aiw-mcard");
      if (card) card.classList.toggle("is-open");
    });

    document.getElementById("aiMemoryList").addEventListener("change", async (e) => {
      const check = e.target.closest("[data-ai-done]");
      if (!check) return;
      const id = Number(check.dataset.aiDone);
      await API.aiUpdateMemory(id, { is_done: check.checked });
      const item = allMemory.find((m) => m.id === id);
      if (item) item.is_done = check.checked;
      const card = check.closest(".aiw-mcard");
      if (card) card.classList.toggle("is-done", check.checked);
      loadOverview();
    });

    // Pattern list delegation: expand evidence
    document.getElementById("aiPatternList").addEventListener("click", (e) => {
      const card = e.target.closest(".aiw-pcard");
      if (card) card.classList.toggle("is-open");
    });

    // Decision list delegation: choose option
    document.getElementById("aiDecisionList").addEventListener("click", async (e) => {
      const chooseBtn = e.target.closest("[data-choose-decision]");
      if (chooseBtn) {
        await API.aiChooseDecision(Number(chooseBtn.dataset.chooseDecision), chooseBtn.dataset.chooseLabel);
        cachedDecisions = null;
        await loadDecisions();
        return;
      }
      const deleteBtn = e.target.closest("[data-delete-decision]");
      if (deleteBtn) {
        await API.aiDeleteDecision(Number(deleteBtn.dataset.deleteDecision));
        cachedDecisions = null;
        await loadDecisions();
      }
    });
  }

  function openDrawer() {
    document.getElementById("aiDecisionDrawer").hidden = false;
    requestAnimationFrame(() => document.getElementById("aiDecisionDrawer").classList.add("is-open"));
    loadDecisions();
  }

  function closeDrawer() {
    const drawer = document.getElementById("aiDecisionDrawer");
    drawer.classList.remove("is-open");
    setTimeout(() => { drawer.hidden = true; }, 280);
  }

  /* ---------------------------------------------------------------
     Master render — the page is no longer tab-switched, so every
     section loads together whenever the workspace is opened.
     --------------------------------------------------------------- */
  async function render() {
    wire();
    await Promise.all([
      loadOverview(),
      loadMemory(),
      loadPatterns(),
      loadTimeline(),
      loadDecisionBadge(),
    ]);
  }

  function renderConflicts(conflicts) {
    const box = document.getElementById("aiMemoryConflicts");
    if (!conflicts || conflicts.length === 0) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = conflicts.map((c) => `<div class="aiw-conflict">⚠ ${Utils.escapeHtml(c)}</div>`).join("");
  }

  /* ---------------------------------------------------------------
     AIOverview
     --------------------------------------------------------------- */
  async function loadOverview() {
    const insights = await API.aiGetInsights();
    renderStats(insights);
    renderHighlight(insights);
    renderRecommendations(insights.recommendations);
  }

  function renderStats(insights) {
    const box = document.getElementById("aiwStats");
    const stats = [
      { value: insights.memory_count, label: "записей в памяти" },
      { value: insights.open_commitments, label: "открытых договорённостей", cls: insights.open_commitments > 0 ? "aiw-stat--warn" : "aiw-stat--ok" },
      { value: insights.patterns.length, label: "найденных паттернов" },
    ];
    box.innerHTML = stats.map((s) => `
      <div class="aiw-stat ${s.cls || ""}">
        <div class="aiw-stat__value">${s.value}</div>
        <div class="aiw-stat__label">${s.label}</div>
      </div>`).join("");
  }

  function renderHighlight(insights) {
    let el = document.getElementById("aiwHighlight");
    if (!el) {
      el = document.createElement("div");
      el.id = "aiwHighlight";
      el.className = "aiw-hero__highlight";
      document.getElementById("aiwHero").appendChild(el);
    }
    let text;
    if (insights.recommendations.length) {
      text = insights.recommendations[0];
    } else if (insights.patterns.length) {
      text = `Самый уверенный паттерн: «${insights.patterns[0].title}»`;
    } else {
      text = "Пока всё спокойно — новых сигналов нет.";
    }
    el.innerHTML = `${ICONS.bulb}<span>${Utils.escapeHtml(text)}</span>`;
  }

  function renderRecommendations(list) {
    const box = document.getElementById("aiRecommendList");
    if (!list || list.length === 0) {
      box.innerHTML = `<div class="aiw-empty aiw-empty--tiny">Пока нет рекомендаций.</div>`;
      return;
    }
    box.innerHTML = list.map((r) => `<div class="aiw-reco">${ICONS.bulb}<span>${Utils.escapeHtml(r)}</span></div>`).join("");
  }

  /* ---------------------------------------------------------------
     MemoryCard grid
     --------------------------------------------------------------- */
  const FILTERS = [
    { key: "all", label: "Все" },
    { key: "event", label: "События" },
    { key: "commitment", label: "Договорённости" },
    { key: "plan", label: "Планы" },
    { key: "preference", label: "Предпочтения" },
    { key: "fact", label: "Факты" },
  ];

  function renderMemoryFilters() {
    const box = document.getElementById("aiwMemoryFilters");
    box.innerHTML = FILTERS.map((f) => `
      <button type="button" class="aiw-chip ${activeFilter === f.key ? "is-active" : ""}" data-filter="${f.key}">${f.label}</button>
    `).join("");
  }

  async function loadMemory() {
    allMemory = await API.aiListMemory();
    if (!document.getElementById("aiwMemoryFilters").children.length) renderMemoryFilters();
    renderMemoryList();
  }

  function renderMemoryList() {
    const list = document.getElementById("aiMemoryList");
    const items = activeFilter === "all" ? allMemory : allMemory.filter((m) => m.kind === activeFilter);
    if (items.length === 0) {
      list.innerHTML = `<div class="aiw-empty">Пока ничего не запомнено. Опишите что-нибудь в строке выше.</div>`;
      return;
    }
    list.innerHTML = items.map(memoryCardHTML).join("");
  }

  function memoryCardHTML(item) {
    const meta = KIND_META[item.kind] || KIND_META.fact;
    const when = item.related_at ? Utils.formatDateTime(item.related_at) : "";
    const contact = item.contact_name ? Utils.escapeHtml(item.contact_name) : "";
    const showCheckbox = item.kind === "commitment" || item.kind === "plan";
    const hasDetail = !!item.details;
    return `
      <div class="aiw-mcard ${item.is_done ? "is-done" : ""}">
        <div class="aiw-mcard__row">
          ${showCheckbox ? `<input type="checkbox" class="aiw-mcard__check" data-ai-done="${item.id}" ${item.is_done ? "checked" : ""}>` : `<span class="aiw-mcard__check" style="width:17px"></span>`}
          <span class="aiw-mcard__kind" style="background:${meta.tint};color:${meta.color}">${meta.icon}</span>
          <div class="aiw-mcard__body">
            <div class="aiw-mcard__title">
              <span class="aiw-mcard__kindlabel" style="color:${meta.color}">${meta.label}</span>
              <span class="aiw-mcard__title-text">${Utils.escapeHtml(item.title)}</span>
            </div>
            <div class="aiw-mcard__meta">${when ? `<span>${when}</span>` : ""}${contact ? `<span>· ${contact}</span>` : ""}</div>
          </div>
          ${hasDetail ? `<span class="aiw-mcard__chevron">${ICONS.chevron}</span>` : ""}
          <button class="aiw-mcard__del" data-ai-delete="${item.id}" title="Удалить">&times;</button>
        </div>
        ${hasDetail ? `
          <div class="aiw-mcard__detail">
            <div class="aiw-mcard__detail-inner">
              <div class="aiw-mcard__detail-text">${Utils.escapeHtml(item.details)}</div>
            </div>
          </div>` : ""}
      </div>`;
  }

  /* ---------------------------------------------------------------
     PatternCard
     --------------------------------------------------------------- */
  async function loadPatterns() {
    const patterns = await API.aiGetPatterns();
    renderPatterns(patterns);
  }

  function renderPatterns(patterns) {
    const list = document.getElementById("aiPatternList");
    if (!patterns || patterns.length === 0) {
      list.innerHTML = `<div class="aiw-empty aiw-empty--tiny">Паттерны ещё не посчитаны — нажмите «пересчитать» вверху панели.<br>Нужно минимум 5 записей в «Памяти».</div>`;
      return;
    }
    list.innerHTML = patterns.map((p) => {
      const pct = Math.round((p.confidence || 0) * 100);
      return `
      <div class="aiw-pcard">
        <div class="aiw-pcard__title">${Utils.escapeHtml(p.title)}</div>
        ${p.description ? `<div class="aiw-pcard__desc">${Utils.escapeHtml(p.description)}</div>` : ""}
        <div class="aiw-pcard__conf">
          <div class="aiw-pcard__conf-track"><div class="aiw-pcard__conf-fill" style="width:${pct}%"></div></div>
          <span class="aiw-pcard__conf-label">${pct}%</span>
        </div>
        ${p.evidence && p.evidence.length ? `
          <div class="aiw-pcard__evidence">
            <div class="aiw-pcard__evidence-inner">
              <div class="aiw-pcard__evidence-list">
                ${p.evidence.map((e) => `<div class="aiw-pcard__evidence-item">${Utils.escapeHtml(e)}</div>`).join("")}
              </div>
            </div>
          </div>` : ""}
      </div>`;
    }).join("");
  }

  /* ---------------------------------------------------------------
     TimelineView
     --------------------------------------------------------------- */
  async function loadTimeline() {
    const entries = await API.aiTimeline();
    renderTimeline(entries);
  }

  function renderTimeline(entries) {
    const box = document.getElementById("aiTimeline");
    if (!entries || entries.length === 0) {
      box.innerHTML = `<div class="aiw-empty">Таймлайн пока пуст.</div>`;
      return;
    }
    const buckets = { past: [], present: [], future: [] };
    entries.forEach((e) => buckets[e.bucket]?.push(e));
    const labels = { past: "Прошлое", present: "Сейчас", future: "Будущее" };
    box.innerHTML = ["past", "present", "future"].map((b) => `
      <div class="aiw-tcol aiw-tcol--${b}">
        <span class="aiw-tcol__node"></span>
        <h3>${labels[b]}</h3>
        ${buckets[b].length === 0
          ? `<div class="aiw-empty aiw-empty--tiny">пусто</div>`
          : buckets[b].map((e, i) => `
            <div class="aiw-tentry" style="animation-delay:${i * 0.04}s">
              <div class="aiw-tentry__title">${Utils.escapeHtml(e.title)}</div>
              ${e.at ? `<div class="aiw-tentry__at">${Utils.formatDateTime(e.at)}</div>` : ""}
            </div>`).join("")}
      </div>`).join("");
  }

  /* ---------------------------------------------------------------
     Decisions drawer
     --------------------------------------------------------------- */
  async function loadDecisionBadge() {
    const decisions = await API.aiListDecisions();
    cachedDecisions = decisions;
    const badge = document.getElementById("aiwDecisionCount");
    badge.textContent = decisions.length ? decisions.length : "";
    badge.hidden = decisions.length === 0;
  }

  async function loadDecisions() {
    const list = document.getElementById("aiDecisionList");
    const decisions = cachedDecisions || await API.aiListDecisions();
    cachedDecisions = decisions;
    const badge = document.getElementById("aiwDecisionCount");
    badge.textContent = decisions.length ? decisions.length : "";
    badge.hidden = decisions.length === 0;
    if (decisions.length === 0) {
      list.innerHTML = `<div class="aiw-empty">Пока нет разобранных решений. Опишите ситуацию выше — AI разложит плюсы, минусы и вероятные последствия каждого варианта.</div>`;
      return;
    }
    list.innerHTML = decisions.map(decisionCardHTML).join("");
  }

  function decisionCardHTML(d) {
    return `
      <div class="aiw-dcard">
        <div class="aiw-dcard__head">
          <div class="aiw-dcard__situation">${Utils.escapeHtml(d.situation)}</div>
          <button class="aiw-dcard__delete" data-delete-decision="${d.id}" title="Удалить">×</button>
        </div>
        ${d.options.map((o) => `
          <div class="aiw-dcard__option ${d.chosen_option === o.label ? "is-chosen" : ""}">
            <div class="aiw-dcard__option-head">
              <strong>${Utils.escapeHtml(o.label)}</strong>
              ${d.chosen_option === o.label
                ? `<span class="aiw-dcard__badge">${ICONS.check} выбрано</span>`
                : `<button class="aiw-dcard__choose" data-choose-decision="${d.id}" data-choose-label="${Utils.escapeHtml(o.label)}">Выбрать</button>`}
            </div>
            ${decisionBlock("Плюсы", o.pros)}
            ${decisionBlock("Минусы", o.cons)}
            ${decisionBlock("Возможные последствия", o.consequences)}
          </div>`).join("")}
      </div>`;
  }

  function decisionBlock(label, items) {
    if (!items || items.length === 0) return "";
    return `<div class="aiw-dcard__block"><span>${label}:</span> ${items.map(Utils.escapeHtml).join(" · ")}</div>`;
  }

  return { wire, render };
})();
