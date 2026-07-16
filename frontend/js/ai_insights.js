/* AI Insights (Personal AI Operating System) — память, таймлайн,
   дерево решений и паттерны САМОГО пользователя. Не показывает и не
   считает никаких оценок поведения контактов (это отдельно, см.
   contacts.js / Contact Intelligence). */
const AIInsights = (() => {
  const KIND_LABELS = {
    event: "Событие",
    commitment: "Договорённость",
    plan: "План",
    preference: "Предпочтение",
    fact: "Факт",
  };

  let activeTab = "memory";
  let loaded = false;

  function wire() {
    document.querySelectorAll(".ai-tabs__btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.dataset.aitab));
    });

    document.getElementById("aiMemoryForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = document.getElementById("aiMemoryInput");
      const text = input.value.trim();
      if (!text) return;
      try {
        const result = await API.aiExtractMemory(text);
        input.value = "";
        renderConflicts(result.conflicts);
        await loadMemory();
      } catch (err) {
        Utils.toast(err.message || "Не удалось разобрать запись", "error");
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
        await loadDecisions();
      } catch (err) {
        Utils.toast(err.message || "Не удалось построить разбор решения", "error");
      }
    });

    document.getElementById("btnRefreshPatterns").addEventListener("click", async () => {
      const btn = document.getElementById("btnRefreshPatterns");
      btn.disabled = true;
      btn.textContent = "Считаю…";
      try {
        const patterns = await API.aiRefreshPatterns();
        await loadPatterns();
        // Пересчёт мог реально выполниться и просто не найти
        // закономерностей (см. backend/ai_personal_engine.py —
        // analyze_patterns() требует минимум 5 записей в "Памяти").
        // Без этого тоста результат "запрос прошёл, но пусто" и
        // "кнопка ничего не сделала" выглядели на экране одинаково.
        Utils.toast(patterns.length ? "Паттерны обновлены" : "Обновлено — пока недостаточно данных");
      } catch (err) {
        Utils.toast(err.message || "Не удалось пересчитать паттерны", "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "Пересчитать паттерны";
      }
    });
  }

  function switchTab(tab) {
    activeTab = tab;
    document.querySelectorAll(".ai-tabs__btn").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.aitab === tab);
    });
    ["memory", "timeline", "decisions", "patterns", "recommendations"].forEach((t) => {
      document.getElementById(`aiPanel${cap(t)}`).hidden = t !== tab;
    });
    loadTab(tab);
  }

  function cap(s) { return s.charAt(0).toUpperCase() + s.slice(1); }

  async function render() {
    if (!loaded) {
      loaded = true;
      switchTab(activeTab);
      return;
    }
    loadTab(activeTab);
  }

  function loadTab(tab) {
    if (tab === "memory") return loadMemory();
    if (tab === "timeline") return loadTimeline();
    if (tab === "decisions") return loadDecisions();
    if (tab === "patterns") return loadPatterns();
    if (tab === "recommendations") return loadRecommendations();
  }

  function renderConflicts(conflicts) {
    const box = document.getElementById("aiMemoryConflicts");
    if (!conflicts || conflicts.length === 0) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    box.hidden = false;
    box.innerHTML = conflicts.map((c) => `<div class="ai-conflict">⚠ ${Utils.escapeHtml(c)}</div>`).join("");
  }

  async function loadMemory() {
    const list = document.getElementById("aiMemoryList");
    const items = await API.aiListMemory();
    if (items.length === 0) {
      list.innerHTML = `<div class="ai-empty">Пока ничего не запомнено. Напишите что-нибудь выше.</div>`;
      return;
    }
    list.innerHTML = items.map(memoryItemHTML).join("");
    list.querySelectorAll("[data-ai-done]").forEach((cb) => {
      cb.addEventListener("change", async () => {
        await API.aiUpdateMemory(Number(cb.dataset.aiDone), { is_done: cb.checked });
      });
    });
    list.querySelectorAll("[data-ai-delete]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await API.aiDeleteMemory(Number(btn.dataset.aiDelete));
        await loadMemory();
      });
    });
  }

  function memoryItemHTML(item) {
    const when = item.related_at ? Utils.formatDateTime(item.related_at) : "";
    const contact = item.contact_name ? `<span class="ai-item__contact">· ${Utils.escapeHtml(item.contact_name)}</span>` : "";
    const showCheckbox = item.kind === "commitment" || item.kind === "plan";
    return `
      <div class="ai-item">
        ${showCheckbox ? `<input type="checkbox" data-ai-done="${item.id}" ${item.is_done ? "checked" : ""}>` : ""}
        <div class="ai-item__body">
          <div class="ai-item__title">
            <span class="ai-item__kind ai-item__kind--${item.kind}">${KIND_LABELS[item.kind] || item.kind}</span>
            ${Utils.escapeHtml(item.title)}
          </div>
          ${item.details ? `<div class="ai-item__details">${Utils.escapeHtml(item.details)}</div>` : ""}
          <div class="ai-item__meta">${when} ${contact}</div>
        </div>
        <button class="ai-item__del" data-ai-delete="${item.id}" title="Удалить">&times;</button>
      </div>`;
  }

  async function loadTimeline() {
    const box = document.getElementById("aiTimeline");
    const entries = await API.aiTimeline();
    if (entries.length === 0) {
      box.innerHTML = `<div class="ai-empty">Таймлайн пока пуст.</div>`;
      return;
    }
    const buckets = { past: [], present: [], future: [] };
    entries.forEach((e) => buckets[e.bucket]?.push(e));
    const labels = { past: "Прошлое", present: "Сейчас", future: "Будущее" };
    box.innerHTML = ["past", "present", "future"].map((b) => `
      <div class="ai-timeline__col">
        <h3>${labels[b]}</h3>
        ${buckets[b].length === 0
          ? `<div class="ai-empty ai-empty--small">пусто</div>`
          : buckets[b].map((e) => `
            <div class="ai-timeline__entry">
              <div class="ai-timeline__title">${Utils.escapeHtml(e.title)}</div>
              ${e.at ? `<div class="ai-timeline__at">${Utils.formatDateTime(e.at)}</div>` : ""}
            </div>`).join("")}
      </div>`).join("");
  }

  async function loadDecisions() {
    const list = document.getElementById("aiDecisionList");
    const decisions = await API.aiListDecisions();
    if (decisions.length === 0) {
      list.innerHTML = `<div class="ai-empty">Пока нет разобранных решений.</div>`;
      return;
    }
    list.innerHTML = decisions.map(decisionHTML).join("");
    list.querySelectorAll("[data-choose-decision]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        await API.aiChooseDecision(Number(btn.dataset.chooseDecision), btn.dataset.chooseLabel);
        await loadDecisions();
      });
    });
  }

  function decisionHTML(d) {
    return `
      <div class="ai-decision">
        <div class="ai-decision__situation">${Utils.escapeHtml(d.situation)}</div>
        <div class="ai-decision__options">
          ${d.options.map((o) => `
            <div class="ai-decision__option ${d.chosen_option === o.label ? "is-chosen" : ""}">
              <div class="ai-decision__option-head">
                <strong>${Utils.escapeHtml(o.label)}</strong>
                ${d.chosen_option === o.label
                  ? `<span class="ai-decision__badge">выбрано</span>`
                  : `<button class="btn btn--tiny" data-choose-decision="${d.id}" data-choose-label="${Utils.escapeHtml(o.label)}">Выбрать</button>`}
              </div>
              ${listBlock("Плюсы", o.pros)}
              ${listBlock("Минусы", o.cons)}
              ${listBlock("Возможные последствия", o.consequences)}
            </div>`).join("")}
        </div>
      </div>`;
  }

  function listBlock(label, items) {
    if (!items || items.length === 0) return "";
    return `<div class="ai-decision__block"><span>${label}:</span> ${items.map(Utils.escapeHtml).join(" · ")}</div>`;
  }

  async function loadPatterns() {
    const list = document.getElementById("aiPatternList");
    const patterns = await API.aiGetPatterns();
    if (patterns.length === 0) {
      list.innerHTML = `<div class="ai-empty">Паттерны ещё не посчитаны — нажмите «Пересчитать паттерны».<br>Нужно минимум 5 записей в «Памяти», чтобы найти закономерность.</div>`;
      return;
    }
    list.innerHTML = patterns.map((p) => `
      <div class="ai-pattern">
        <div class="ai-pattern__title">${Utils.escapeHtml(p.title)}</div>
        ${p.description ? `<div class="ai-pattern__desc">${Utils.escapeHtml(p.description)}</div>` : ""}
        <div class="ai-pattern__confidence">Уверенность: ${Math.round(p.confidence * 100)}%</div>
        ${p.evidence.length ? `<div class="ai-pattern__evidence">${p.evidence.map(Utils.escapeHtml).join(" · ")}</div>` : ""}
      </div>`).join("");
  }

  async function loadRecommendations() {
    const list = document.getElementById("aiRecommendList");
    const insights = await API.aiGetInsights();
    if (insights.recommendations.length === 0) {
      list.innerHTML = `<div class="ai-empty">Пока нет рекомендаций.</div>`;
      return;
    }
    list.innerHTML = insights.recommendations.map((r) => `<div class="ai-recommend">${Utils.escapeHtml(r)}</div>`).join("");
  }

  return { wire, render };
})();
