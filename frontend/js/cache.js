/* ============================================================
   Cache — локальный кеш на IndexedDB (Этап 10).

   Идея: приложение никогда не должно показывать пустой экран, пока
   ждёт ответ от backend/Telegram. Вместо этого при запуске мы сразу
   отрисовываем то, что уже лежит в IndexedDB с прошлого раза, а
   свежие данные подтягиваются в фоне и тихо заменяют кеш, когда
   приходят.

   Хранилища (object stores):
     contacts      — CRM-контакты, keyPath "id"
     dialogs       — список диалогов Telegram, keyPath "telegram_id"
     messages      — переписка по диалогам, keyPath "dialog_id",
                     значение { dialog_id, messages: [...], updated_at }
     meta          — служебные данные (last_sync_time и т.п.), keyPath "key"

   UI-состояние (какой вид открыт, что введено в поле поиска и т.д.)
   сюда осознанно не попадает — это отдельный слой ("UI state"),
   держим его в обычных переменных модулей, а не в кеше данных.
   ============================================================ */
const Cache = (() => {
  const DB_NAME = "telegram_crm_cache";
  const DB_VERSION = 1;
  const SUPPORTED = typeof indexedDB !== "undefined";

  let dbPromise = null;

  function openDB() {
    if (!SUPPORTED) return Promise.resolve(null);
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains("contacts")) db.createObjectStore("contacts", { keyPath: "id" });
        if (!db.objectStoreNames.contains("dialogs")) db.createObjectStore("dialogs", { keyPath: "telegram_id" });
        if (!db.objectStoreNames.contains("messages")) db.createObjectStore("messages", { keyPath: "dialog_id" });
        if (!db.objectStoreNames.contains("meta")) db.createObjectStore("meta", { keyPath: "key" });
      };
      req.onsuccess = () => resolve(req.result);
      // Кеш — это только ускорение, а не источник истины: если IndexedDB
      // недоступна (приватный режим, старый браузер, квота и т.п.),
      // приложение должно просто работать как раньше, напрямую с сетью.
      req.onerror = () => resolve(null);
    });
    return dbPromise;
  }

  async function withStore(storeName, mode, fn) {
    const db = await openDB();
    if (!db) return null;
    return new Promise((resolve) => {
      try {
        const tx = db.transaction(storeName, mode);
        const store = tx.objectStore(storeName);
        const result = fn(store);
        tx.oncomplete = () => resolve(result && result.__value !== undefined ? result.__value : result);
        tx.onerror = () => resolve(null);
      } catch (_) {
        resolve(null);
      }
    });
  }

  function reqToPromise(req) {
    // Заворачиваем IDBRequest в объект, значение которого withStore
    // сможет прочитать после завершения транзакции (onsuccess может
    // сработать раньше oncomplete транзакции).
    const holder = { __value: undefined };
    req.onsuccess = () => { holder.__value = req.result; };
    return holder;
  }

  async function getAll(storeName) {
    const rows = await withStore(storeName, "readonly", (store) => reqToPromise(store.getAll()));
    return rows || [];
  }

  async function get(storeName, key) {
    return withStore(storeName, "readonly", (store) => reqToPromise(store.get(key)));
  }

  async function put(storeName, value) {
    return withStore(storeName, "readwrite", (store) => reqToPromise(store.put(value)));
  }

  async function bulkPut(storeName, values) {
    return withStore(storeName, "readwrite", (store) => {
      values.forEach((v) => store.put(v));
      return { __value: true };
    });
  }

  async function replaceAll(storeName, values) {
    // Полная замена содержимого стора — используется для списков
    // (контакты, диалоги), где элементы могут и удаляться, а не
    // только обновляться.
    return withStore(storeName, "readwrite", (store) => {
      store.clear();
      values.forEach((v) => store.put(v));
      return { __value: true };
    });
  }

  // ---- contacts ----
  const contacts = {
    getAll: () => getAll("contacts"),
    replaceAll: (list) => replaceAll("contacts", list),
    put: (contact) => put("contacts", contact),
  };

  // ---- dialogs ----
  const dialogs = {
    getAll: () => getAll("dialogs"),
    replaceAll: (list) => replaceAll("dialogs", list),
  };

  // ---- messages ----
  const messages = {
    get: async (dialogId) => {
      const row = await get("messages", dialogId);
      return row ? row.messages : null;
    },
    set: (dialogId, msgs) => put("messages", { dialog_id: dialogId, messages: msgs, updated_at: Date.now() }),
  };

  // ---- meta / sync bookkeeping ----
  async function getLastSync(key) {
    const row = await get("meta", key);
    return row ? row.value : null;
  }
  function setLastSync(key, value) {
    return put("meta", { key, value });
  }

  return {
    supported: SUPPORTED,
    contacts, dialogs, messages,
    getLastSync, setLastSync,
  };
})();
