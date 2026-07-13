/* ============================================================
   MediaMessage — единый компонент отрисовки вложений сообщения.

   Раньше эта логика была продублирована в chatview.js (раздел «Чат»)
   и в contacts.js (встроенный чат CRM-карточки), и рано или поздно
   они бы разошлись. Теперь оба места вызывают MediaMessage.render(),
   а тип вложения (photo/video/video_note/animation/voice/audio/
   sticker/document) определяется backend'ом один раз и приходит
   в поле message.media.kind — здесь только отрисовка.
   ============================================================ */
const MediaMessage = (() => {
  function waveBars(seed, count) {
    let s = seed || 1;
    const rnd = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
    return Array.from({ length: count || 22 }, () => 4 + Math.round(rnd() * 14));
  }

  function fmtDuration(seconds) {
    const dur = seconds ? Math.round(seconds) : 0;
    const mm = String(Math.floor(dur / 60));
    const ss = String(dur % 60).padStart(2, "0");
    return `${mm}:${ss}`;
  }

  // ---- lightbox (полноэкранный просмотр фото/видео) ------------------
  // Один переиспользуемый оверлей на всё приложение вместо копирования
  // модального разметки в каждом месте, где может понадобиться просмотр.
  function openLightbox(url, kind) {
    let overlay = document.getElementById("mediaLightbox");
    if (overlay) overlay.remove();
    overlay = document.createElement("div");
    overlay.id = "mediaLightbox";
    overlay.className = "media-lightbox";
    overlay.innerHTML = kind === "video"
      ? `<video src="${url}" controls autoplay playsinline></video>`
      : `<img src="${url}" alt="">`;
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) overlay.remove();
    });
    document.body.appendChild(overlay);
  }

  function voiceLikeHtml(url, durationLabel, wave, extraClass, metaHtml) {
    const waveHtml = wave.map((h, i) => `<span data-bar="${i}" style="height:${h}px"></span>`).join("");
    return `
      <div class="att-voice ${extraClass || ""}" data-voice-url="${url}">
        <button type="button" class="att-voice__play">
          <svg class="att-voice__icon-play" width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4.5v15l14-7.5-14-7.5Z"/></svg>
          <svg class="att-voice__icon-pause" width="13" height="13" viewBox="0 0 24 24" fill="currentColor" hidden><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>
        </button>
        ${metaHtml || `<div class="att-voice__wave">${waveHtml}</div>`}
        <span class="att-voice__time">${durationLabel}</span>
      </div>`;
  }

  // Определяет иконку/подпись файла по расширению — вместо одного
  // одинакового значка "документ" для абсолютно всех вложений.
  const DOC_KINDS = {
    pdf: { label: "PDF", color: "#e5484d" },
    doc: { label: "DOC", color: "#2f6fed" }, docx: { label: "DOC", color: "#2f6fed" },
    xls: { label: "XLS", color: "#2f9e5c" }, xlsx: { label: "XLS", color: "#2f9e5c" },
    ppt: { label: "PPT", color: "#e0762e" }, pptx: { label: "PPT", color: "#e0762e" },
    zip: { label: "ZIP", color: "#8a6de0" }, rar: { label: "RAR", color: "#8a6de0" }, "7z": { label: "7Z", color: "#8a6de0" },
    txt: { label: "TXT", color: "#8a8f98" },
    mp3: { label: "MP3", color: "#2f9e5c" },
  };
  function docKind(fileName) {
    const ext = (fileName || "").split(".").pop().toLowerCase();
    return DOC_KINDS[ext] || { label: ext ? ext.slice(0, 4).toUpperCase() : "FILE", color: "#8a8f98" };
  }

  // seed — обычно id сообщения, нужен только чтобы "случайная" форма
  // волны голосового была стабильной между перерисовками, а не дёргалась.
  function render(media, url, seed) {
    if (!media) return "";
    switch (media.kind) {
      case "photo":
        return `<div class="att-image" data-lightbox="photo" data-url="${url}"><img src="${url}" alt="Фото" loading="lazy"></div>`;

      case "video":
        return `<div class="att-image att-image--video" data-video-gif-check="1">
          <video src="${url}" preload="metadata" playsinline style="width:100%;border-radius:12px;"></video>
          <button type="button" class="att-video__play" data-lightbox="video" data-url="${url}">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4.5v15l14-7.5-14-7.5Z"/></svg>
          </button>
          ${media.duration ? `<span class="att-video__duration">${fmtDuration(media.duration)}</span>` : ""}
        </div>`;

      case "video_note":
        // Telegram "кружок": круглая маска, кнопка Play поверх, круговой
        // прогресс воспроизведения и длительность — заполняются в wire().
        return `<div class="att-video-note" data-duration="${media.duration || 0}">
          <video src="${url}" preload="metadata" playsinline loop></video>
          <svg class="att-video-note__ring" viewBox="0 0 100 100">
            <circle cx="50" cy="50" r="47" class="att-video-note__ring-bg"/>
            <circle cx="50" cy="50" r="47" class="att-video-note__ring-fg" stroke-dasharray="295.3" stroke-dashoffset="295.3"/>
          </svg>
          <button type="button" class="att-video-note__play">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4.5v15l14-7.5-14-7.5Z"/></svg>
          </button>
          <span class="att-video-note__time">${fmtDuration(media.duration)}</span>
        </div>`;

      case "animation":
        // GIF в Telegram — это видео без звука с автоплеем и зацикливанием, без кнопки Play.
        return `<div class="att-image"><video src="${url}" autoplay loop muted playsinline></video></div>`;

      case "voice":
        return voiceLikeHtml(url, fmtDuration(media.duration), waveBars(seed));

      case "audio":
        return voiceLikeHtml(
          url, fmtDuration(media.duration), [], "att-voice--audio",
          `<div class="att-audio__meta"><span class="att-audio__name">${Utils.escapeHtml(media.file_name || "Аудио")}</span></div>`
        );

      case "sticker":
        if ((media.mime || "").includes("webp")) {
          return `<div class="att-image att-image--sticker"><img src="${url}" alt="Стикер" loading="lazy"></div>`;
        }
        if ((media.mime || "").includes("webm")) {
          return `<div class="att-image att-image--sticker"><video src="${url}" autoplay loop muted playsinline></video></div>`;
        }
        // TGS — Lottie-анимация; без бандла lottie-web их не отрисовать
        // покадрово, поэтому показываем узнаваемый плейсхолдер.
        return `<div style="font-size:52px;line-height:1;" title="Анимированный стикер">🖼</div>`;

      case "document":
      default: {
        const kind = docKind(media.file_name);
        return `
          <a class="att-doc" href="${url}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">
            <div class="att-doc__icon" style="background:${kind.color}22;color:${kind.color};">
              <span class="att-doc__ext">${kind.label}</span>
            </div>
            <div>
              <div class="att-doc__name">${Utils.escapeHtml(media.file_name || "Файл")}</div>
              <div class="att-doc__size">${Utils.formatFileSize(media.size)}</div>
            </div>
          </a>`;
      }
    }
  }

  // Навешивает интерактивность на уже отрисованные вложения одного
  // контейнера сообщений (лайтбокс, кружки, волна голосового). Вызывается
  // после каждого renderThread()/renderMessages() — как и play-обработчик
  // голосовых, который уже был у вызывающего кода.
  function wire(container) {
    container.querySelectorAll("[data-lightbox]").forEach((el) => {
      el.addEventListener("click", () => openLightbox(el.dataset.url, el.dataset.lightbox));
    });

    // Некоторые "гифки" (например, реакции из стороннего клиента) приходят
    // от Telegram как обычное video, а не animation — backend классифицирует
    // строго по флагу, который Telegram сам проставляет, и тут ничего не
    // поделать без ложных срабатываний. Но если браузер надёжно сообщает,
    // что в коротком ролике нет звука (Firefox: video.mozHasAudio, Safari:
    // video.audioTracks.length), по факту это гифка — переключаем вид на
    // автовоспроизведение без кнопки Play/таймера, как для animation.
    // В Chrome такой надёжной проверки до начала декодирования нет —
    // там ролик остаётся обычным видео, это осознанное ограничение.
    container.querySelectorAll('[data-video-gif-check="1"]').forEach((wrap) => {
      const video = wrap.querySelector("video");
      const upgrade = () => {
        let hasAudio = null; // null = браузер не может сказать надёжно
        if ("mozHasAudio" in video) hasAudio = !!video.mozHasAudio;
        else if (video.audioTracks) hasAudio = video.audioTracks.length > 0;
        if (hasAudio === false && video.duration && video.duration <= 8) {
          wrap.classList.add("att-image--video-gif");
          video.autoplay = true; video.loop = true; video.muted = true;
          video.play().catch(() => {});
        }
      };
      if (video.readyState >= 1) upgrade();
      else video.addEventListener("loadedmetadata", upgrade, { once: true });
    });

    container.querySelectorAll(".att-video-note").forEach((el) => {
      const video = el.querySelector("video");
      const ring = el.querySelector(".att-video-note__ring-fg");
      const playBtn = el.querySelector(".att-video-note__play");
      const timeEl = el.querySelector(".att-video-note__time");
      const duration = Number(el.dataset.duration) || 0;
      const CIRC = 295.3;

      const setPlaying = (playing) => {
        el.classList.toggle("is-playing", playing);
        playBtn.innerHTML = playing
          ? `<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>`
          : `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4.5v15l14-7.5-14-7.5Z"/></svg>`;
      };

      playBtn.addEventListener("click", () => {
        if (video.paused) { video.play().catch(() => {}); setPlaying(true); }
        else { video.pause(); setPlaying(false); }
      });
      video.addEventListener("timeupdate", () => {
        const total = video.duration || duration || 1;
        const frac = Math.min(1, video.currentTime / total);
        ring.setAttribute("stroke-dashoffset", String(CIRC * (1 - frac)));
        timeEl.textContent = fmtDuration(total - video.currentTime);
      });
      video.addEventListener("ended", () => { setPlaying(false); ring.setAttribute("stroke-dashoffset", String(CIRC)); timeEl.textContent = fmtDuration(duration); });
      // Двойной клик / долгое удержание на кружке = полноэкранный просмотр.
      el.addEventListener("dblclick", () => openLightbox(video.currentSrc || video.src, "video"));
    });
  }

  return { render, wire, openLightbox };
})();
