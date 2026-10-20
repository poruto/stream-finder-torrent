// Shared UI: translations, settings, navigation, live search, carousels,
// browse paging, watch progress and "My list".
(() => {
  "use strict";

  const SF = (window.SF = window.SF || {});
  const readJSON = (id, fallback) => {
    try {
      return JSON.parse(document.getElementById(id).textContent);
    } catch {
      return fallback;
    }
  };

  // ---------------------------------------------------------- i18n
  const STRINGS = readJSON("i18n", {});
  const DEFAULTS = readJSON("defaults", {
    subtitles: ["cs", "en"],
    lang: "en",
  });
  SF.lang = DEFAULTS.lang;
  SF.t = (key, params = {}) =>
    (STRINGS[key] || key).replace(/\{(\w+)\}/g, (_, name) =>
      name in params ? String(params[name]) : `{${name}}`,
    );
  SF.plural = (n, base) =>
    SF.t(`${base}_${n === 1 ? "one" : n >= 2 && n <= 4 ? "few" : "many"}`, {
      n,
    });
  const t = SF.t;

  // ---------------------------------------------------------- DOM helpers
  SF.el = function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key === "className") node.className = value;
      else if (key in node && typeof value !== "string") node[key] = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    node.append(
      ...[]
        .concat(children)
        .filter((c) => c !== null && c !== undefined && c !== false),
    );
    return node;
  };

  SF.icon = (name, extraClass = "") => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", `icon ${extraClass}`.trim());
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.append(use);
    return svg;
  };

  // `signal`: an AbortController's signal, to drop a request nobody waits for any more.
  SF.api = async (url, body, { signal } = {}) => {
    const options =
      body === undefined
        ? { signal }
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
            signal,
          };
    const response = await fetch(url, options);
    let data;
    try {
      data = await response.json();
    } catch {
      throw new Error(`HTTP ${response.status}`);
    }
    if (!response.ok || data.success === false) {
      const error = new Error(data.error || `HTTP ${response.status}`);
      error.data = data;
      throw error;
    }
    return data;
  };

  SF.formatTime = (seconds) => {
    const s = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };

  // ---------------------------------------------------------- local storage
  const store = (key, fallback) => ({
    read() {
      try {
        return JSON.parse(localStorage.getItem(key)) ?? fallback();
      } catch {
        return fallback();
      }
    },
    write(value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch {
        /* storage full or blocked */
      }
    },
  });

  // Watch progress per title: { "<type>:<id>": { title, backdrop, url, season, episode,
  //   position, duration, magnet, fileIndex, updated, episodes: { "1x3": {position, duration} } } }
  // A removed title stays as { removed: true, updated } so other devices learn about it.
  const progressStore = store("streamfinder.progress.v1", () => ({}));
  SF.progress = {
    all: () => Object.fromEntries(Object.entries(progressStore.read()).filter(([, p]) => !p.removed)),
    get: (key) => {
      const entry = progressStore.read()[key];
      return entry && !entry.removed ? entry : null;
    },
    set(key, value) {
      const data = progressStore.read();
      const old = data[key]?.removed ? {} : data[key] || {};
      data[key] = { ...old, ...value, updated: Date.now() };
      progressStore.write(data);
      syncSoon();
    },
    remove(key) {
      const data = progressStore.read();
      data[key] = { removed: true, updated: Date.now() };
      progressStore.write(data);
      syncSoon();
    },
  };

  // My list: [{ key, id, type, title, poster, url, added }]; removed keys: { key: time }
  const listStore = store("streamfinder.mylist.v1", () => []);
  const listRemovedStore = store("streamfinder.mylist.removed.v1", () => ({}));
  SF.myList = {
    all: () => listStore.read(),
    has: (key) => listStore.read().some((i) => i.key === key),
    toggle(item) {
      const items = listStore.read();
      const exists = items.some((i) => i.key === item.key);
      const removed = listRemovedStore.read();
      if (exists) removed[item.key] = Date.now();
      else delete removed[item.key];
      listRemovedStore.write(removed);
      listStore.write(
        exists
          ? items.filter((i) => i.key !== item.key)
          : [{ ...item, added: Date.now() }, ...items],
      );
      syncSoon(1000);
      return !exists;
    },
  };

  // ---------------------------------------------------------- shared between devices
  // Progress and My list are merged with the server's copy (streamfinder/shared.py), so
  // a film started on the TV continues on the phone. Entry by entry the newer one wins.
  const newer = (a, b, field) => ((b?.[field] || 0) > (a?.[field] || 0) ? b : a);
  function mergeShared(remote) {
    const progress = progressStore.read();
    for (const [key, entry] of Object.entries(remote.progress || {})) progress[key] = newer(progress[key], entry, "updated");
    const removed = listRemovedStore.read();
    for (const [key, when] of Object.entries(remote.listRemoved || {})) removed[key] = Math.max(removed[key] || 0, when);
    const items = new Map(listStore.read().map((i) => [i.key, i]));
    for (const item of remote.list || []) items.set(item.key, newer(items.get(item.key), item, "added"));
    const list = [...items.values()].filter((i) => (i.added || 0) > (removed[i.key] || 0)).sort((a, b) => (b.added || 0) - (a.added || 0));
    progressStore.write(progress);
    listRemovedStore.write(removed);
    listStore.write(list);
  }
  const sharedBody = () => ({ progress: progressStore.read(), list: listStore.read(), listRemoved: listRemovedStore.read() });
  let syncTimer = null;
  SF.syncShared = async () => {
    clearTimeout(syncTimer);
    syncTimer = null;
    try {
      // Merged into what is stored now: a change made while the request ran is kept.
      mergeShared(await SF.api("/api/shared", sharedBody()));
      document.dispatchEvent(new CustomEvent("sf:shared"));
    } catch {
      /* offline or an older server: this device's own copy still works */
    }
  };
  // Not more often than every few seconds while a film saves its position.
  function syncSoon(delay = 5000) {
    if (syncTimer === null) syncTimer = setTimeout(SF.syncShared, delay);
  }
  addEventListener("pagehide", () => {
    if (syncTimer === null) return;
    clearTimeout(syncTimer);
    navigator.sendBeacon?.("/api/shared", new Blob([JSON.stringify(sharedBody())], { type: "application/json" }));
  });
  SF.syncShared();

  // Entries saved before the URLs became English (/film/1 -> /movie/1, /serial/1 -> /show/1).
  const englishUrl = (url) =>
    url?.replace(/^\/film\//, "/movie/").replace(/^\/serial\//, "/show/");
  try {
    const progress = progressStore.read();
    if (Object.values(progress).some((p) => englishUrl(p.url) !== p.url)) {
      for (const p of Object.values(progress)) p.url = englishUrl(p.url);
      progressStore.write(progress);
    }
    const list = listStore.read();
    if (list.some((i) => englishUrl(i.url) !== i.url)) {
      listStore.write(list.map((i) => ({ ...i, url: englishUrl(i.url) })));
    }
  } catch {
    /* nothing stored yet */
  }

  // Settings: { quality, primary, fallback, auto, autoSwitch, autoNext, volume, muted }
  const settingsStore = store("streamfinder.settings.v1", () => ({}));
  const DEFAULT_SETTINGS = {
    quality: "auto",
    primary: DEFAULTS.subtitles[0] || "cs",
    fallback: DEFAULTS.subtitles[1] || "en",
    auto: true,
    autoSwitch: true,
    autoNext: true,
    alwaysConvert: false,
    // "cs": Czech/Slovak dubbed versions and audio tracks first, "any": no language preference
    versionLang: DEFAULTS.lang === "cs" ? "cs" : "any",
    // No subtitles in my language: "translate" others, "ai" from the audio, "other" language as is
    subsMissing: "translate",
    translation: "auto", // "auto": AI when a Groq/OpenAI key is set, else Google; or "google" / "ai"
    aiTarget: "en", // AI subtitles: translate into this language ("" = as spoken)
    aiVia: "english", // Whisper translates the speech into English, Google into the target; or "direct"
    volume: 1,
    muted: false,
  };
  // Small flags for languages (emoji flags don't show on Windows). Unknown: the code.
  const FLAG_SVG = {
    cs: '<rect width="30" height="10" fill="#fff"/><rect y="10" width="30" height="10" fill="#d7141a"/><path d="M0 0 15 10 0 20Z" fill="#11457e"/>',
    sk: '<rect width="30" height="7" fill="#fff"/><rect y="7" width="30" height="6" fill="#0b4ea2"/><rect y="13" width="30" height="7" fill="#ee1c25"/><path d="M6 4h8v7q0 4-4 6-4-2-4-6Z" fill="#ee1c25" stroke="#fff" stroke-width="1"/>',
    en: '<rect width="30" height="20" fill="#012169"/><path d="M0 0 30 20M30 0 0 20" stroke="#fff" stroke-width="4"/><path d="M0 0 30 20M30 0 0 20" stroke="#c8102e" stroke-width="1.6"/><path d="M15 0v20M0 10h30" stroke="#fff" stroke-width="6"/><path d="M15 0v20M0 10h30" stroke="#c8102e" stroke-width="3.4"/>',
    de: '<rect width="30" height="7" fill="#000"/><rect y="7" width="30" height="6" fill="#dd0000"/><rect y="13" width="30" height="7" fill="#ffce00"/>',
    fr: '<rect width="10" height="20" fill="#0055a4"/><rect x="10" width="10" height="20" fill="#fff"/><rect x="20" width="10" height="20" fill="#ef4135"/>',
    it: '<rect width="10" height="20" fill="#009246"/><rect x="10" width="10" height="20" fill="#fff"/><rect x="20" width="10" height="20" fill="#ce2b37"/>',
    es: '<rect width="30" height="20" fill="#aa151b"/><rect y="5" width="30" height="10" fill="#f1bf00"/>',
    pl: '<rect width="30" height="10" fill="#fff"/><rect y="10" width="30" height="10" fill="#dc143c"/>',
    hu: '<rect width="30" height="7" fill="#ce2939"/><rect y="7" width="30" height="6" fill="#fff"/><rect y="13" width="30" height="7" fill="#477050"/>',
    ru: '<rect width="30" height="7" fill="#fff"/><rect y="7" width="30" height="6" fill="#0039a6"/><rect y="13" width="30" height="7" fill="#d52b1e"/>',
    uk: '<rect width="30" height="10" fill="#0057b7"/><rect y="10" width="30" height="10" fill="#ffd700"/>',
    nl: '<rect width="30" height="7" fill="#ae1c28"/><rect y="7" width="30" height="6" fill="#fff"/><rect y="13" width="30" height="7" fill="#21468b"/>',
    pt: '<rect width="12" height="20" fill="#006600"/><rect x="12" width="18" height="20" fill="#ff0000"/><circle cx="12" cy="10" r="4" fill="#ffcc00"/>',
    ja: '<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="6" fill="#bc002d"/>',
    ko: '<rect width="30" height="20" fill="#fff"/><path d="M9 10a6 6 0 0 1 12 0Z" fill="#cd2e3a"/><path d="M9 10a6 6 0 0 0 12 0Z" fill="#0047a0"/>',
    zh: '<rect width="30" height="20" fill="#de2910"/><path d="m6 3 1.2 3.6H11L8 8.8l1.1 3.6L6 10.2l-3.1 2.2L4 8.8 1 6.6h3.8Z" fill="#ffde00"/>',
    sv: '<rect width="30" height="20" fill="#006aa7"/><path d="M11 0v20M0 10h30" stroke="#fecc00" stroke-width="4"/>',
    da: '<rect width="30" height="20" fill="#c8102e"/><path d="M11 0v20M0 10h30" stroke="#fff" stroke-width="4"/>',
    fi: '<rect width="30" height="20" fill="#fff"/><path d="M11 0v20M0 10h30" stroke="#002f6c" stroke-width="5"/>',
    no: '<rect width="30" height="20" fill="#ba0c2f"/><path d="M11 0v20M0 10h30" stroke="#fff" stroke-width="5"/><path d="M11 0v20M0 10h30" stroke="#00205b" stroke-width="2.4"/>',
    tr: '<rect width="30" height="20" fill="#e30a17"/><circle cx="12" cy="10" r="5" fill="#fff"/><circle cx="13.3" cy="10" r="4" fill="#e30a17"/>',
    hr: '<rect width="30" height="7" fill="#ff0000"/><rect y="7" width="30" height="6" fill="#fff"/><rect y="13" width="30" height="7" fill="#171796"/>',
    ro: '<rect width="10" height="20" fill="#002b7f"/><rect x="10" width="10" height="20" fill="#fcd116"/><rect x="20" width="10" height="20" fill="#ce1126"/>',
  };
  SF.flag = (code) => {
    const svg = FLAG_SVG[code];
    if (!svg) return SF.el("span", { className: "flag flag-code", "aria-hidden": "true" }, [String(code || "?").toUpperCase()]);
    const holder = SF.el("span", { className: "flag", "aria-hidden": "true" });
    holder.innerHTML = `<svg viewBox="0 0 30 20" preserveAspectRatio="none">${svg}</svg>`;
    return holder;
  };

  SF.settings = {
    get: () => ({ ...DEFAULT_SETTINGS, ...settingsStore.read() }),
    set: (changes) => settingsStore.write({ ...SF.settings.get(), ...changes }),
    czech: () => SF.settings.get().versionLang === "cs",
    quality: () =>
      SF.settings.get().quality === "auto"
        ? null
        : Number(SF.settings.get().quality),
    languages() {
      const { primary, fallback } = SF.settings.get();
      return [
        ...new Set([primary, fallback, ...DEFAULTS.subtitles].filter(Boolean)),
      ].slice(0, 3);
    },
  };

  const settingsForm = document.getElementById("settings-form");
  if (settingsForm) {
    const current = SF.settings.get();
    const quality = settingsForm.querySelector(
      `input[name="quality"][value="${current.quality}"]`,
    );
    if (quality) quality.checked = true;
    const versionLang = settingsForm.querySelector(
      `input[name="versionLang"][value="${current.versionLang}"]`,
    );
    if (versionLang) versionLang.checked = true;
    settingsForm.primary.value = current.primary;
    settingsForm.fallback.value = current.fallback;
    settingsForm.auto.checked = current.auto;
    settingsForm.autoSwitch.checked = current.autoSwitch;
    settingsForm.autoNext.checked = current.autoNext;
    settingsForm.alwaysConvert.checked = current.alwaysConvert;
    settingsForm.subsMissing.value = current.subsMissing;
    settingsForm.translation.value = current.translation;
    if (settingsForm.aiTarget) settingsForm.aiTarget.value = current.aiTarget;
    if (settingsForm.aiVia) settingsForm.aiVia.value = current.aiVia === "direct" ? "direct" : "english";
    const saved = document.getElementById("settings-saved");
    settingsForm.addEventListener("change", () => {
      SF.settings.set({
        quality: settingsForm.quality.value,
        versionLang: settingsForm.versionLang.value,
        primary: settingsForm.primary.value,
        fallback: settingsForm.fallback.value,
        auto: settingsForm.auto.checked,
        autoSwitch: settingsForm.autoSwitch.checked,
        autoNext: settingsForm.autoNext.checked,
        alwaysConvert: settingsForm.alwaysConvert.checked,
        subsMissing: settingsForm.subsMissing.value || "translate",
        translation: settingsForm.translation.value || "auto",
        ...(settingsForm.aiTarget ? { aiTarget: settingsForm.aiTarget.value } : {}),
        ...(settingsForm.aiVia ? { aiVia: settingsForm.aiVia.value } : {}),
      });
      saved.textContent = t("saved");
    });
  }

  // ---------------------------------------------------------- API keys (Settings page)
  // Keys are write-only: the server only reports whether each one is set.
  const keysSection = document.getElementById("keys");
  if (keysSection) {
    const renderState = (state) => {
      for (const badge of keysSection.querySelectorAll(".key-state")) {
        const ok = Boolean(state[badge.dataset.key]);
        badge.textContent = ok ? t("key_set") : t("key_missing");
        badge.dataset.ok = String(ok);
      }
      for (const remove of keysSection.querySelectorAll("[data-remove]")) {
        remove.hidden = !state[remove.dataset.remove];
      }
      // AI subtitles: which source is in use, and the local server's address.
      const ai = keysSection.querySelector('[data-field="transcribe"]');
      if (ai) {
        const radio = ai.querySelector(
          `input[name="transcribe_provider"][value="${state.transcribe_provider || "local"}"]`,
        );
        if (radio) radio.checked = true;
        ai.querySelector('input[name="transcribe_api_url"]').value =
          state.transcribe_local || "";
        ai.querySelector('input[name="transcribe_enabled"]').checked = Boolean(
          state.transcribe_enabled,
        );
        const quality = ai.querySelector('select[name="whisper_quality"]');
        if (quality && state.whisper_quality)
          quality.value = state.whisper_quality;
        const note = ai.querySelector(".whisper-state");
        if (note) {
          note.textContent =
            state.whisper_state === "downloading"
              ? t("whisper_downloading")
              : String(state.whisper_state || "").startsWith("error")
                ? t("whisper_failed", { error: state.whisper_state.slice(7) })
                : state.whisper_local === false
                  ? t("whisper_host_only")
                  : "";
          // A new model is downloading: check again in a few seconds.
          if (state.whisper_state === "downloading")
            setTimeout(
              () =>
                SF.api("/api/keys")
                  .then(renderState)
                  .catch(() => {}),
              4000,
            );
        }
      }
    };
    SF.api("/api/keys")
      .then(renderState)
      .catch(() => {});

    const submit = async (form, body) => {
      const message = form.querySelector(".key-message");
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      message.className = "key-message";
      message.textContent = t("key_checking");
      try {
        const state = await SF.api("/api/keys", body);
        renderState(state);
        form.reset();
        message.className = "key-message ok";
        message.textContent = t("key_saved");
        if (
          form.dataset.field === "tmdb" &&
          new URLSearchParams(location.search).get("setup")
        ) {
          setTimeout(() => (location.href = "/"), 900);
        }
      } catch (error) {
        message.className = "key-message bad";
        message.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };

    // Picking a Whisper quality applies right away (the model downloads in the background).
    keysSection
      .querySelector('select[name="whisper_quality"]')
      ?.addEventListener("change", (event) =>
        event.target.form.requestSubmit(),
      );

    for (const form of keysSection.querySelectorAll(".key-form")) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const body = Object.fromEntries(
          [...new FormData(form)].filter(([, v]) => String(v).trim()),
        );
        // The local address is shown pre-filled: only send it when it was changed.
        const url = form.querySelector('input[name="transcribe_api_url"]');
        if (
          url &&
          url.value === url.defaultValue &&
          body.transcribe_provider !== "local"
        )
          delete body.transcribe_api_url;
        const enabled = form.querySelector('input[name="transcribe_enabled"]');
        if (enabled) body.transcribe_enabled = enabled.checked ? "1" : "0";
        if (!Object.keys(body).length) return;
        submit(form, body);
      });
    }
    for (const button of keysSection.querySelectorAll("[data-remove]")) {
      button.addEventListener("click", (event) => {
        const form = event.target.closest(".key-form");
        const what = button.dataset.remove;
        if (confirm(t(`key_remove_confirm_${what}`)))
          submit(form, { remove: [what] });
      });
    }
  }

  // ---------------------------------------------------------- admin login (Settings page)
  const loginForm = document.getElementById("admin-login");
  const formMessage = (form, text, kind = "") => {
    const node = form.querySelector(".key-message");
    node.className = `key-message ${kind}`.trim();
    node.textContent = text;
  };
  loginForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    formMessage(loginForm, t("loading"));
    try {
      await SF.api(
        "/api/admin/login",
        Object.fromEntries(new FormData(loginForm)),
      );
      location.reload();
    } catch (error) {
      formMessage(loginForm, error.message, "bad");
    }
  });
  document
    .getElementById("admin-logout")
    ?.addEventListener("click", async () => {
      await SF.api("/api/admin/logout", {}).catch(() => {});
      location.reload();
    });
  const credentialsForm = document.getElementById("admin-credentials");
  credentialsForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(credentialsForm));
    if (data.new_password !== data.repeat)
      return formMessage(credentialsForm, t("password_mismatch"), "bad");
    try {
      await SF.api("/api/admin/credentials", data);
      formMessage(credentialsForm, t("password_changed"), "ok");
      setTimeout(() => location.reload(), 1200);
    } catch (error) {
      formMessage(credentialsForm, error.message, "bad");
    }
  });

  // ---------------------------------------------------------- navigation
  const nav = document.getElementById("nav");
  const onScroll = () => nav.classList.toggle("is-solid", window.scrollY > 40);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  const statusLink = document.getElementById("torrserver-status");
  async function checkTorrServer() {
    let online = false;
    try {
      online =
        (
          await (
            await fetch("/api/torrserver-status", { cache: "no-store" })
          ).json()
        ).status === "online";
    } catch {
      /* treated as offline */
    }
    statusLink.dataset.state = online ? "online" : "offline";
    statusLink.title = online ? t("torrserver_ok") : t("torrserver_down");
    statusLink.querySelector(".nav-status-label").textContent = online
      ? "TorrServer"
      : t("torrserver_down_short");
  }
  checkTorrServer();
  setInterval(() => {
    if (!document.hidden) checkTorrServer();
  }, 30000);

  // ---------------------------------------------------------- carousels
  // Arrow visibility of the carousels is measured for all of them first and then applied,
  // once per frame, so scrolling and resizing don't force a layout per row.
  const carousels = new Set();
  const dirtyCarousels = new Set();
  let carouselFrame = 0;
  const flushCarousels = () => {
    carouselFrame = 0;
    const measured = [...dirtyCarousels].map(({ track, prev, next }) => [
      prev,
      next,
      track.scrollLeft < 8,
      track.scrollLeft + track.clientWidth >= track.scrollWidth - 8,
    ]);
    dirtyCarousels.clear();
    for (const [prev, next, atStart, atEnd] of measured) {
      prev.hidden = atStart;
      next.hidden = atEnd;
    }
  };
  const queueCarousel = (row) => {
    dirtyCarousels.add(row);
    carouselFrame ||= requestAnimationFrame(flushCarousels);
  };
  window.addEventListener("resize", () => carousels.forEach(queueCarousel));

  SF.initRow = (viewport) => {
    const track = viewport.querySelector(".row-track");
    const prev = viewport.querySelector(".row-arrow.prev");
    const next = viewport.querySelector(".row-arrow.next");
    if (!track || !prev || !next) return;
    const row = { track, prev, next };
    const page = (dir) =>
      track.scrollBy({
        left: dir * track.clientWidth * 0.85,
        behavior: "smooth",
      });
    prev.addEventListener("click", () => page(-1));
    next.addEventListener("click", () => page(1));
    track.addEventListener("scroll", () => queueCarousel(row), {
      passive: true,
    });
    carousels.add(row);
    queueCarousel(row);
  };
  document.querySelectorAll(".row-viewport").forEach(SF.initRow);

  // ---------------------------------------------------------- poster markup (JS-rendered grids)
  SF.poster = (item) =>
    SF.el("a", { className: "poster", href: item.url }, [
      SF.el("img", {
        src: item.poster,
        alt: "",
        loading: "lazy",
        decoding: "async",
        width: "342",
        height: "513",
      }),
      item.imdb
        ? SF.el("span", { className: "poster-badge", title: "IMDb" }, [
            `IMDb ${item.imdb}`,
          ])
        : null,
      SF.el("span", { className: "poster-meta" }, [
        SF.el("span", { className: "poster-title" }, [item.title]),
        SF.el("span", { className: "poster-sub" }, [
          item.year ? SF.el("span", {}, [String(item.year)]) : null,
          item.rating
            ? SF.el("span", { className: "rating" }, [
                SF.icon("star"),
                String(item.rating),
              ])
            : null,
        ]),
      ]),
    ]);

  // ---------------------------------------------------------- live search
  const searchInput = document.getElementById("nav-search-input");
  const searchGrid = document.getElementById("search-grid");
  let searchTimer;
  let searchSeq = 0;
  let searchKind = new URLSearchParams(location.search).get("type") || "";

  async function liveSearch(query) {
    const seq = ++searchSeq;
    const heading = document.getElementById("search-heading");
    const empty = document.getElementById("search-empty");
    const hint = document.getElementById("search-hint");
    const params = new URLSearchParams();
    if (query) params.set("q", query);
    if (searchKind) params.set("type", searchKind);
    history.replaceState(
      null,
      "",
      `/search${params.toString() ? `?${params}` : ""}`,
    );
    if (!query) {
      searchGrid.replaceChildren();
      heading.textContent = heading.dataset.empty;
      empty.hidden = true;
      hint.hidden = false;
      return;
    }
    try {
      const data = await SF.api(`/api/search?${params}`);
      if (seq !== searchSeq) return;
      searchGrid.replaceChildren(...data.items.map(SF.poster));
      heading.textContent = heading.dataset.template.replace("{q}", query);
      empty.textContent = empty.dataset.template.replace("{q}", query);
      empty.hidden = data.items.length > 0;
      hint.hidden = true;
    } catch {
      /* keep the previous results */
    }
  }

  if (searchInput) {
    if (searchGrid) {
      searchInput.focus();
      searchInput.setSelectionRange(
        searchInput.value.length,
        searchInput.value.length,
      );
      searchInput.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(
          () => liveSearch(searchInput.value.trim()),
          300,
        );
      });
      searchInput.form.addEventListener("submit", (event) => {
        event.preventDefault();
        liveSearch(searchInput.value.trim());
      });
      document.querySelectorAll("#search-kind .chip").forEach((chip) => {
        chip.addEventListener("click", () => {
          searchKind = chip.dataset.kind;
          document
            .querySelectorAll("#search-kind .chip")
            .forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
          liveSearch(searchInput.value.trim());
        });
      });
    } else {
      initSuggestions(searchInput);
    }
  }

  // Instant results under the header search field (Enter opens the full results page).
  function initSuggestions(input) {
    const form = input.form;
    const box = SF.el("div", {
      className: "suggest",
      id: "suggest",
      role: "listbox",
      hidden: true,
    });
    form.append(box);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-controls", "suggest");
    input.setAttribute("aria-expanded", "false");
    let seq = 0;
    let active = -1;

    const close = () => {
      box.hidden = true;
      input.setAttribute("aria-expanded", "false");
      active = -1;
    };
    const options = () => [...box.querySelectorAll(".suggest-item")];
    const highlight = (index) => {
      const items = options();
      active = (index + items.length) % items.length;
      items.forEach((item, i) =>
        item.setAttribute("aria-selected", String(i === active)),
      );
    };

    async function update() {
      const query = input.value.trim();
      const mySeq = ++seq;
      if (query.length < 2) return close();
      let data;
      try {
        data = await SF.api(`/api/search?q=${encodeURIComponent(query)}`);
      } catch {
        return;
      }
      if (mySeq !== seq) return;
      const items = data.items.slice(0, 7).map((item) =>
        SF.el(
          "a",
          {
            className: "suggest-item",
            href: item.url,
            role: "option",
            "aria-selected": "false",
          },
          [
            SF.el("img", {
              src: item.poster.replace("/w342/", "/w92/"),
              alt: "",
              width: "46",
              height: "69",
              loading: "lazy",
            }),
            SF.el("span", {}, [
              SF.el("strong", {}, [item.title]),
              SF.el("small", {}, [
                [item.year, item.type === "movie" ? t("movie") : t("show")]
                  .filter(Boolean)
                  .join(" · "),
              ]),
            ]),
          ],
        ),
      );
      const all = SF.el(
        "a",
        {
          className: "suggest-all",
          href: `/search?q=${encodeURIComponent(query)}`,
        },
        [
          items.length
            ? t("results_for", { q: query })
            : t("no_results", { q: query }),
        ],
      );
      box.replaceChildren(...items, all);
      box.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = -1;
    }

    input.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(update, 220);
    });
    input.addEventListener(
      "focus",
      () => input.value.trim().length >= 2 && update(),
    );
    input.addEventListener("keydown", (event) => {
      if (box.hidden) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        highlight(active + (event.key === "ArrowDown" ? 1 : -1));
      } else if (event.key === "Enter" && active >= 0) {
        event.preventDefault();
        location.href = options()[active].href;
      } else if (event.key === "Escape") {
        close();
      }
    });
    document.addEventListener(
      "click",
      (event) => !form.contains(event.target) && close(),
    );
  }

  document.addEventListener("keydown", (event) => {
    const typing = event.target.matches("input, textarea, select");
    if (
      !typing &&
      (event.key === "/" ||
        ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k"))
    ) {
      event.preventDefault();
      if (searchInput && getComputedStyle(searchInput.form).display !== "none")
        searchInput.focus();
      else location.href = "/search";
    }
  });

  // ---------------------------------------------------------- browse: filters & infinite scroll
  const filterBar = document.getElementById("filter-bar");
  filterBar?.querySelectorAll("select").forEach((select) => {
    select.addEventListener("change", () => {
      // Drop empty values so URLs stay short and shareable.
      for (const field of filterBar.querySelectorAll("select, input"))
        field.disabled = !field.value;
      filterBar.submit();
    });
  });

  const browseGrid = document.getElementById("browse-grid");
  const loadMore = document.getElementById("load-more");
  if (browseGrid && loadMore) {
    let loading = false;
    const fetchMore = async () => {
      if (loading || !browseGrid.dataset.next) return;
      loading = true;
      const { type, genre, sort, imdb, decade, lang, next } =
        browseGrid.dataset;
      const params = new URLSearchParams({
        type,
        genre,
        sort,
        imdb,
        decade,
        lang,
        page: next,
      });
      loadMore.disabled = true;
      loadMore.textContent = t("loading");
      let loaded = false;
      try {
        const data = await SF.api(`/api/browse?${params}`);
        browseGrid.append(...data.items.map(SF.poster));
        browseGrid.dataset.next = data.next_page || "";
        loadMore.hidden = !data.next_page;
        loadMore.textContent = t("load_more");
        loaded = true;
      } catch {
        loadMore.textContent = t("load_failed");
      } finally {
        loadMore.disabled = false;
        loading = false;
      }
      // Tall screens (TVs): the button can still be in reach after a page, and the
      // observer only fires when that changes, so keep going here.
      if (
        loaded &&
        !loadMore.hidden &&
        loadMore.getBoundingClientRect().top < window.innerHeight + 800
      )
        fetchMore();
    };
    loadMore.addEventListener("click", fetchMore);
    // Load the next page automatically shortly before the end of the grid.
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(
        (entries) => entries.some((e) => e.isIntersecting) && fetchMore(),
        {
          rootMargin: "800px 0px",
        },
      ).observe(loadMore);
    }
  }

  // ---------------------------------------------------------- home: continue watching & my list
  // Drawn again when another device's progress or list arrives.
  const continueRow = document.getElementById("continue-row");
  function renderContinue() {
    const items = Object.entries(SF.progress.all())
      .filter(
        ([, p]) =>
          p.duration && p.position > 30 && p.position < p.duration * 0.95,
      )
      .sort(([, a], [, b]) => b.updated - a.updated)
      .slice(0, 20);
    const track = document.getElementById("continue-track");
    track.replaceChildren();
    for (const [key, p] of items) {
      const remove = SF.el(
        "button",
        {
          className: "wide-remove",
          type: "button",
          "aria-label": t("remove_item", { title: p.title }),
        },
        [SF.icon("close")],
      );
      const cardNode = SF.el(
        "a",
        { className: "wide-card", href: `${p.url}?play=1` },
        [
          p.backdrop
            ? SF.el("img", { src: p.backdrop, alt: "", loading: "lazy" })
            : null,
          SF.el("div", { className: "progress" }, [
            SF.el("i", {
              style: `width:${Math.min(100, (p.position / p.duration) * 100)}%`,
            }),
          ]),
          SF.el("div", { className: "wide-info" }, [
            SF.el("strong", {}, [p.title]),
            SF.el("span", {}, [
              p.season
                ? t("season_episode", { s: p.season, e: p.episode })
                : t("time_left", {
                    time: SF.formatTime(p.duration - p.position),
                  }),
            ]),
          ]),
          remove,
        ],
      );
      remove.addEventListener("click", (event) => {
        event.preventDefault();
        SF.progress.remove(key);
        cardNode.remove();
        continueRow.hidden = !track.children.length;
      });
      track.append(cardNode);
    }
    continueRow.hidden = !items.length;
  }

  const listRow = document.getElementById("mylist-row");
  function renderList() {
    const items = SF.myList.all();
    document
      .getElementById("mylist-track")
      .replaceChildren(...items.map(SF.poster));
    listRow.hidden = !items.length;
  }
  if (continueRow) renderContinue();
  if (listRow) renderList();
  document.addEventListener("sf:shared", () => {
    if (continueRow) renderContinue();
    if (listRow) renderList();
  });
})();
