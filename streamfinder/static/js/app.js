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

  SF.api = async (url, body) => {
    const options =
      body === undefined
        ? {}
        : {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
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
  const progressStore = store("streamfinder.progress.v1", () => ({}));
  SF.progress = {
    all: () => progressStore.read(),
    get: (key) => progressStore.read()[key] || null,
    set(key, value) {
      const data = progressStore.read();
      data[key] = { ...(data[key] || {}), ...value, updated: Date.now() };
      progressStore.write(data);
    },
    remove(key) {
      const data = progressStore.read();
      delete data[key];
      progressStore.write(data);
    },
  };

  // My list: [{ key, id, type, title, poster, url, added }]
  const listStore = store("streamfinder.mylist.v1", () => []);
  SF.myList = {
    all: () => listStore.read(),
    has: (key) => listStore.read().some((i) => i.key === key),
    toggle(item) {
      const items = listStore.read();
      const exists = items.some((i) => i.key === item.key);
      listStore.write(
        exists
          ? items.filter((i) => i.key !== item.key)
          : [{ ...item, added: Date.now() }, ...items],
      );
      return !exists;
    },
  };

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
    volume: 1,
    muted: false,
  };
  SF.settings = {
    get: () => ({ ...DEFAULT_SETTINGS, ...settingsStore.read() }),
    set: (changes) => settingsStore.write({ ...SF.settings.get(), ...changes }),
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
    settingsForm.primary.value = current.primary;
    settingsForm.fallback.value = current.fallback;
    settingsForm.auto.checked = current.auto;
    settingsForm.autoSwitch.checked = current.autoSwitch;
    settingsForm.autoNext.checked = current.autoNext;
    settingsForm.alwaysConvert.checked = current.alwaysConvert;
    const saved = document.getElementById("settings-saved");
    settingsForm.addEventListener("change", () => {
      SF.settings.set({
        quality: settingsForm.quality.value,
        primary: settingsForm.primary.value,
        fallback: settingsForm.fallback.value,
        auto: settingsForm.auto.checked,
        autoSwitch: settingsForm.autoSwitch.checked,
        autoNext: settingsForm.autoNext.checked,
        alwaysConvert: settingsForm.alwaysConvert.checked,
      });
      saved.textContent = t("saved");
    });
  }

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
  SF.initRow = (viewport) => {
    const track = viewport.querySelector(".row-track");
    const prev = viewport.querySelector(".row-arrow.prev");
    const next = viewport.querySelector(".row-arrow.next");
    if (!track || !prev || !next) return;
    const update = () => {
      prev.hidden = track.scrollLeft < 8;
      next.hidden =
        track.scrollLeft + track.clientWidth >= track.scrollWidth - 8;
    };
    const page = (dir) =>
      track.scrollBy({
        left: dir * track.clientWidth * 0.85,
        behavior: "smooth",
      });
    prev.addEventListener("click", () => page(-1));
    next.addEventListener("click", () => page(1));
    track.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    update();
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
      `/hledat${params.toString() ? `?${params}` : ""}`,
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
      const items = data.items
        .slice(0, 7)
        .map((item) =>
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
          href: `/hledat?q=${encodeURIComponent(query)}`,
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
      else location.href = "/hledat";
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
      try {
        const data = await SF.api(`/api/browse?${params}`);
        browseGrid.append(...data.items.map(SF.poster));
        browseGrid.dataset.next = data.next_page || "";
        loadMore.hidden = !data.next_page;
        loadMore.textContent = t("load_more");
      } catch {
        loadMore.textContent = t("load_failed");
      } finally {
        loadMore.disabled = false;
        loading = false;
      }
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
  const continueRow = document.getElementById("continue-row");
  if (continueRow) {
    const items = Object.entries(SF.progress.all())
      .filter(
        ([, p]) =>
          p.duration && p.position > 30 && p.position < p.duration * 0.95,
      )
      .sort(([, a], [, b]) => b.updated - a.updated)
      .slice(0, 20);
    const track = document.getElementById("continue-track");
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
  if (listRow) {
    const items = SF.myList.all();
    document
      .getElementById("mylist-track")
      .replaceChildren(...items.map(SF.poster));
    listRow.hidden = !items.length;
  }
})();
