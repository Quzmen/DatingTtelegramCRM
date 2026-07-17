/*
 * Вход в CRM через Telegram (см. backend/routers/auth.py, /api/auth/*).
 *
 * Единственная точка входа в приложение: Auth.init() решает, показать
 * ли экран логина (send-code -> sign-in) или, если cookie crm_session
 * уже валиден (см. GET /api/auth/me), сразу пропустить пользователя и
 * запустить App.init() — это и есть "автологин по crm_session" из ТЗ,
 * никакого отдельного шага не нужно: браузер сам отправит httponly
 * cookie с каждым запросом.
 *
 * Также подписан на API.request() как обработчик 401 "на лету" — если
 * сессия CRM истечёт прямо во время работы (например, TTL cookie или
 * запись в user_sessions удалена вручную), пользователь сразу увидит
 * экран входа вместо серии непонятных ошибок по всему интерфейсу
 * (см. api.js: request() зовёт Auth.handleUnauthorized() на 401).
 */
const Auth = (() => {
  let currentUser = null;
  let pendingPhone = null;
  let appStarted = false;

  function els() {
    return {
      screen: document.getElementById("loginScreen"),
      appRoot: document.querySelector(".app"),
      phoneStep: document.getElementById("loginStepPhone"),
      codeStep: document.getElementById("loginStepCode"),
      phoneInput: document.getElementById("loginPhone"),
      codeInput: document.getElementById("loginCode"),
      passwordInput: document.getElementById("loginPassword"),
      passwordRow: document.getElementById("loginPasswordRow"),
      error: document.getElementById("loginError"),
      phoneLabel: document.getElementById("loginPhoneLabel"),
    };
  }

  function showError(msg) {
    const { error } = els();
    if (!error) return;
    error.textContent = msg || "";
    error.hidden = !msg;
  }

  function resetToPhoneStep() {
    const { phoneStep, codeStep, passwordRow, codeInput, passwordInput } = els();
    phoneStep.hidden = false;
    codeStep.hidden = true;
    passwordRow.hidden = true;
    codeInput.value = "";
    passwordInput.value = "";
    pendingPhone = null;
    showError("");
  }

  function showLogin() {
    const { screen, appRoot } = els();
    if (screen) screen.hidden = false;
    if (appRoot) appRoot.hidden = true;
    resetToPhoneStep();
  }

  function hideLogin() {
    const { screen, appRoot } = els();
    if (screen) screen.hidden = true;
    if (appRoot) appRoot.hidden = false;
  }

  async function sendCode() {
    const { phoneInput, phoneStep, codeStep, phoneLabel, codeInput } = els();
    const phone = (phoneInput.value || "").trim();
    if (!phone) {
      showError("Введите номер телефона");
      return;
    }
    showError("");
    try {
      await API.authSendCode(phone);
      pendingPhone = phone;
      phoneLabel.textContent = phone;
      phoneStep.hidden = true;
      codeStep.hidden = false;
      codeInput.focus();
    } catch (e) {
      showError(e.message || "Не удалось отправить код");
    }
  }

  async function submitCode() {
    const { codeInput, passwordInput, passwordRow } = els();
    if (!pendingPhone) {
      resetToPhoneStep();
      return;
    }
    showError("");
    try {
      const result = await API.authSignIn({
        phone: pendingPhone,
        code: (codeInput.value || "").trim(),
        password: passwordInput.value || null,
      });
      if (result.needs_password) {
        passwordRow.hidden = false;
        passwordInput.focus();
        return;
      }
      if (result.authorized) {
        currentUser = result.user;
        await onAuthenticated();
      }
    } catch (e) {
      showError(e.message || "Неверный код, попробуйте снова");
    }
  }

  function renderUserMenu() {
    const nameEl = document.getElementById("userMenuName");
    const phoneEl = document.getElementById("userMenuPhone");
    const initialsEl = document.getElementById("userMenuInitials");
    if (!currentUser) return;
    const displayName = currentUser.name || currentUser.username || currentUser.phone || "Пользователь";
    if (nameEl) nameEl.textContent = displayName;
    if (phoneEl) phoneEl.textContent = currentUser.phone || (currentUser.username ? "@" + currentUser.username : "");
    if (initialsEl && window.Utils) initialsEl.textContent = Utils.initials(displayName);
  }

  function toggleUserMenu(force) {
    const dropdown = document.getElementById("userMenuDropdown");
    if (!dropdown) return;
    const willShow = force !== undefined ? force : dropdown.hidden;
    dropdown.hidden = !willShow;
  }

  async function logout() {
    toggleUserMenu(false);
    try {
      await API.authLogout();
    } catch (_) {
      // Даже если запрос не удался (например, сессии уже нет), всё
      // равно возвращаем пользователя на экран входа.
    }
    currentUser = null;
    appStarted = false;
    location.reload();
  }

  // Вызывается из api.js при любом 401 от бэкенда, кроме самих
  // /auth/* эндпоинтов (иначе неверный код на экране логина тоже
  // считался бы "сессия истекла" и зациклил бы форму).
  function handleUnauthorized() {
    if (!appStarted) return; // ещё не входили — и так уже на экране логина
    currentUser = null;
    appStarted = false;
    showLogin();
    showError("Сессия истекла, войдите заново");
  }

  async function onAuthenticated() {
    hideLogin();
    renderUserMenu();
    if (!appStarted) {
      appStarted = true;
      await App.init();
    }
  }

  function wire() {
    document.getElementById("btnLoginSendCode").addEventListener("click", sendCode);
    document.getElementById("loginPhone").addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendCode();
    });
    document.getElementById("btnLoginSubmitCode").addEventListener("click", submitCode);
    document.getElementById("loginCode").addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitCode();
    });
    document.getElementById("loginPassword").addEventListener("keydown", (e) => {
      if (e.key === "Enter") submitCode();
    });
    document.getElementById("btnLoginBack").addEventListener("click", resetToPhoneStep);

    document.getElementById("userMenuBtn").addEventListener("click", (e) => {
      e.stopPropagation();
      toggleUserMenu();
    });
    document.getElementById("btnLogout").addEventListener("click", logout);
    document.addEventListener("click", (e) => {
      const menu = document.getElementById("userMenu");
      if (menu && !menu.contains(e.target)) toggleUserMenu(false);
    });
  }

  async function init() {
    wire();
    try {
      currentUser = await API.authMe();
      appStarted = true;
      hideLogin();
      renderUserMenu();
      await App.init();
    } catch (e) {
      showLogin();
    }
  }

  return { init, handleUnauthorized };
})();
