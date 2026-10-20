// Detail page: one-click playback of the recommended version, episode list,
// version picker, a full-screen player with custom controls, automatic
// subtitles, resume, autoplay of the next episode and automatic fallback to a
// faster source when a torrent is slow.
(() => {
  "use strict";

  const SF = window.SF;
  const { el, icon, api, formatTime, progress, t } = SF;
  const ctx = JSON.parse(document.getElementById("player-context").textContent);
  const $ = (id) => document.getElementById(id);
  const isTV = ctx.media_type === "tv";
  const progressKey = `${ctx.media_type}:${ctx.tmdb_id}`;
  const detailUrl = location.pathname;
  const video = $("video");
  const playerEl = $("player");
  const today = new Date().toISOString().slice(0, 10);

  // Automatic source switching is deliberately conservative: torrents often need
  // a while to warm up, and flip-flopping between versions is worse than waiting.
  const START_TIMEOUT = 40000; // not playing after this long...
  const START_GRACE = 30000; // ...but still downloading well: wait this much longer once
  const STALL_TIMEOUT = 20000; // a single buffering pause (not caused by seeking)
  const SEEK_GRACE = 30000; // buffering right after a seek is expected
  const SWITCH_COOLDOWN = 120000; // minimum time between automatic switches
  const SETTLED_AFTER = 180; // seconds of smooth playback after which we only offer, never force
  const MAX_SWITCHES = 2;
  const GOOD_SPEED = 300 * 1024; // bytes/s that count as "it is downloading"
  const PREFETCH_AT = 0.85; // start looking for the next episode's versions

  const state = {
    episodes: ctx.episodes || [],
    season: ctx.season,
    versions: new Map(), // "<target>|<query>|<quality>" -> Promise(search response)
    current: null, // { target, torrent, auto, started, hash, file, files, magnet }
    subs: [],
    activeSub: null,
    subOffset: 0,
    tried: new Set(), // info hashes already attempted for this title/episode
    switches: 0,
    stallStart: null,
    switching: false,
    prefetched: false,
    timers: {},
  };

  const clearTimer = (name) => clearTimeout(state.timers[name]);
  const setTimer = (name, fn, ms) => {
    clearTimer(name);
    state.timers[name] = setTimeout(fn, ms);
  };

  // ============================================================= targets

  const hasSeason = (target) =>
    target.season !== null && target.season !== undefined;
  const movieTarget = () => ({
    season: null,
    episode: null,
    runtime: ctx.runtime,
    name: null,
  });
  const episodeTarget = (ep) => ({
    season: ep.season,
    episode: ep.episode,
    runtime: ep.runtime || ctx.runtime,
    name: ep.name,
    airDate: ep.air_date,
  });
  const targetKey = (target) =>
    hasSeason(target) ? `${target.season}x${target.episode}` : "movie";
  const shortLabel = (target) =>
    hasSeason(target)
      ? t("ep_short", { s: target.season, e: target.episode })
      : "";
  const isAired = (date) => Boolean(date) && date <= today;

  function findVersions(target, query = "") {
    const quality = SF.settings.quality();
    const key = `${targetKey(target)}|${query}|${quality}`;
    if (!state.versions.has(key)) {
      const request = api("/api/torrents", {
        tmdb_id: ctx.tmdb_id,
        media_type: ctx.media_type,
        season: target.season,
        episode: target.episode,
        runtime: target.runtime,
        quality,
        query,
      });
      request.catch(() => state.versions.delete(key));
      state.versions.set(key, request);
    }
    return state.versions.get(key);
  }

  const byHash = (data, hash) =>
    data.results.find((r) => r.hash === hash) || null;
  const recommendedOf = (data) => byHash(data, data.picks.recommended);

  // ============================================================= version facts

  const availabilityText = (a) => t(`avail_${a}`);
  const qualityText = (v) =>
    v.cam
      ? t("cam")
      : [v.quality, v.source].filter(Boolean).join(" ") || t("unknown_quality");

  function availabilityNode(v) {
    return el(
      "span",
      {
        className: "avail",
        dataset: { a: v.availability },
        title: t("sources_count", { n: v.seeders }),
      },
      [
        el("span", { className: "avail-bars", "aria-hidden": "true" }, [
          el("i"),
          el("i"),
          el("i"),
        ]),
        availabilityText(v.availability),
      ],
    );
  }

  function facts(v) {
    return [
      v.pack ? el("span", {}, [t(`pack_${v.pack}`)]) : null,
      availabilityNode(v),
      el("span", { className: v.oversized ? "size-big" : "" }, [
        v.per_episode ? t("per_episode", { size: v.per_episode }) : v.size,
      ]),
      v.browser === true
        ? el("span", { className: "browser-ok" }, [t("browser_ok")])
        : v.browser === false
          ? el("span", { className: "browser-no", title: t("browser_no_title") }, [t("browser_no")])
          : null,
      v.languages.length ? el("span", {}, [v.languages.join(", ")]) : null,
    ];
  }

  // ============================================================= hero

  const playBtn = $("play-btn");
  const playLabel = $("play-label");
  const restartBtn = $("restart-btn");
  const hint = $("play-hint");

  function storedFor(target) {
    const saved = progress.get(progressKey);
    if (!saved) return null;
    return isTV ? (saved.episodes || {})[targetKey(target)] || null : saved;
  }
  const isResumable = (p) =>
    p && p.duration && p.position > 30 && p.position < p.duration * 0.92;

  function heroTarget() {
    if (!isTV) return movieTarget();
    const saved = progress.get(progressKey);
    const firstAired = state.episodes.find((ep) => isAired(ep.air_date));
    const first = firstAired ? episodeTarget(firstAired) : null;
    if (!saved || !hasSeason(saved)) return first;
    const last = {
      season: saved.season,
      episode: saved.episode,
      runtime: ctx.runtime,
    };
    if (isResumable((saved.episodes || {})[targetKey(last)])) return last;
    return nextEpisodeOf(last) || last;
  }

  function setHint(kind, ...nodes) {
    hint.className = `play-hint${kind ? ` ${kind}` : ""}`;
    hint.replaceChildren(...nodes);
  }

  async function refreshHero({ autoplay = false } = {}) {
    // Not out yet: nothing to search for.
    if (!isTV && !ctx.released) {
      playBtn.disabled = true;
      playLabel.textContent = t("not_released");
      setHint(
        "warn",
        ctx.release_date
          ? t("release_on", {
              date: new Date(ctx.release_date).toLocaleDateString(SF.lang),
            })
          : t("not_released"),
      );
      return;
    }
    const target = heroTarget();
    if (!target) {
      playBtn.disabled = true;
      playLabel.textContent = t("not_released");
      setHint("warn", t("no_aired_episodes"));
      return;
    }
    const saved = storedFor(target);
    const resume = isResumable(saved);
    if (isTV)
      playLabel.textContent = t(resume ? "resume_ep" : "play_ep", {
        ep: shortLabel(target),
      });
    else
      playLabel.textContent = resume
        ? t("resume_from", { time: formatTime(saved.position) })
        : t("play");
    restartBtn.hidden = !resume;
    playBtn.onclick = () => play(target, { resume: true });
    restartBtn.onclick = () => play(target, { resume: false });
    playBtn.disabled = false;

    setHint("", t("finding_best"));
    try {
      const data = await findVersions(target);
      const best = recommendedOf(data);
      if (!best) {
        const custom = el("button", { type: "button" }, [t("try_custom")]);
        custom.addEventListener("click", () =>
          openSheet(target, { focusSearch: true }),
        );
        setHint("bad", t("none_found"), " ", custom);
        return;
      }
      if (data.notice === "cam_only") {
        setHint("warn", icon("alert"), " ", t("cam_only_hint"));
      } else {
        setHint(
          "",
          ...[el("span", {}, [qualityText(best)]), ...facts(best).slice(0, 4)]
            .filter(Boolean)
            .map((n) => {
              n.classList.add("fact");
              return n;
            }),
        );
      }
      if (autoplay) play(target, { resume: true });
    } catch (error) {
      setHint("bad", t("search_failed", { error: error.message }));
    }
  }

  $("versions-btn")?.addEventListener("click", () => openSheet(movieTarget()));

  // My list
  const listBtn = $("list-btn");
  const listKey = `${ctx.media_type}:${ctx.tmdb_id}`;
  const renderListButton = () => {
    const inList = SF.myList.has(listKey);
    listBtn.setAttribute("aria-pressed", String(inList));
    $("list-label").textContent = inList ? t("in_list") : t("add_to_list");
  };
  listBtn.addEventListener("click", () => {
    SF.myList.toggle({
      key: listKey,
      id: ctx.tmdb_id,
      type: ctx.media_type,
      title: ctx.title,
      poster: ctx.poster,
      url: detailUrl,
      year: ctx.year,
    });
    renderListButton();
  });
  renderListButton();

  // ============================================================= episodes (page list + player menu)

  function episodeProgress(ep) {
    const saved = progress.get(progressKey) || {};
    return (saved.episodes || {})[`${ep.season}x${ep.episode}`];
  }

  function airLabel(ep) {
    return ep.air_date
      ? t("airs_on", {
          date: new Date(ep.air_date).toLocaleDateString(SF.lang),
        })
      : t("not_aired");
  }

  function renderEpisodes() {
    const list = $("episode-list");
    if (!list) return;
    list.replaceChildren(
      ...state.episodes.map((ep) => {
        const target = episodeTarget(ep);
        const p = episodeProgress(ep);
        const aired = isAired(ep.air_date);
        const more = el(
          "button",
          { className: "episode-more", type: "button", disabled: !aired },
          [t("versions")],
        );
        more.addEventListener("click", (event) => {
          event.stopPropagation();
          openSheet(target);
        });
        const item = el(
          "li",
          {
            className: `episode${aired ? "" : " is-unaired"}`,
            id: `e${ep.episode}`,
          },
          [
            el("span", { className: "episode-num" }, [String(ep.episode)]),
            el("div", { className: "episode-thumb" }, [
              ep.still
                ? el("img", {
                    src: ep.still,
                    alt: "",
                    loading: "lazy",
                    width: "300",
                    height: "169",
                  })
                : null,
              aired
                ? el("span", { className: "play-badge" }, [icon("play")])
                : null,
              p && p.duration
                ? el("div", { className: "progress" }, [
                    el("i", {
                      style: `width:${Math.min(100, (p.position / p.duration) * 100)}%`,
                    }),
                  ])
                : null,
            ]),
            el("div", { className: "episode-text" }, [
              el("div", { className: "episode-name" }, [
                el(
                  "button",
                  {
                    type: "button",
                    disabled: !aired,
                    "aria-label": t("play_episode_aria", {
                      n: ep.episode,
                      name: ep.name,
                    }),
                  },
                  [ep.name],
                ),
                el("span", { className: "episode-runtime" }, [
                  aired
                    ? ep.runtime
                      ? t("minutes", { n: ep.runtime })
                      : ""
                    : airLabel(ep),
                ]),
              ]),
              ep.overview
                ? el("p", { className: "episode-overview" }, [ep.overview])
                : null,
            ]),
            more,
          ],
        );
        if (aired)
          item.addEventListener("click", () => play(target, { resume: true }));
        return item;
      }),
    );
  }

  async function loadSeason(season) {
    const data = await api(`/api/season/${ctx.tmdb_id}/${season}`);
    state.season = season;
    state.episodes = data.episodes;
    return data.episodes;
  }

  $("season-select")?.addEventListener("change", async (event) => {
    const list = $("episode-list");
    list.setAttribute("aria-busy", "true");
    try {
      await loadSeason(Number(event.target.value));
      renderEpisodes();
      history.replaceState(null, "", `${detailUrl}?season=${state.season}`);
    } finally {
      list.removeAttribute("aria-busy");
    }
  });

  function nextEpisodeOf(target) {
    if (!isTV || !hasSeason(target)) return null;
    if (target.season === state.season) {
      const index = state.episodes.findIndex(
        (ep) => ep.episode === target.episode,
      );
      const next = state.episodes[index + 1];
      if (index >= 0 && next)
        return isAired(next.air_date) ? episodeTarget(next) : null;
    }
    const seasons = (ctx.seasons || []).filter((s) => s.number > 0);
    const current = seasons.find((s) => s.number === target.season);
    if (current && target.episode < current.episodes)
      return {
        season: target.season,
        episode: target.episode + 1,
        runtime: ctx.runtime,
      };
    if (seasons.some((s) => s.number === target.season + 1))
      return { season: target.season + 1, episode: 1, runtime: ctx.runtime };
    return null;
  }

  // Episode picker inside the player
  const episodesBtn = $("episodes-btn");
  const episodesMenu = $("episodes-menu");
  const playerSeason = $("player-season");
  if (isTV) {
    episodesBtn.hidden = false;
    playerSeason.replaceChildren(
      ...(ctx.seasons || []).map((s) =>
        el("option", { value: String(s.number) }, [s.name]),
      ),
    );
    playerSeason.addEventListener("change", async () => {
      await loadSeason(Number(playerSeason.value));
      renderPlayerEpisodes();
      renderEpisodes();
    });
  }

  function renderPlayerEpisodes() {
    const current = state.current?.target;
    playerSeason.value = String(state.season);
    $("episodes-menu-list").replaceChildren(
      ...state.episodes.map((ep) => {
        const aired = isAired(ep.air_date);
        const isCurrent =
          current &&
          current.season === ep.season &&
          current.episode === ep.episode;
        const p = episodeProgress(ep);
        const button = el(
          "button",
          {
            type: "button",
            className: "episodes-menu-item",
            disabled: !aired,
            "aria-current": isCurrent ? "true" : null,
          },
          [
            el("span", { className: "episodes-menu-num" }, [
              String(ep.episode),
            ]),
            el("span", { className: "episodes-menu-text" }, [
              el("strong", {}, [ep.name]),
              el("small", {}, [
                aired
                  ? ep.runtime
                    ? t("minutes", { n: ep.runtime })
                    : ""
                  : airLabel(ep),
              ]),
              p && p.duration
                ? el("span", { className: "progress" }, [
                    el("i", {
                      style: `width:${Math.min(100, (p.position / p.duration) * 100)}%`,
                    }),
                  ])
                : null,
            ]),
          ],
        );
        button.addEventListener("click", () => {
          closeMenus();
          play(episodeTarget(ep), { resume: true });
        });
        return el("li", {}, [button]);
      }),
    );
  }

  // ============================================================= player shell

  function openPlayer(target) {
    $("player-title").textContent = ctx.title;
    $("player-subtitle").textContent = hasSeason(target)
      ? `${t("season_episode", { s: target.season, e: target.episode })}${target.name ? ` · ${target.name}` : ""}`
      : "";
    playerEl.hidden = false;
    document.body.style.overflow = "hidden";
    closeMenus();
    $("next-btn").hidden = true;
    $("player-stats").textContent = "";
    $("player-toast").hidden = true;
    $("player-buffering").hidden = true;
    clearTimer("next");
    state.prefetched = false;
    wakeControls();
  }

  function closePlayer() {
    saveProgress(true);
    Object.keys(state.timers).forEach(clearTimer);
    video.pause();
    video.removeAttribute("src");
    video.load();
    clearTracks();
    playerEl.hidden = true;
    document.body.style.overflow = "";
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    state.current = null;
    refreshHero();
    renderEpisodes();
  }
  $("player-close").addEventListener("click", closePlayer);

  function setStep(step, detail = "") {
    $("player-message").hidden = true;
    $("player-loading").hidden = false;
    const order = ["search", "connect", "buffer"];
    for (const li of document.querySelectorAll(".steps li")) {
      const diff = order.indexOf(li.dataset.step) - order.indexOf(step);
      li.dataset.state = diff < 0 ? "done" : diff === 0 ? "active" : "";
    }
    $("loading-detail").textContent = detail;
  }

  function showMessage(title, text, actions = []) {
    $("player-loading").hidden = true;
    $("player-buffering").hidden = true;
    $("message-title").textContent = title;
    $("message-text").textContent = text;
    $("message-actions").replaceChildren(...actions.filter(Boolean));
    $("player-message").hidden = false;
    wakeControls();
  }

  function actionButton(label, onClick, primary = false) {
    const button = el(
      "button",
      {
        type: "button",
        className: `btn ${primary ? "btn-play" : "btn-ghost"}`,
      },
      [label],
    );
    button.addEventListener("click", onClick);
    return button;
  }

  function toast(text, actions = []) {
    const node = $("player-toast");
    node.replaceChildren(el("span", {}, [text]), ...actions);
    node.hidden = false;
    setTimer(
      "toast",
      () => (node.hidden = true),
      actions.length ? 15000 : 6000,
    );
  }

  const anyMenuOpen = () =>
    !$("subs-menu").hidden || !episodesMenu.hidden || !$("versions").hidden;

  function wakeControls() {
    playerEl.classList.remove("is-idle");
    setTimer(
      "idle",
      () => {
        if (!video.paused && !anyMenuOpen() && $("player-message").hidden)
          playerEl.classList.add("is-idle");
      },
      3000,
    );
  }
  playerEl.addEventListener("mousemove", wakeControls);
  playerEl.addEventListener("touchstart", wakeControls, { passive: true });

  function closeMenus() {
    for (const [menu, button] of [
      [$("subs-menu"), $("subs-btn")],
      [episodesMenu, episodesBtn],
    ]) {
      menu.hidden = true;
      button.setAttribute("aria-expanded", "false");
    }
  }

  function toggleMenu(menu, button) {
    const open = menu.hidden;
    closeMenus();
    menu.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    if (open && menu === episodesMenu) renderPlayerEpisodes();
    wakeControls();
  }

  // ============================================================= playback

  async function play(
    target,
    {
      torrent = null,
      resume = true,
      fileIndex = null,
      startAt: forcedStart = null,
      auto = false,
    } = {},
  ) {
    if (hasSeason(target) && target.airDate && !isAired(target.airDate)) return;
    closeSheet();
    openPlayer(target);
    const saved = progress.get(progressKey) || {};
    const stored = storedFor(target);
    let startAt =
      forcedStart ?? (resume && isResumable(stored) ? stored.position : 0);
    if (torrent && !auto) {
      // A version picked by hand resets the automatic fallback history.
      state.tried = new Set();
      state.switches = 0;
    }

    try {
      if (!torrent) {
        const sameEpisode =
          saved.magnet &&
          (!isTV ||
            (saved.season === target.season &&
              saved.episode === target.episode));
        const samePack =
          isTV && saved.magnet && saved.pack && saved.season === target.season;
        if (resume && (sameEpisode || samePack)) {
          // Reuse the torrent played last time: same file, instant start.
          torrent = { magnet: saved.magnet, ...(saved.torrent || {}) };
          if (sameEpisode) fileIndex = fileIndex ?? saved.fileIndex ?? null;
        } else {
          setStep("search");
          const data = await findVersions(target);
          const best = recommendedOf(data);
          if (!best) {
            showMessage(t("no_version_title"), t("no_version_text"), [
              actionButton(
                t("custom_search"),
                () => openSheet(target, { focusSearch: true }),
                true,
              ),
              actionButton(t("close"), closePlayer),
            ]);
            return;
          }
          if (data.notice === "cam_only") {
            showMessage(t("cam_title"), t("cam_text"), [
              actionButton(
                t("play_anyway"),
                () => play(target, { torrent: best, resume }),
                true,
              ),
              actionButton(t("close"), closePlayer),
            ]);
            return;
          }
          torrent = best;
          state.tried = new Set();
          state.switches = 0;
        }
        auto = true;
      }

      setStep(
        "connect",
        torrent.quality ? `${qualityText(torrent)}, ${torrent.size}` : "",
      );
      const data = await api("/api/play", {
        magnet: torrent.magnet,
        season: fileIndex === null ? target.season : null,
        episode: fileIndex === null ? target.episode : null,
        file_index: fileIndex,
        title: hasSeason(target)
          ? `${ctx.title} S${String(target.season).padStart(2, "0")}E${String(target.episode).padStart(2, "0")}`
          : ctx.title,
        poster: ctx.poster,
      });
      if (
        forcedStart === null &&
        stored &&
        stored.fileIndex !== undefined &&
        stored.fileIndex !== data.file.index
      ) {
        startAt = resume && isResumable(stored) ? stored.position : 0;
      }
      if (torrent.hash) state.tried.add(torrent.hash);
      state.tried.add(data.hash);
      state.current = {
        target,
        torrent,
        auto,
        started: false,
        hash: data.hash,
        file: data.file,
        files: data.files,
        magnet: data.magnet,
      };
      setStep("buffer", data.file.name);
      renderFiles(data.files, data.file);
      startVideo(data.stream_url, startAt);
      loadSubtitles();
      const next = nextEpisodeOf(target);
      $("next-btn").hidden = !next;
      $("next-btn").onclick = () => next && play(next, { resume: false });
    } catch (error) {
      showMessage(t("play_failed_title"), error.message, [
        actionButton(t("choose_other"), () => openSheet(target), true),
        error.data?.retry
          ? actionButton(t("retry"), () =>
              play(target, { torrent, resume, fileIndex }),
            )
          : null,
        actionButton(t("close"), closePlayer),
      ]);
    }
  }

  function startVideo(url, startAt) {
    clearTracks();
    state.stallStart = null;
    applyVolume();
    video.src = url;
    video.addEventListener(
      "loadedmetadata",
      () => {
        if (startAt > 0 && startAt < video.duration - 10)
          video.currentTime = startAt;
      },
      { once: true },
    );
    video.play().catch(() => {
      /* autoplay blocked: the big play button is visible */
    });
    const current = state.current;
    let extended = false;
    const checkStart = () => {
      if (current !== state.current || video.readyState >= 3) return;
      // Still pulling data at a decent rate: give it more time instead of switching.
      if (!extended && (state.lastSpeed || 0) >= GOOD_SPEED) {
        extended = true;
        setTimer("start", checkStart, START_GRACE);
        return;
      }
      switchToFaster("start");
    };
    setTimer("start", checkStart, START_TIMEOUT);
    pollStats();
  }

  video.addEventListener("loadeddata", () => {
    $("player-loading").hidden = true;
  });
  video.addEventListener("seeking", () => {
    state.seekedAt = Date.now();
    state.stallStart = null;
  });
  video.addEventListener("waiting", () => {
    if (!state.current) return;
    $("player-buffering").hidden = !$("player-loading").hidden ? true : false;
    const afterSeek = Date.now() - (state.seekedAt || 0) < SEEK_GRACE;
    if (state.current.started && !afterSeek) state.stallStart ??= Date.now();
  });
  video.addEventListener("playing", () => {
    if (state.current && !state.current.started) {
      const current = state.current;
      setTimer("audio", () => current === state.current && checkAudio(), AUDIO_CHECK_DELAY);
    }
    if (state.current) state.current.started = true;
    state.stallStart = null;
    $("player-loading").hidden = true;
    $("player-message").hidden = true;
    $("player-buffering").hidden = true;
    clearTimer("start");
  });
  video.addEventListener(
    "canplay",
    () => ($("player-buffering").hidden = true),
  );

  video.addEventListener("error", () => {
    if (!state.current || !video.getAttribute("src")) return;
    const code = video.error?.code;
    const unsupported = code === 3 || code === 4;
    const target = state.current.target;
    const copy = actionButton(t("copy_vlc"), async (event) => {
      await navigator.clipboard.writeText(video.currentSrc);
      event.target.textContent = t("copied");
    });
    showMessage(
      t(unsupported ? "unsupported_title" : "load_failed_title"),
      t(unsupported ? "unsupported_text" : "load_failed_text"),
      [
        actionButton(
          t("choose_other"),
          () => openSheet(target, { browserOnly: unsupported }),
          true,
        ),
        copy,
        actionButton(t("close"), closePlayer),
      ],
    );
  });

  video.addEventListener("timeupdate", () => {
    saveProgress(false);
    updateTimeline();
    // Prepare the next episode while this one is still playing.
    if (
      !state.prefetched &&
      state.current &&
      video.duration &&
      video.currentTime / video.duration > PREFETCH_AT
    ) {
      state.prefetched = true;
      const next = nextEpisodeOf(state.current.target);
      if (next) findVersions(next).catch(() => {});
    }
  });
  video.addEventListener("pause", () => {
    saveProgress(true);
    updatePlayButton();
    wakeControls();
  });
  video.addEventListener("play", updatePlayButton);

  video.addEventListener("ended", () => {
    saveProgress(true);
    const next = state.current && nextEpisodeOf(state.current.target);
    if (!next || !SF.settings.get().autoNext) return;
    let seconds = 8;
    const playNow = actionButton(
      t("play_now"),
      () => play(next, { resume: false }),
      true,
    );
    const cancel = actionButton(t("cancel"), () => {
      clearTimer("next");
      $("player-message").hidden = true;
    });
    const tick = () => {
      showMessage(
        t("next_in", { n: seconds }),
        t("season_episode", { s: next.season, e: next.episode }),
        [playNow, cancel],
      );
      if (seconds-- <= 0) play(next, { resume: false });
      else setTimer("next", tick, 1000);
    };
    tick();
  });

  function saveProgress(force) {
    const current = state.current;
    if (
      !current ||
      !video.duration ||
      !Number.isFinite(video.duration) ||
      !current.started
    )
      return;
    const now = Date.now();
    if (!force && now - (state.lastSave || 0) < 5000) return;
    state.lastSave = now;
    const entry = {
      position: Math.floor(video.currentTime),
      duration: Math.floor(video.duration),
    };
    const saved = progress.get(progressKey) || {};
    const torrent = current.torrent || {};
    const update = {
      title: ctx.title,
      backdrop: ctx.backdrop,
      url: detailUrl,
      magnet: current.magnet,
      fileIndex: current.file.index,
      pack: Boolean(torrent.pack),
      torrent: {
        quality: torrent.quality,
        source: torrent.source,
        size: torrent.size,
        pack: torrent.pack,
        cam: torrent.cam,
        browser: torrent.browser,
        seeders: torrent.seeders,
      },
      ...entry,
    };
    if (isTV) {
      update.season = current.target.season;
      update.episode = current.target.episode;
      update.episodes = {
        ...(saved.episodes || {}),
        [targetKey(current.target)]: entry,
      };
    }
    progress.set(progressKey, update);
  }

  async function pollStats() {
    clearTimer("stats");
    const current = state.current;
    if (!current) return;
    try {
      const stats = await api(`/api/torrent-stats/${current.hash}`);
      if (current !== state.current) return;
      const speed = stats.download_speed / 1024 / 1024;
      state.lastSpeed = stats.download_speed;
      $("player-stats").textContent = stats.peers
        ? `${SF.plural(stats.peers, "peers")} · ${speed.toFixed(1)} MB/s`
        : t("finding_peers");
      if (state.stallStart && Date.now() - state.stallStart > STALL_TIMEOUT && tooSlow(current, stats.download_speed)) {
        state.stallStart = null;
        switchToFaster("stall");
      }
    } catch {
      /* best effort */
    }
    if (!playerEl.hidden) setTimer("stats", pollStats, 3000);
  }

  function renderFiles(files, currentFile) {
    const wrap = $("player-file");
    const select = $("file-select");
    wrap.hidden = files.length < 2;
    select.replaceChildren(
      ...files.map((f) =>
        el(
          "option",
          { value: String(f.index), selected: f.index === currentFile.index },
          [f.name],
        ),
      ),
    );
    select.onchange = () => {
      const { target, torrent } = state.current;
      closeMenus();
      play(target, {
        torrent,
        fileIndex: Number(select.value),
        resume: false,
        auto: true,
      });
    };
  }

  // ============================================================= automatic fallback

  // Bytes per second the playing version needs (from its size per hour of video).
  function neededSpeed(current) {
    const gbPerHour = current.torrent?.gb_per_hour || 2.5;
    return (gbPerHour * 1024 ** 3) / 3600;
  }

  const tooSlow = (current, speed) => speed < neededSpeed(current) * 0.8;

  async function fasterCandidate(current) {
    let data;
    try {
      data = await findVersions(current.target);
    } catch {
      return null;
    }
    const playing = current.torrent || {};
    const fresh = data.results.filter(
      (v) =>
        !state.tried.has(v.hash) &&
        v.hash !== current.hash &&
        !v.low &&
        !v.oversized,
    );
    const needsBrowser = playing.browser !== false;
    // Results are sorted by score, so the first match is the best of the faster ones.
    return (
      fresh.find(
        (v) =>
          v.seeders >= Math.max(10, (playing.seeders || 0) * 1.5) &&
          (!needsBrowser || v.browser === true),
      ) ||
      fresh.find((v) => v.seeders > (playing.seeders || 0)) ||
      null
    );
  }

  async function switchToFaster(reason) {
    const current = state.current;
    if (!current || state.switching) return;
    state.switching = true;
    try {
      const next = await fasterCandidate(current);
      if (current !== state.current) return;
      if (!next) {
        if (reason === "start")
          $("loading-detail").textContent = t("slow_no_alternative");
        return;
      }
      const position = current.started ? video.currentTime : null;
      const settled = current.started && video.currentTime > SETTLED_AFTER;
      const cooledDown = Date.now() - (state.lastSwitchAt || 0) > SWITCH_COOLDOWN;
      const go = () =>
        play(current.target, {
          torrent: next,
          resume: false,
          startAt: position,
          auto: true,
        });
      const label = `${qualityText(next)}, ${availabilityText(next.availability).toLowerCase()}`;
      if (
        SF.settings.get().autoSwitch &&
        current.auto &&
        !settled &&
        cooledDown &&
        state.switches < MAX_SWITCHES
      ) {
        state.switches += 1;
        state.lastSwitchAt = Date.now();
        toast(
          t(reason === "start" ? "switch_start" : "switch_stall", { label }),
        );
        go();
      } else if (reason === "start") {
        $("loading-detail").replaceChildren(
          t("slow_offer", { label }),
          " ",
          actionButton(t("switch_button"), go, true),
        );
      } else {
        toast(t("stall_offer", { label }), [
          actionButton(t("switch_button"), go, true),
        ]);
      }
    } finally {
      state.switching = false;
    }
  }

  // ============================================================= silent video detection

  const AUDIO_CHECK_DELAY = 4000;

  // true / false, or null when the browser can't tell.
  function browserDecodesAudio() {
    if (typeof video.webkitAudioDecodedByteCount === "number") {
      // Chrome/Edge: video bytes but no audio bytes = the audio codec isn't supported.
      return video.webkitVideoDecodedByteCount > 0 ? video.webkitAudioDecodedByteCount > 0 : null;
    }
    if (typeof video.mozHasAudio === "boolean") return video.mozHasAudio;
    if (video.audioTracks) return video.audioTracks.length > 0;
    return null;
  }

  async function checkAudio() {
    const current = state.current;
    if (!current || video.paused || browserDecodesAudio() !== false) return;
    current.torrent = { ...(current.torrent || {}), browser: false };
    let data;
    try {
      data = await findVersions(current.target);
    } catch {
      data = { results: [] };
    }
    if (current !== state.current) return;
    const alternative = data.results.find(
      (v) => v.browser === true && !v.low && !v.oversized && !state.tried.has(v.hash) && v.hash !== current.hash,
    );
    const copy = actionButton(t("copy_vlc"), async (event) => {
      await navigator.clipboard.writeText(video.currentSrc);
      event.target.textContent = t("copied");
    });
    if (!alternative) {
      toast(t("no_audio_vlc"), [copy]);
      return;
    }
    const label = `${qualityText(alternative)}, ${availabilityText(alternative.availability).toLowerCase()}`;
    const go = () => play(current.target, { torrent: alternative, resume: false, startAt: video.currentTime, auto: true });
    if (SF.settings.get().autoSwitch && current.auto && state.switches < MAX_SWITCHES) {
      state.switches += 1;
      state.lastSwitchAt = Date.now();
      toast(t("no_audio_switch", { label }));
      go();
    } else {
      toast(t("no_audio_offer", { label }), [actionButton(t("switch_button"), go, true), copy]);
    }
  }

  // ============================================================= custom controls

  const seek = $("seek");
  const seekPlayed = $("seek-played");
  const seekBuffered = $("seek-buffered");
  const seekThumb = $("seek-thumb");
  const seekTooltip = $("seek-tooltip");
  const volumeInput = $("ctl-volume");

  function updatePlayButton() {
    playerEl.classList.toggle("is-paused", video.paused);
  }

  function updateTimeline() {
    const duration = Number.isFinite(video.duration) ? video.duration : 0;
    const ratio = duration ? video.currentTime / duration : 0;
    seekPlayed.style.width = `${ratio * 100}%`;
    seekThumb.style.left = `${ratio * 100}%`;
    let buffered = 0;
    for (let i = 0; i < video.buffered.length; i++) {
      if (video.buffered.start(i) <= video.currentTime + 1)
        buffered = Math.max(buffered, video.buffered.end(i));
    }
    seekBuffered.style.width = duration
      ? `${(buffered / duration) * 100}%`
      : "0";
    $("ctl-time").textContent =
      `${formatTime(video.currentTime)} / ${formatTime(duration)}`;
    seek.setAttribute("aria-valuemax", String(Math.floor(duration)));
    seek.setAttribute("aria-valuenow", String(Math.floor(video.currentTime)));
    seek.setAttribute("aria-valuetext", formatTime(video.currentTime));
  }
  video.addEventListener("progress", updateTimeline);
  video.addEventListener("durationchange", updateTimeline);

  const seekRatio = (event) => {
    const rect = seek.getBoundingClientRect();
    return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  };
  let dragging = false;
  seek.addEventListener("pointerdown", (event) => {
    if (!video.duration) return;
    dragging = true;
    seek.setPointerCapture(event.pointerId);
    video.currentTime = seekRatio(event) * video.duration;
  });
  seek.addEventListener("pointermove", (event) => {
    if (!video.duration) return;
    const ratio = seekRatio(event);
    seekTooltip.hidden = false;
    seekTooltip.style.left = `${ratio * 100}%`;
    seekTooltip.textContent = formatTime(ratio * video.duration);
    if (dragging) video.currentTime = ratio * video.duration;
  });
  seek.addEventListener("pointerup", () => (dragging = false));
  seek.addEventListener("pointerleave", () => (seekTooltip.hidden = true));
  seek.addEventListener("keydown", (event) => {
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      event.stopPropagation();
      skip(event.key === "ArrowLeft" ? -10 : 10);
    }
  });

  const togglePlay = () => {
    if (!video.getAttribute("src")) return;
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  };
  const skip = (seconds) => {
    if (!video.duration) return;
    video.currentTime = Math.min(
      video.duration - 1,
      Math.max(0, video.currentTime + seconds),
    );
    wakeControls();
  };

  function applyVolume() {
    const { volume, muted } = SF.settings.get();
    video.volume = volume;
    video.muted = muted;
    volumeInput.value = String(muted ? 0 : volume);
    playerEl.classList.toggle("is-muted", muted || volume === 0);
  }
  volumeInput.addEventListener("input", () => {
    const volume = Number(volumeInput.value);
    SF.settings.set({ volume, muted: volume === 0 });
    applyVolume();
  });
  $("ctl-mute").addEventListener("click", () => {
    const { muted, volume } = SF.settings.get();
    SF.settings.set({ muted: !muted, volume: !muted ? volume : volume || 1 });
    applyVolume();
  });
  const changeVolume = (delta) => {
    const volume = Math.min(1, Math.max(0, SF.settings.get().volume + delta));
    SF.settings.set({ volume, muted: false });
    applyVolume();
  };

  const toggleFullscreen = () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else playerEl.requestFullscreen?.().catch(() => {});
  };
  document.addEventListener("fullscreenchange", () =>
    playerEl.classList.toggle(
      "is-fullscreen",
      Boolean(document.fullscreenElement),
    ),
  );

  $("ctl-play").addEventListener("click", togglePlay);
  $("ctl-back").addEventListener("click", () => skip(-10));
  $("ctl-forward").addEventListener("click", () => skip(10));
  $("ctl-fullscreen").addEventListener("click", toggleFullscreen);
  video.addEventListener("click", () => {
    if (anyMenuOpen()) closeMenus();
    else togglePlay();
  });
  video.addEventListener("dblclick", toggleFullscreen);

  // Picture-in-picture and casting (Chromecast / AirPlay) when the browser supports them
  const pipBtn = $("ctl-pip");
  if (document.pictureInPictureEnabled) {
    pipBtn.hidden = false;
    pipBtn.addEventListener("click", () => {
      (document.pictureInPictureElement
        ? document.exitPictureInPicture()
        : video.requestPictureInPicture()
      ).catch(() => {});
    });
  }
  const castBtn = $("ctl-cast");
  if (video.remote && typeof video.remote.prompt === "function") {
    video.remote
      .watchAvailability((available) => (castBtn.hidden = !available))
      .catch(() => (castBtn.hidden = false));
    castBtn.addEventListener("click", () =>
      video.remote.prompt().catch(() => toast(t("cast_unavailable"))),
    );
  } else if (window.WebKitPlaybackTargetAvailabilityEvent) {
    video.addEventListener(
      "webkitplaybacktargetavailabilitychanged",
      (event) => (castBtn.hidden = event.availability !== "available"),
    );
    castBtn.addEventListener("click", () =>
      video.webkitShowPlaybackTargetPicker(),
    );
  }

  // ============================================================= subtitles

  const subsBtn = $("subs-btn");
  const subsLabel = $("subs-label");
  const subsMenu = $("subs-menu");

  function clearTracks() {
    for (const track of video.querySelectorAll("track")) {
      URL.revokeObjectURL(track.src);
      track.remove();
    }
  }

  async function loadSubtitles() {
    const current = state.current;
    state.subs = [];
    state.activeSub = null;
    subsBtn.disabled = true;
    subsLabel.textContent = t("subs_searching");
    try {
      const data = await api("/api/subtitles/find", {
        hash: current.hash,
        file_index: current.file.index,
        tmdb_id: ctx.tmdb_id,
        media_type: ctx.media_type,
        season: current.target.season,
        episode: current.target.episode,
        languages: SF.settings.languages(),
      });
      if (current !== state.current) return;
      state.subs = data.candidates;
      subsBtn.disabled = false;
      renderSubsMenu();
      const settings = SF.settings.get();
      if (!settings.auto || !data.candidates.length) {
        subsLabel.textContent = data.candidates.length
          ? t("subs_off")
          : t("subs_none");
        return;
      }
      await applySubtitle(
        data.candidates.find((c) => c.language === settings.primary) ||
          data.best,
      );
    } catch {
      if (current === state.current) {
        subsLabel.textContent = t("subs_unavailable");
        subsBtn.disabled = false;
        renderSubsMenu();
      }
    }
  }

  function renderSubsMenu() {
    const option = (checked, title, detail, onClick) => {
      const button = el(
        "button",
        {
          type: "button",
          className: "subs-option",
          role: "menuitemradio",
          "aria-checked": String(checked),
          title: detail || null,
        },
        [
          icon("check"),
          el("span", {}, [
            el("strong", {}, [title]),
            detail ? el("small", {}, [detail]) : null,
          ]),
        ],
      );
      button.addEventListener("click", onClick);
      return button;
    };
    const options = [
      option(!state.activeSub, t("subs_off_option"), "", () => {
        SF.settings.set({ auto: false });
        applySubtitle(null);
      }),
    ];
    let group = null;
    for (const c of state.subs) {
      if (c.language_name !== group) {
        group = c.language_name;
        options.push(el("div", { className: "subs-group" }, [group]));
      }
      options.push(
        option(
          state.activeSub?.id === c.id,
          c.note ? t(`note_${c.note}`) : t("subtitles"),
          c.release,
          () => {
            SF.settings.set({ auto: true, primary: c.language });
            applySubtitle(c);
          },
        ),
      );
    }
    if (!state.subs.length)
      options.push(
        el("p", { className: "muted subs-help" }, [t("subs_not_found")]),
      );
    $("subs-options").replaceChildren(...options);
  }

  async function applySubtitle(candidate, attempt = 0) {
    clearTracks();
    state.activeSub = null;
    if (!candidate) {
      subsLabel.textContent = t("subs_off");
      renderSubsMenu();
      return;
    }
    subsLabel.textContent = `${candidate.language_name}…`;
    try {
      // Fetch first so a failed download can fall back instead of failing silently.
      const response = await fetch(candidate.url);
      if (!response.ok) throw new Error(await response.text());
      const src = URL.createObjectURL(await response.blob());
      const track = el("track", {
        kind: "subtitles",
        label: candidate.language_name,
        srclang: candidate.language,
        src,
        default: true,
      });
      track.dataset.offset = "0";
      track.addEventListener("load", () => {
        track.track.mode = "showing";
        applyOffset();
      });
      video.append(track);
      track.track.mode = "showing";
      state.activeSub = candidate;
      subsLabel.textContent = candidate.language_name;
    } catch {
      const alternatives = state.subs.filter(
        (c) => c.language === candidate.language && c.id !== candidate.id,
      );
      if (attempt < 3 && alternatives[attempt])
        return applySubtitle(alternatives[attempt], attempt + 1);
      subsLabel.textContent = t("subs_failed");
    }
    renderSubsMenu();
  }

  function applyOffset() {
    const track = video.querySelector("track");
    if (!track?.track.cues) return;
    const delta = state.subOffset - Number(track.dataset.offset);
    if (!delta) return;
    for (const cue of Array.from(track.track.cues)) {
      cue.startTime = Math.max(0, cue.startTime + delta);
      cue.endTime = Math.max(0, cue.endTime + delta);
    }
    track.dataset.offset = String(state.subOffset);
  }

  function shiftSubtitles(seconds) {
    state.subOffset = Math.round((state.subOffset + seconds) * 100) / 100;
    const text = `${state.subOffset > 0 ? "+" : ""}${state.subOffset.toFixed(1)} s`;
    $("sync-value").textContent =
      SF.lang === "cs" ? text.replace(".", ",") : text;
    applyOffset();
  }

  subsBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleMenu(subsMenu, subsBtn);
  });
  episodesBtn.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleMenu(episodesMenu, episodesBtn);
  });
  for (const menu of [subsMenu, episodesMenu])
    menu.addEventListener("click", (event) => event.stopPropagation());
  document
    .querySelectorAll("[data-sync]")
    .forEach((b) =>
      b.addEventListener("click", () => shiftSubtitles(Number(b.dataset.sync))),
    );

  // ============================================================= versions sheet

  const sheet = $("versions");
  let sheetTarget = null;

  function closeSheet() {
    sheet.hidden = true;
  }
  sheet
    .querySelectorAll("[data-close]")
    .forEach((node) => node.addEventListener("click", closeSheet));
  $("player-versions").addEventListener(
    "click",
    () => state.current && openSheet(state.current.target),
  );
  $("sheet-quality").addEventListener("change", (event) => {
    SF.settings.set({ quality: event.target.value });
    if (sheetTarget) openSheet(sheetTarget);
    if (playerEl.hidden) refreshHero();
  });

  async function openSheet(
    target,
    { focusSearch = false, browserOnly = false, query = "" } = {},
  ) {
    sheetTarget = target;
    sheet.hidden = false;
    $("sheet-quality").value = SF.settings.get().quality;
    $("versions-context").textContent = [
      ctx.title,
      hasSeason(target)
        ? t("season_episode", { s: target.season, e: target.episode })
        : null,
    ]
      .filter(Boolean)
      .join(" – ");
    const status = $("versions-status");
    status.className = "versions-status";
    status.textContent = query
      ? t("searching_query", { q: query })
      : t("searching_versions");
    $("versions-picks").replaceChildren();
    $("versions-list").replaceChildren();
    $("versions-low").hidden = true;
    $("versions-filters").hidden = true;
    if (focusSearch) {
      sheet.querySelector(".versions-advanced").open = true;
      $("manual-query").focus();
    }
    try {
      const data = await findVersions(target, query);
      if (sheetTarget !== target) return;
      renderVersions(data, target, { browserOnly });
    } catch (error) {
      status.textContent = t("search_failed", { error: error.message });
    }
  }

  function renderVersions(data, target, { browserOnly }) {
    const status = $("versions-status");
    const recommended = recommendedOf(data);
    status.replaceChildren();
    if (!data.results.length) {
      status.textContent = t("no_versions");
      sheet.querySelector(".versions-advanced").open = true;
      return;
    }
    const note = (text) =>
      el("p", { className: "note" }, [icon("alert"), el("span", {}, [text])]);
    if (data.notice === "cam_only") status.append(note(t("notice_cam")));
    else if (data.notice === "low_availability")
      status.append(note(t("notice_low")));
    if (data.failed_providers.length)
      status.append(
        el("p", { className: "muted" }, [
          t("providers_failed", { list: data.failed_providers.join(", ") }),
        ]),
      );

    const pickHashes = new Set();
    $("versions-picks").replaceChildren(
      ...[
        ["recommended", "pick_recommended"],
        ["best_quality", "pick_best"],
        ["smallest", "pick_small"],
      ]
        .map(([key, label]) => {
          const v = byHash(data, data.picks[key]);
          if (!v || pickHashes.has(v.hash)) return null;
          pickHashes.add(v.hash);
          const button = el(
            "button",
            { type: "button", className: "btn btn-play" },
            [icon("play"), t("play")],
          );
          button.addEventListener("click", () =>
            play(target, { torrent: v, resume: false }),
          );
          return el(
            "div",
            { className: `pick${key === "recommended" ? " is-primary" : ""}` },
            [
              el("span", { className: "pick-label" }, [t(label)]),
              el("span", { className: "pick-quality" }, [qualityText(v)]),
              el("span", { className: "pick-facts" }, facts(v)),
              button,
            ],
          );
        })
        .filter(Boolean),
    );

    const rest = data.results.filter((v) => !pickHashes.has(v.hash));
    const anyGood = data.results.some((v) => !v.low);
    const main = anyGood ? rest.filter((v) => !v.low) : rest;
    const low = anyGood ? rest.filter((v) => v.low) : [];

    const versionRow = (v) => {
      const item = el("li", {
        className: "version",
        dataset: { quality: v.quality || "", browser: String(v.browser) },
      });
      const button = el("button", { type: "button", className: "tool-btn" }, [
        icon("play"),
        t("play"),
      ]);
      button.addEventListener("click", () => {
        const better =
          recommended && recommended.hash !== v.hash && !recommended.low
            ? recommended
            : null;
        if (better && (v.cam || v.availability === "none")) {
          item.querySelector(".version-confirm")?.remove();
          const why = t(v.cam ? "cam_confirm" : "dead_confirm");
          item.append(
            el("div", { className: "version-confirm" }, [
              el("p", {}, [
                t("better_available", {
                  why,
                  label: `${qualityText(better)}, ${availabilityText(better.availability).toLowerCase()}`,
                }),
              ]),
              actionButton(
                t("play_better"),
                () => play(target, { torrent: better, resume: false }),
                true,
              ),
              actionButton(t("play_anyway"), () =>
                play(target, { torrent: v, resume: false }),
              ),
            ]),
          );
          return;
        }
        play(target, { torrent: v, resume: false });
      });
      item.append(
        el(
          "span",
          {
            className: "version-quality",
            dataset: { q: v.cam ? "CAM" : v.quality || "" },
          },
          [v.cam ? "CAM" : v.quality || "?"],
        ),
        el("div", { className: "version-main" }, [
          el("div", { className: "version-facts" }, facts(v)),
          el("span", { className: "version-name", title: v.name }, [v.name]),
        ]),
        button,
      );
      return item;
    };

    const list = $("versions-list");
    list.replaceChildren(...main.map(versionRow));

    const qualities = [...new Set(main.map((v) => v.quality).filter(Boolean))];
    const mixedBrowser =
      main.some((v) => v.browser === true) && main.some((v) => v.browser !== true);
    const filters = $("versions-filters");
    if (main.length > 4 && (qualities.length > 1 || mixedBrowser)) {
      let active = browserOnly && mixedBrowser ? "browser" : "all";
      const apply = () => {
        for (const row of list.children) {
          row.hidden =
            active === "browser"
              ? row.dataset.browser !== "true"
              : active !== "all" && row.dataset.quality !== active;
        }
        for (const chip of filters.children)
          chip.setAttribute(
            "aria-pressed",
            String(chip.dataset.value === active),
          );
      };
      const chip = (value, label) => {
        const node = el(
          "button",
          { type: "button", className: "chip", dataset: { value } },
          [label],
        );
        node.addEventListener("click", () => {
          active = value;
          apply();
        });
        return node;
      };
      filters.replaceChildren(
        chip("all", t("all")),
        ...qualities.map((q) => chip(q, q)),
        mixedBrowser ? chip("browser", t("browser_ok")) : null,
      );
      filters.hidden = false;
      apply();
    }

    const lowBox = $("versions-low");
    lowBox.hidden = !low.length;
    if (low.length) {
      lowBox.querySelector("summary").textContent = t("worse_versions", {
        n: low.length,
      });
      lowBox.querySelector("ul").replaceChildren(...low.map(versionRow));
    }
  }

  $("manual-search").addEventListener("submit", (event) => {
    event.preventDefault();
    const query = $("manual-query").value.trim();
    if (query && sheetTarget) openSheet(sheetTarget, { query });
  });
  $("magnet-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const magnet = $("magnet-input").value.trim();
    if (magnet && sheetTarget)
      play(sheetTarget, { torrent: { magnet }, resume: false });
  });

  // ============================================================= keyboard

  document.addEventListener("keydown", (event) => {
    if (event.target.matches("input, textarea, select")) return;
    if (event.key === "Escape") {
      if (!sheet.hidden) closeSheet();
      else if (anyMenuOpen()) closeMenus();
      else if (!playerEl.hidden && !document.fullscreenElement) closePlayer();
      return;
    }
    if (playerEl.hidden || !sheet.hidden) return;
    const key = event.key.toLowerCase();
    const actions = {
      " ": togglePlay,
      k: togglePlay,
      arrowleft: () => skip(-10),
      arrowright: () => skip(10),
      j: () => skip(-10),
      l: () => skip(10),
      arrowup: () => changeVolume(0.1),
      arrowdown: () => changeVolume(-0.1),
      m: () => $("ctl-mute").click(),
      f: toggleFullscreen,
      g: () => shiftSubtitles(-0.25),
      h: () => shiftSubtitles(0.25),
      n: () => !$("next-btn").hidden && $("next-btn").click(),
      c: () => {
        if (state.activeSub) applySubtitle(null);
        else if (state.subs.length)
          applySubtitle(
            state.subs.find((s) => s.language === SF.settings.get().primary) ||
              state.subs[0],
          );
      },
    };
    if (!actions[key]) return;
    if (event.target.closest("button") && (key === " " || key === "enter"))
      return;
    event.preventDefault();
    actions[key]();
    wakeControls();
  });

  window.addEventListener("beforeunload", () => saveProgress(true));

  // ============================================================= start

  applyVolume();
  updatePlayButton();
  renderEpisodes();
  if (location.hash.startsWith("#e"))
    document
      .getElementById(location.hash.slice(1))
      ?.scrollIntoView({ block: "center" });
  const autoplay = new URLSearchParams(location.search).get("play") === "1";
  if (autoplay)
    history.replaceState(
      null,
      "",
      detailUrl + (isTV && state.season ? `?season=${state.season}` : ""),
    );
  refreshHero({ autoplay });
})();
