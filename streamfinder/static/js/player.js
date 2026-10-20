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
  const STALL_END_MARGIN = 30; // seconds: a converted stream ending earlier than this broke off
  const HEAL_RETRIES = 2; // restarts at the same spot before another version is tried

  const state = {
    episodes: ctx.episodes || [],
    season: ctx.season,
    versions: new Map(), // "<target>|<query>|<quality>" -> Promise(search response)
    current: null, // { target, torrent, auto, started, hash, file, files, magnet }
    subs: [],
    activeSub: null,
    subOffset: 0,
    subSync: null, // { pieces, offset, scale } from the server's subtitle sync
    subSyncChecks: [], // positions the active subtitle was synced at
    syncRequest: null, // AbortController of the subtitle sync in progress
    tried: new Set(), // info hashes already attempted for this title/episode
    switches: 0,
    stallStart: null,
    switching: false,
    prefetched: false,
    timers: {},
    // Bumped by every play() and by closing the player: older async work sees it and stops.
    playGen: 0,
    playMagnet: null, // torrent the latest play() asked the server for
    subRequest: null, // AbortController of the subtitle download in progress
    convertSeq: 0, // latest startConverted() call; older ones give up
    skipTarget: null, // converted streams: where quick repeated skips are heading
    countdown: false, // the "next episode in N s" countdown is running
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
    const czech = SF.settings.czech();
    const key = `${targetKey(target)}|${query}|${quality}|${czech}`;
    if (!state.versions.has(key)) {
      const request = api("/api/torrents", {
        tmdb_id: ctx.tmdb_id,
        media_type: ctx.media_type,
        season: target.season,
        episode: target.episode,
        runtime: target.runtime,
        quality,
        query,
        czech,
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
      languageTag(v),
      v.pack ? el("span", {}, [t(`pack_${v.pack}`)]) : null,
      availabilityNode(v),
      el("span", { className: v.oversized ? "size-big" : "" }, [
        v.per_episode ? t("per_episode", { size: v.per_episode }) : v.size,
      ]),
      v.browser === true
        ? el("span", { className: "browser-ok" }, [t("browser_ok")])
        : v.browser === false && !ctx.convert
          ? el(
              "span",
              { className: "browser-no", title: t("browser_no_title") },
              [t("browser_no")],
            )
          : null,
    ];
  }

  // "CZ dabing" / "CZ titulky" / nothing for versions in the original language.
  function languageTag(v) {
    if (!v.languages.length) return null;
    const langs = v.languages.join("/");
    const kind = v.czech === "subs" ? "subs" : "dub";
    const code = v.languages[0] === "SK" ? "sk" : "cs";
    return el("span", { className: `lang-tag lang-${kind}` }, [SF.flag(code), t(`lang_${kind}`, { l: langs })]);
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

  // `saved`: the stored progress, read once by callers rendering a whole list.
  function episodeProgress(ep, saved = progress.get(progressKey) || {}) {
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
    const saved = progress.get(progressKey) || {};
    list.replaceChildren(
      ...state.episodes.map((ep) => {
        const target = episodeTarget(ep);
        const p = episodeProgress(ep, saved);
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
                : el("span", { className: "thumb-blank", "aria-hidden": "true" }, [icon("tv")]),
              aired
                ? el("span", { className: "play-badge" }, [icon("play")])
                : el("span", { className: "release-badge" }, [airLabel(ep)]),
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

  // The episodes of `season`, or null when a newer pick overtook this one.
  let seasonSeq = 0;
  async function loadSeason(season) {
    const seq = ++seasonSeq;
    const data = await api(`/api/season/${ctx.tmdb_id}/${season}`);
    if (seq !== seasonSeq) return null;
    state.season = season;
    state.episodes = data.episodes;
    return data.episodes;
  }

  // A season that didn't load: one button in place of the list that tries again.
  function seasonFailed(list, select) {
    const retry = el("button", { type: "button", className: "btn btn-ghost" }, [t("load_failed")]);
    retry.addEventListener("click", (event) => {
      event.stopPropagation();
      select.dispatchEvent(new Event("change"));
    });
    list.replaceChildren(el("li", { className: "season-failed" }, [retry]));
  }

  $("season-select")?.addEventListener("change", async (event) => {
    const list = $("episode-list");
    list.setAttribute("aria-busy", "true");
    try {
      if (!(await loadSeason(Number(event.target.value)))) return;
      renderEpisodes();
      history.replaceState(null, "", `${detailUrl}?season=${state.season}`);
    } catch {
      seasonFailed(list, event.target);
    } finally {
      list.removeAttribute("aria-busy");
    }
  });

  // Season episode lists, so episodes outside the season on screen can be checked too.
  const seasonCache = new Map();
  async function seasonEpisodes(season) {
    if (season === state.season) return state.episodes;
    if (!seasonCache.has(season)) {
      const request = api(`/api/season/${ctx.tmdb_id}/${season}`).then((d) => d.episodes);
      request.catch(() => seasonCache.delete(season));
      seasonCache.set(season, request);
    }
    return seasonCache.get(season);
  }

  // Fill in name/air date/runtime for a bare {season, episode}; null if it doesn't exist.
  async function resolveEpisode(target) {
    if (!isTV || !hasSeason(target) || target.airDate) return target;
    try {
      const ep = (await seasonEpisodes(target.season)).find((e) => e.episode === target.episode);
      return ep ? episodeTarget(ep) : null;
    } catch {
      return target; // unknown: let the torrent search decide
    }
  }

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
      try {
        if (!(await loadSeason(Number(playerSeason.value)))) return;
      } catch {
        seasonFailed($("episodes-menu-list"), playerSeason);
        return;
      }
      renderPlayerEpisodes();
      renderEpisodes();
    });
  }

  function renderPlayerEpisodes() {
    const current = state.current?.target;
    const saved = progress.get(progressKey) || {};
    playerSeason.value = String(state.season);
    $("episodes-menu-list").replaceChildren(
      ...state.episodes.map((ep) => {
        const aired = isAired(ep.air_date);
        const isCurrent =
          current &&
          current.season === ep.season &&
          current.episode === ep.episode;
        const p = episodeProgress(ep, saved);
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

  // This page's id for its conversions on the server: two viewers of one file each get
  // their own ffmpeg instead of replacing each other's.
  const VIEWER = Math.random().toString(36).slice(2, 12);

  // While the player or the versions sheet is open, everything behind it is inert (no Tab
  // or TV-remote focus there, Space doesn't press a hidden button), and on close focus
  // goes back where it was.
  const layers = []; // [{ node, returnFocus }], the topmost last

  function updateInert() {
    const top = layers.length ? layers[layers.length - 1].node : null;
    for (const node of document.body.children) node.inert = Boolean(top) && node !== top;
  }
  function openLayer(node, focusNode = node) {
    if (layers.some((layer) => layer.node === node)) return;
    layers.push({ node, returnFocus: document.activeElement });
    updateInert();
    focusNode.focus({ preventScroll: true });
  }
  function closeLayer(node) {
    const index = layers.findIndex((layer) => layer.node === node);
    if (index < 0) return;
    const [{ returnFocus }] = layers.splice(index, 1);
    updateInert();
    if (returnFocus?.isConnected && !returnFocus.closest("[inert]")) returnFocus.focus({ preventScroll: true });
  }

  // Lock screen, headset and TV-remote media keys.
  const media = "mediaSession" in navigator ? navigator.mediaSession : null;
  function setMediaAction(action, handler) {
    try {
      media?.setActionHandler(action, handler);
    } catch {
      /* action not supported by this browser */
    }
  }
  function setMediaSession(target) {
    if (!media || typeof MediaMetadata === "undefined") return;
    media.metadata = new MediaMetadata({
      title: hasSeason(target) ? `${ctx.title} ${shortLabel(target)}` : ctx.title,
      artist: target.name || "",
      artwork: ctx.poster ? [{ src: ctx.poster }] : [],
    });
  }
  // Positions as the user sees them (a converted stream's own clock starts at its offset).
  function updatePositionState() {
    if (!media?.setPositionState || !dur()) return;
    try {
      media.setPositionState({
        duration: dur(),
        position: Math.min(Math.max(0, pos()), dur()),
        playbackRate: video.playbackRate || 1,
      });
    } catch {
      /* best effort */
    }
  }

  function openPlayer(target) {
    $("player-title").textContent = ctx.title;
    $("player-subtitle").textContent = hasSeason(target)
      ? `${t("season_episode", { s: target.season, e: target.episode })}${target.name ? ` · ${target.name}` : ""}`
      : "";
    const opening = playerEl.hidden;
    playerEl.hidden = false;
    document.body.style.overflow = "hidden";
    if (opening) {
      openLayer(playerEl);
      // The phone's or TV remote's Back button then closes the player, not the page.
      history.pushState({ player: true }, "");
    }
    fitPlayer();
    closeMenus();
    $("next-btn").hidden = true;
    $("player-stats").textContent = "";
    $("player-toast").hidden = true;
    $("player-buffering").hidden = true;
    clearTimer("next");
    clearTimer("skip");
    state.countdown = false;
    state.skipTarget = null;
    state.prefetched = false;
    setMediaSession(target);
    wakeControls();
  }

  // Tell TorrServer to stop downloading a torrent we no longer play (fire and forget).
  const releaseTorrent = (hash) =>
    fetch(`/api/torrent-stop/${hash}`, { method: "POST", keepalive: true }).catch(() => {});
  function stopTorrent(hash) {
    video.pause();
    releaseTorrent(hash);
  }

  function closePlayer() {
    if (playerEl.hidden) return;
    // Whatever play() is still waiting for must not start the video behind the page.
    state.playGen += 1;
    saveProgress(true);
    if (state.current?.hash) stopTorrent(state.current.hash);
    Object.keys(state.timers).forEach(clearTimer);
    state.countdown = false;
    state.skipTarget = null;
    video.pause();
    video.removeAttribute("src");
    video.load();
    clearTracks();
    playerEl.hidden = true;
    document.body.style.overflow = "";
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    state.current = null;
    closeLayer(playerEl);
    if (media) media.metadata = null;
    setMediaAction("nexttrack", null);
    if (history.state?.player) history.back();
    refreshHero();
    renderEpisodes();
  }
  $("player-close").addEventListener("click", closePlayer);

  window.addEventListener("popstate", () => {
    closeSheet();
    closePlayer();
  });
  // Reloaded while the player was open: its history entry would need a second Back.
  if (history.state?.player) history.replaceState(null, "");
  // Leaving the page (or the browser discarding it): keep the position, stop the download.
  window.addEventListener("pagehide", () => {
    saveProgress(true);
    if (state.current?.hash) releaseTorrent(state.current.hash);
  });
  // Back from the browser's page cache: that torrent was stopped, start from the page again.
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) closePlayer();
  });

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

  // "Copy link for VLC". navigator.clipboard only exists on https or localhost; on a LAN
  // address over plain http the old way still works, and failing that the link is shown.
  function copyButton(getUrl) {
    return actionButton(t("copy_vlc"), async (event) => {
      const button = event.currentTarget;
      const url = getUrl();
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(url);
        } else {
          const area = el("textarea", { readonly: true, style: "position:fixed;top:0;left:0;opacity:0" });
          area.value = url;
          playerEl.append(area); // inside the player: it may be the fullscreen element
          area.select();
          area.setSelectionRange(0, url.length);
          const copied = document.execCommand("copy");
          area.remove();
          if (!copied) throw new Error("copy failed");
        }
        button.textContent = t("copied");
      } catch {
        window.prompt(t("copy_vlc"), url);
      }
    });
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
    !$("subs-menu").hidden ||
    !$("audio-menu").hidden ||
    !episodesMenu.hidden ||
    !$("versions").hidden;

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
  // Size the player to the area really visible (phones hide part of the layout viewport
  // behind the browser bars) and tell the CSS how tall the controls are, so subtitles
  // can sit above them.
  const fitPlayer = () => {
    if (playerEl.hidden) return;
    const height = document.fullscreenElement ? 0 : window.visualViewport?.height;
    if (height) playerEl.style.setProperty("--player-h", `${Math.round(height)}px`);
    else playerEl.style.removeProperty("--player-h");
    playerEl.style.setProperty("--controls-h", `${$("player-controls").offsetHeight}px`);
  };
  window.visualViewport?.addEventListener("resize", fitPlayer);
  window.addEventListener("resize", fitPlayer);
  window.addEventListener("orientationchange", () => setTimeout(fitPlayer, 300));
  document.addEventListener("fullscreenchange", fitPlayer);
  new ResizeObserver(fitPlayer).observe($("player-controls"));

  playerEl.addEventListener("mousemove", wakeControls);
  playerEl.addEventListener("touchstart", wakeControls, { passive: true });

  function closeMenus() {
    for (const [menu, button] of [
      [$("subs-menu"), $("subs-btn")],
      [$("audio-menu"), $("audio-btn")],
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
    // Only the latest play() may touch the player: a slower earlier one (another
    // episode clicked meanwhile, or the player closed) stops at its next await.
    const gen = ++state.playGen;
    const stale = () => gen !== state.playGen;
    closeSheet();
    target = (await resolveEpisode(target)) || target;
    if (stale()) return;
    if (hasSeason(target) && target.airDate && !isAired(target.airDate)) {
      openPlayer(target);
      showMessage(t("not_released"), t("episode_airs_on", { date: new Date(target.airDate).toLocaleDateString(SF.lang) }), [
        actionButton(t("close"), closePlayer, true),
      ]);
      return;
    }
    openPlayer(target);
    if (state.current?.hash && torrent?.hash && torrent.hash !== state.current.hash) {
      // A different version was picked: silence and stop the old one at once.
      stopTorrent(state.current.hash);
      video.removeAttribute("src");
      video.load();
    }
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
          if (stale()) return;
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
      state.playMagnet = torrent.magnet;
      const data = await api(
        "/api/play",
        {
          magnet: torrent.magnet,
          season: fileIndex === null ? target.season : null,
          episode: fileIndex === null ? target.episode : null,
          file_index: fileIndex,
          title: hasSeason(target)
            ? `${ctx.title} S${String(target.season).padStart(2, "0")}E${String(target.episode).padStart(2, "0")}`
            : ctx.title,
          poster: ctx.poster,
        },
      );
      if (stale()) {
        // Closed meanwhile, or another play() took over with a different torrent: don't
        // let this one download for nothing.
        if (playerEl.hidden || state.playMagnet !== torrent.magnet) releaseTorrent(data.hash);
        return;
      }
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
      // Another torrent than the one playing: stop that one downloading right away.
      if (state.current?.hash && state.current.hash !== data.hash) stopTorrent(state.current.hash);
      state.current = {
        target,
        torrent,
        auto,
        started: false,
        hash: data.hash,
        file: data.file,
        files: data.files,
        magnet: data.magnet,
        streamUrl: data.stream_url,
      };
      renderFiles(data.files, data.file);
      $("audio-btn").hidden = true;
      const mode = await chooseMode(state.current);
      if (stale()) return; // another play() started meanwhile
      renderAudioMenu();
      setStep("buffer", data.file.name);
      if (mode) await startConverted(startAt, mode);
      else startVideo(data.stream_url, startAt);
      if (stale()) return;
      loadSubtitles();
      const next = await resolveEpisode(nextEpisodeOf(target) || {});
      if (stale()) return;
      const nextAvailable = next && hasSeason(next) && (!next.airDate || isAired(next.airDate));
      const playNext = () => nextAvailable && play(next, { resume: false });
      $("next-btn").hidden = !nextAvailable;
      $("next-btn").onclick = playNext;
      setMediaAction("nexttrack", nextAvailable ? playNext : null);
    } catch (error) {
      if (stale()) return; // superseded (or aborted): its failure doesn't matter now
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

  // ------------------------------------------------------------- converted playback
  // Files whose audio (AC3/DTS/...) or container (Matroska outside Chromium) the
  // browser can't handle are streamed through ffmpeg: video copied, audio -> AAC.
  // The stream then starts at `offset`, so all positions go through pos()/dur().

  const isChromium = Boolean(navigator.userAgentData?.brands?.some((b) => b.brand === "Chromium"));
  const converted = () => state.current?.convert;
  const pos = () => (converted() ? converted().offset + video.currentTime : video.currentTime);
  const dur = () => {
    if (converted()) return converted().duration || 0;
    return Number.isFinite(video.duration) ? video.duration : 0;
  };

  // null = play the file directly, else { copyAudio } for the ffmpeg route.
  async function chooseMode(current) {
    const always = SF.settings.get().alwaysConvert;
    if (current.torrent?.browser === true && !always) return null; // known AAC/H.264: no need to look
    let info;
    try {
      info = await api(`/api/probe/${current.hash}/${current.file.index}`);
    } catch {
      return null;
    }
    if (!info.available || !info.probed) return null;
    current.probe = info;
    checkRuntime(current, info.duration);
    const track = preferredAudio(info.audio || []);
    current.audioTrack = track;
    const chosen = (info.audio || [])[track];
    if (track > 0) return { copyAudio: Boolean(chosen?.browser_ok), audio: track };
    if (always) return { copyAudio: info.audio_ok };
    if (!info.audio_ok) return { copyAudio: false };
    if (info.matroska && !isChromium) return { copyAudio: true };
    return null;
  }

  // Index of the audio track to play: the user's pick, else Czech/Slovak if wanted, else the first.
  function preferredAudio(tracks) {
    if (tracks.length < 2) return 0;
    if (SF.settings.czech()) {
      const czech = tracks.findIndex((a) => ["cze", "ces", "cs", "slk", "slo", "sk"].includes((a.language || "").toLowerCase()));
      if (czech > 0) return czech;
    }
    return 0;
  }

  const LANG3 = { cze: "cs", ces: "cs", slk: "sk", slo: "sk", eng: "en", ger: "de", deu: "de", fre: "fr", fra: "fr", spa: "es", ita: "it", pol: "pl", rus: "ru", hun: "hu", jpn: "ja", kor: "ko", chi: "zh", zho: "zh", por: "pt", dut: "nl", nld: "nl", ukr: "uk" };
  const languageNames = (() => {
    try {
      return new Intl.DisplayNames([SF.lang], { type: "language" });
    } catch {
      return null;
    }
  })();
  function audioLabel(track, i) {
    const code = (track.language || "").toLowerCase();
    const short = LANG3[code] || (code.length === 2 ? code : "");
    let name = "";
    try {
      name = short && languageNames ? languageNames.of(short) : "";
    } catch {
      name = "";
    }
    name = name ? name[0].toUpperCase() + name.slice(1) : t("audio_track_n", { n: i + 1 });
    const channels = track.channels ? (track.channels >= 6 ? "5.1" : track.channels === 2 ? "stereo" : `${track.channels} ch`) : "";
    const detail = [track.title, (track.codec || "").toUpperCase(), channels].filter(Boolean).join(" · ");
    return { name, detail, code: short };
  }

  function renderAudioMenu() {
    const current = state.current;
    const tracks = current?.probe?.audio || [];
    const button = $("audio-btn");
    button.hidden = tracks.length < 2;
    if (button.hidden) return;
    const active = current.audioTrack || 0;
    $("audio-label").textContent = audioLabel(tracks[active], active).name;
    $("audio-options").replaceChildren(
      ...tracks.map((track, i) => {
        const { name, detail, code } = audioLabel(track, i);
        // Same three columns as the subtitle options: check, flag, text.
        const option = el(
          "button",
          { type: "button", className: "subs-option", role: "menuitemradio", "aria-checked": String(i === active) },
          [
            icon("check"),
            code ? SF.flag(code) : el("span", { className: "flag flag-none" }),
            el("span", { className: "subs-text" }, [el("strong", {}, [name]), detail ? el("small", {}, [detail]) : null]),
          ],
        );
        option.addEventListener("click", () => switchAudio(i));
        return option;
      }),
    );
  }

  // Another audio track: the server's converter picks it (and converts it if the browser can't play it).
  function switchAudio(index) {
    const current = state.current;
    const track = current?.probe?.audio?.[index];
    if (!track || index === (current.audioTrack || 0)) return closeMenus();
    current.audioTrack = index;
    closeMenus();
    renderAudioMenu();
    toast(t("audio_switching", { name: audioLabel(track, index).name }));
    $("player-buffering").hidden = false;
    startConverted(pos(), { copyAudio: Boolean(track.browser_ok), audio: index });
    if (state.ai) {
      // The transcript belongs to the old track.
      applySubtitle(state.activeSub);
    }
  }
  $("audio-btn").addEventListener("click", (event) => {
    event.stopPropagation();
    toggleMenu($("audio-menu"), $("audio-btn"));
  });
  $("audio-menu").addEventListener("click", (event) => event.stopPropagation());
  for (const button of document.querySelectorAll("[data-close-menu]")) button.addEventListener("click", closeMenus);

  async function startConverted(position, mode) {
    const current = state.current;
    // Quick seeks each ask for a keyframe; only the latest one may start its stream
    // (the answers can come back in any order).
    const seq = ++state.convertSeq;
    let start = 0;
    if (position > 1) {
      try {
        start = (await api(`/api/keyframe/${current.hash}/${current.file.index}?t=${position.toFixed(1)}`)).start;
      } catch {
        start = position;
      }
    }
    if (current !== state.current || seq !== state.convertSeq) return;
    renderAudioMenu();
    const audio = mode.audio ?? current.audioTrack ?? 0;
    current.convert = { offset: start, duration: current.probe?.duration || 0, copyAudio: mode.copyAudio, audio };
    const url = `/api/transcode/${current.hash}/${current.file.index}.mp4?start=${start}${mode.copyAudio ? "&copy=1" : ""}${audio ? `&audio=${audio}` : ""}&sid=${VIEWER}`;
    // Land exactly on the requested second once the stream (starting at the keyframe) is playing.
    const skipTo = position - start;
    startVideo(url, skipTo > 0.5 ? skipTo : 0, { keepTracks: true });
    applyOffset();
  }

  const clampPosition = (position) => Math.min(Math.max(0, position), Math.max(0, dur() - 1));

  // The stream's own time for `position` when that part is already loaded, else null.
  function loadedAt(position) {
    const local = position - (converted()?.offset || 0);
    const buffered = video.buffered;
    for (let i = 0; i < buffered.length; i++) {
      if (local >= buffered.start(i) && local <= buffered.end(i)) return local;
    }
    return null;
  }

  function seekTo(position) {
    const target = clampPosition(position);
    const convert = converted();
    if (!convert) {
      video.currentTime = target;
      return;
    }
    // Inside what is already loaded: a normal seek. Otherwise restart ffmpeg there.
    const local = loadedAt(target);
    if (local !== null) {
      video.currentTime = local;
      return;
    }
    state.seekedAt = Date.now();
    $("player-buffering").hidden = false;
    startConverted(target, { copyAudio: convert.copyAudio, audio: convert.audio });
  }

  function startVideo(url, startAt, { keepTracks = false } = {}) {
    if (!keepTracks) clearTracks();
    state.stallStart = null;
    applyVolume();
    video.src = url;
    // One pending start position: a stream replaced before its metadata arrived must not
    // seek the next one too.
    video.removeEventListener("loadedmetadata", state.onMetadata);
    state.onMetadata = () => {
      if (!converted()) checkRuntime(state.current, video.duration);
      if (startAt > 0 && (converted() || startAt < video.duration - 10)) video.currentTime = startAt;
    };
    video.addEventListener("loadedmetadata", state.onMetadata, { once: true });
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
    cancelCountdown(); // going back after the end: stay in this episode
  });
  video.addEventListener("waiting", () => {
    if (!state.current) return;
    // One spinner at a time: the small buffering one only once the loading screen is gone.
    $("player-buffering").hidden = !$("player-loading").hidden;
    const afterSeek = Date.now() - (state.seekedAt || 0) < SEEK_GRACE;
    if (state.current.started && !afterSeek) state.stallStart ??= Date.now();
  });
  video.addEventListener("playing", () => {
    if (state.current && !state.current.started) {
      const current = state.current;
      setTimer(
        "audio",
        () => current === state.current && checkAudio(),
        AUDIO_CHECK_DELAY,
      );
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
    // A converted stream fails like this too when the server had no free conversion slot
    // (503) or TorrServer stopped delivering: start it again in a moment.
    const played = state.current.started || video.currentTime > 0;
    if (converted() && played && (!dur() || brokeOff())) return healConverted({ delay: 3000, unsupported });
    showStreamError(unsupported);
  });

  // The stream can't go on: say so and offer another version, VLC or (`retry`) another try.
  function showStreamError(unsupported, retry = null) {
    const target = state.current.target;
    // This stream is done: no slow-start fallback behind the message, no more stats.
    clearTimer("start");
    clearTimer("stats");
    const copy = copyButton(() => state.current?.streamUrl || video.currentSrc);
    showMessage(
      t(unsupported ? "unsupported_title" : "load_failed_title"),
      t(unsupported ? "unsupported_text" : "load_failed_text"),
      [
        actionButton(
          t("choose_other"),
          () => openSheet(target, { browserOnly: unsupported }),
          true,
        ),
        retry ? actionButton(t("retry"), retry) : null,
        copy,
        actionButton(t("close"), closePlayer),
      ],
    );
  }

  // A converted stream that stops well before the known end was cut off (the server gives
  // up after 60 s without data from TorrServer): not the end of the film.
  const brokeOff = () => Boolean(converted()) && dur() > 0 && pos() < dur() - STALL_END_MARGIN;

  // Start a cut-off or failed converted stream again where it stopped. After HEAL_RETRIES
  // restarts at the same spot, the usual fallback to a faster version takes over.
  function healConverted({ delay = 0, unsupported = false } = {}) {
    const current = state.current;
    const convert = converted();
    const position = pos();
    const restart = () =>
      current === state.current && startConverted(position, { copyAudio: convert.copyAudio, audio: convert.audio });
    const heal = current.heal && Math.abs(position - current.heal.at) < 10 ? current.heal : { at: position, count: 0 };
    current.heal = heal;
    clearTimer("start");
    if (heal.count >= HEAL_RETRIES) {
      current.heal = null;
      showStreamError(unsupported, () => {
        $("player-message").hidden = true;
        $("player-buffering").hidden = false;
        restart();
      });
      switchToFaster("stall");
      return;
    }
    heal.count += 1;
    // One spinner at a time: the small one only once the loading screen is gone.
    $("player-buffering").hidden = !$("player-loading").hidden;
    setTimer("heal", restart, delay);
  }

  video.addEventListener("timeupdate", () => {
    saveProgress(false);
    updateTimeline();
    // Prepare the next episode while this one is still playing.
    if (
      !state.prefetched &&
      state.current &&
      dur() &&
      pos() / dur() > PREFETCH_AT
    ) {
      state.prefetched = true;
      const next = nextEpisodeOf(state.current.target);
      if (next) findVersions(next).catch(() => {});
    }
  });
  video.addEventListener("pause", () => {
    saveProgress(true);
    updatePlayButton();
    updatePositionState();
    wakeControls();
  });
  video.addEventListener("play", () => {
    updatePlayButton();
    cancelCountdown();
  });
  for (const type of ["playing", "seeked", "durationchange", "ratechange"]) video.addEventListener(type, updatePositionState);

  // Stops the "next episode in N s" countdown, if one is running.
  function cancelCountdown() {
    if (!state.countdown) return;
    state.countdown = false;
    clearTimer("next");
    $("player-message").hidden = true;
  }

  video.addEventListener("ended", () => {
    if (state.current && brokeOff()) return healConverted(); // cut off, not the end of the film
    saveProgress(true);
    const next = state.current && nextEpisodeOf(state.current.target);
    if (!next || !SF.settings.get().autoNext) return;
    let seconds = 8;
    const playNow = actionButton(
      t("play_now"),
      () => play(next, { resume: false }),
      true,
    );
    const cancel = actionButton(t("cancel"), cancelCountdown);
    const tick = () => {
      showMessage(
        t("next_in", { n: seconds }),
        t("season_episode", { s: next.season, e: next.episode }),
        [playNow, cancel],
      );
      if (seconds-- <= 0) {
        state.countdown = false;
        play(next, { resume: false });
      } else setTimer("next", tick, 1000);
    };
    state.countdown = true;
    tick();
  });

  function saveProgress(force) {
    const current = state.current;
    if (!current || !dur() || !current.started) return;
    if (video.ended && brokeOff()) return; // cut off, not watched to the end

    const now = Date.now();
    if (!force && now - (state.lastSave || 0) < 5000) return;
    state.lastSave = now;
    const entry = {
      position: Math.floor(pos()),
      duration: Math.floor(dur()),
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
      if (
        state.stallStart &&
        Date.now() - state.stallStart > STALL_TIMEOUT &&
        tooSlow(current, stats.download_speed)
      ) {
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
      const position = current.started ? pos() : null;
      const settled = current.started && pos() > SETTLED_AFTER;
      const cooledDown =
        Date.now() - (state.lastSwitchAt || 0) > SWITCH_COOLDOWN;
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

  // ============================================================= wrong film detection
  // A file far shorter or longer than the film's runtime on TMDB is another film with
  // the same name, a sample or a TV cut: an automatic pick moves on to the next version.
  // Loose on purpose - extended editions run up to ~1.5x the theatrical cut.
  const RUNTIME_MIN = 0.6;
  const RUNTIME_MAX = 1.6;

  async function checkRuntime(current, seconds) {
    const minutes = current?.target?.runtime;
    if (!current || current.runtimeChecked || ctx.media_type !== "movie" || !minutes) return;
    if (!Number.isFinite(seconds) || seconds <= 0) return;
    current.runtimeChecked = true;
    const ratio = seconds / (minutes * 60);
    if (!current.auto || (ratio >= RUNTIME_MIN && ratio <= RUNTIME_MAX)) return;
    let data;
    try {
      data = await findVersions(current.target);
    } catch {
      return;
    }
    if (current !== state.current) return;
    const next = data.results.find((v) => !state.tried.has(v.hash) && v.hash !== current.hash && !v.low && !v.oversized);
    if (!next) return;
    toast(t("wrong_runtime", { found: Math.round(seconds / 60), expected: minutes }));
    play(current.target, { torrent: next, resume: false, auto: true });
  }

  // ============================================================= silent video detection

  const AUDIO_CHECK_DELAY = 4000;

  // true / false, or null when the browser can't tell.
  function browserDecodesAudio() {
    if (typeof video.webkitAudioDecodedByteCount === "number") {
      // Chrome/Edge: video bytes but no audio bytes = the audio codec isn't supported.
      return video.webkitVideoDecodedByteCount > 0
        ? video.webkitAudioDecodedByteCount > 0
        : null;
    }
    if (typeof video.mozHasAudio === "boolean") return video.mozHasAudio;
    if (video.audioTracks) return video.audioTracks.length > 0;
    return null;
  }

  async function checkAudio() {
    const current = state.current;
    if (!current || video.paused || converted() || browserDecodesAudio() !== false) return;
    current.torrent = { ...(current.torrent || {}), browser: false };
    // Best fix: keep this version and let the server convert its audio.
    if (current.probe?.available !== false) {
      const info = current.probe || (await api(`/api/probe/${current.hash}/${current.file.index}`).catch(() => null));
      if (current !== state.current) return;
      if (info?.available) {
        if (info.probed) current.probe = info;
        current.audioTrack = preferredAudio(info.audio || []);
        toast(t("converting_audio"));
        startConverted(pos(), { copyAudio: false, audio: current.audioTrack });
        return;
      }
    }
    let data;
    try {
      data = await findVersions(current.target);
    } catch {
      data = { results: [] };
    }
    if (current !== state.current) return;
    const alternative = data.results.find(
      (v) =>
        v.browser === true &&
        !v.low &&
        !v.oversized &&
        !state.tried.has(v.hash) &&
        v.hash !== current.hash,
    );
    const copy = copyButton(() => video.currentSrc);
    if (!alternative) {
      toast(t("no_audio_vlc"), [copy]);
      return;
    }
    const label = `${qualityText(alternative)}, ${availabilityText(alternative.availability).toLowerCase()}`;
    const go = () =>
      play(current.target, {
        torrent: alternative,
        resume: false,
        startAt: pos(),
        auto: true,
      });
    if (
      SF.settings.get().autoSwitch &&
      current.auto &&
      state.switches < MAX_SWITCHES
    ) {
      state.switches += 1;
      state.lastSwitchAt = Date.now();
      toast(t("no_audio_switch", { label }));
      go();
    } else {
      toast(t("no_audio_offer", { label }), [
        actionButton(t("switch_button"), go, true),
        copy,
      ]);
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
    if (dragging) return; // the bar follows the finger, not the video
    const duration = dur();
    const now = state.skipTarget ?? pos(); // quick skips still being added up: show where to
    const ratio = duration ? Math.min(1, now / duration) : 0;
    seekPlayed.style.width = `${ratio * 100}%`;
    seekThumb.style.left = `${ratio * 100}%`;
    const offset = converted()?.offset || 0;
    let buffered = 0;
    for (let i = 0; i < video.buffered.length; i++) {
      if (video.buffered.start(i) <= video.currentTime + 1) buffered = Math.max(buffered, offset + video.buffered.end(i));
    }
    seekBuffered.style.width = duration
      ? `${(buffered / duration) * 100}%`
      : "0";
    $("ctl-time").textContent =
      `${formatTime(now)} / ${formatTime(duration)}`;
    seek.setAttribute("aria-valuemax", String(Math.floor(duration)));
    seek.setAttribute("aria-valuenow", String(Math.floor(now)));
    seek.setAttribute("aria-valuetext", formatTime(now));
  }
  video.addEventListener("progress", updateTimeline);
  video.addEventListener("durationchange", updateTimeline);

  const seekRatio = (event) => {
    const rect = seek.getBoundingClientRect();
    return Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
  };
  let dragging = false;
  // Direct streams seek while dragging, at most every DRAG_SEEK_EVERY ms (each seek is a
  // new request to TorrServer); converted ones once, on release (each restarts ffmpeg).
  const DRAG_SEEK_EVERY = 250;
  let dragRatio = null;
  let dragSeekAt = 0;
  let dragSeekRatio = null; // where the last seek during this drag went
  const showRatio = (ratio) => {
    seekPlayed.style.width = `${ratio * 100}%`;
    seekThumb.style.left = `${ratio * 100}%`;
  };
  const dragSeek = (ratio) => {
    dragSeekAt = Date.now();
    dragSeekRatio = ratio;
    seekTo(ratio * dur());
  };
  seek.addEventListener("pointerdown", (event) => {
    if (!dur()) return;
    dragging = true;
    seek.setPointerCapture(event.pointerId);
    dragRatio = seekRatio(event);
    dragSeekRatio = null;
    showRatio(dragRatio);
    if (!converted()) dragSeek(dragRatio);
  });
  seek.addEventListener("pointermove", (event) => {
    if (!dur()) return;
    const ratio = seekRatio(event);
    seekTooltip.hidden = false;
    seekTooltip.style.left = `${ratio * 100}%`;
    seekTooltip.textContent = formatTime(ratio * dur());
    if (dragging) {
      dragRatio = ratio;
      showRatio(ratio);
      if (!converted() && Date.now() - dragSeekAt >= DRAG_SEEK_EVERY) dragSeek(ratio);
    }
  });
  const endDrag = () => {
    // The final position, unless the last seek already went exactly there.
    if (dragging && dragRatio !== null && dragRatio !== dragSeekRatio) seekTo(dragRatio * dur());
    dragging = false;
    dragRatio = null;
    updateTimeline();
  };
  seek.addEventListener("pointerup", endDrag);
  seek.addEventListener("pointercancel", endDrag);
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
    if (!dur()) return;
    wakeControls();
    // Converted streams restart ffmpeg for every seek outside what is loaded: quick
    // presses (a TV remote's arrow held down) are added up into one restart.
    const pending = state.skipTarget;
    const target = clampPosition((pending ?? pos()) + seconds);
    if (converted() && (pending !== null || loadedAt(target) === null)) {
      state.skipTarget = target;
      updateTimeline();
      setTimer(
        "skip",
        () => {
          const to = state.skipTarget;
          state.skipTarget = null;
          if (to !== null) seekTo(to);
        },
        400,
      );
      return;
    }
    seekTo(target);
  };

  // Media keys, lock screen and TV-remote buttons (next track is set per episode in play()).
  setMediaAction("play", () => video.paused && togglePlay());
  setMediaAction("pause", () => !video.paused && togglePlay());
  setMediaAction("seekbackward", (details) => skip(-(details.seekOffset || 10)));
  setMediaAction("seekforward", (details) => skip(details.seekOffset || 10));
  setMediaAction("seekto", (details) => {
    if (dur() && Number.isFinite(details.seekTime)) seekTo(details.seekTime);
  });

  // A mouse click on a player button doesn't keep the focus there, so Space and the arrow
  // keys keep controlling the video; keyboard and TV-remote focus still work as usual.
  playerEl.addEventListener("mousedown", (event) => {
    if (event.target.closest("button")) event.preventDefault();
  });

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

  const isPhone = window.matchMedia("(hover: none) and (pointer: coarse)");
  const toggleFullscreen = () => {
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
      return;
    }
    playerEl
      .requestFullscreen?.({ navigationUI: "hide" })
      .then(() => {
        // Phones: a wide video fills the screen only when turned sideways.
        if (isPhone.matches && video.videoWidth >= video.videoHeight) {
          screen.orientation?.lock?.("landscape").catch(() => {});
        }
      })
      .catch(() => {});
  };
  document.addEventListener("fullscreenchange", () => {
    playerEl.classList.toggle("is-fullscreen", Boolean(document.fullscreenElement));
    if (!document.fullscreenElement) screen.orientation?.unlock?.();
  });

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
    // Downloads and AI windows still on their way belong to what is being removed.
    state.subRequest?.abort();
    state.subRequest = null;
    state.ai?.controller.abort();
    state.ai = null;
    $("ai-status").hidden = true;
    state.syncRequest?.abort();
    state.syncRequest = null;
    clearTimer("subsync-check");
    showSyncStatus("");
    // Hide first: Chrome keeps painting the last cue of a removed track that was showing.
    for (const textTrack of video.textTracks) textTrack.mode = "disabled";
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
      state.aiAvailable = Boolean(data.ai);
      state.translators = data.translators || ["google"];
      subsBtn.disabled = false;
      renderSubsMenu();
      const settings = SF.settings.get();
      if (!settings.auto || (!data.candidates.length && !(settings.subsMissing === "ai" && state.aiAvailable))) {
        subsLabel.textContent = data.candidates.length
          ? t("subs_off")
          : t("subs_none");
        return;
      }
      const mine = data.candidates.find((c) => c.language === settings.primary);
      if (mine) return await applySubtitle(mine);
      // Nothing in my language: translate another one, or write them from the audio.
      if (settings.subsMissing === "ai" && state.aiAvailable) return await applySubtitle(aiCandidate(settings.aiTarget ?? "en"));
      if (settings.subsMissing === "translate" && data.best)
        return await applySubtitle(translatedCandidate(data.candidates.find((c) => c.language === settings.fallback) || data.best, settings.primary));
      await applySubtitle(data.best);
    } catch {
      if (current === state.current) {
        subsLabel.textContent = t("subs_unavailable");
        subsBtn.disabled = false;
        renderSubsMenu();
      }
    }
  }

  // "Interstellar.2014.1080p.BluRay.x264-YIFY" -> "Interstellar 2014 1080p BluRay x264 YIFY"
  const readableRelease = (name) => (name || "").replace(/\.(srt|sub|ass|vtt)$/i, "").replace(/[._-]+/g, " ").trim();

  // Target languages for AI subtitles (Google Translate handles all of these).
  const AI_LANGUAGES = ["cs", "sk", "en", "de", "pl", "hu", "fr", "es", "it", "pt", "nl", "ru", "uk", "hr", "ro", "sv", "da", "fi", "no", "tr", "ja", "ko", "zh"];

  // Which language list is open under "All subtitles".
  let subsTab = null;

  function renderSubsMenu() {
    // One row: [check] [flag] title / detail. `badge`: "AI" etc. instead of a flag.
    const option = (checked, code, title, detail, onClick, badge = "") => {
      const button = el(
        "button",
        { type: "button", className: "subs-option", role: "menuitemradio", "aria-checked": String(checked), title: detail || null },
        [
          icon("check"),
          badge ? el("span", { className: "flag flag-badge", "aria-hidden": "true" }, [badge]) : code ? SF.flag(code) : el("span", { className: "flag flag-none" }),
          el("span", { className: "subs-text" }, [el("strong", {}, [title]), detail ? el("small", {}, [detail]) : null]),
        ],
      );
      button.addEventListener("click", onClick);
      return button;
    };
    const heading = (text) => el("div", { className: "subs-group" }, [text]);
    const settings = SF.settings.get();
    const active = state.activeSub;
    const mine = settings.primary;
    const byLanguage = new Map();
    for (const c of state.subs) {
      if (!byLanguage.has(c.language)) byLanguage.set(c.language, []);
      byLanguage.get(c.language).push(c);
    }
    const rank = (code) => (code === mine ? 0 : code === settings.fallback ? 1 : 2);
    const languages = [...byLanguage.keys()].sort((x, y) => rank(x) - rank(y));
    const sourceText = (c) => (c.note ? t(`note_${c.note}`) : t(`subs_source_${c.source === "torrent" ? "torrent" : "online"}`));
    const pick = (c) => () => {
      SF.settings.set({ auto: true });
      applySubtitle(c);
    };

    // -- one click: what most people want
    const options = [option(!active, "", t("subs_off_option"), "", () => {
      SF.settings.set({ auto: false });
      applySubtitle(null);
    })];
    const recommended = [];
    const best = byLanguage.get(mine)?.[0];
    if (best) {
      recommended.push(option(active?.id === best.id, mine, languageName(mine), `${t("subs_best_match")} · ${sourceText(best)}`, pick(best)));
    } else {
      // Nothing in my language: the best other one, machine-translated.
      const from = byLanguage.get(settings.fallback)?.[0] || state.subs[0];
      if (from) {
        const tr = translatedCandidate(from, mine);
        recommended.push(option(active?.id === tr.id, mine, t("subs_translated_from", { lang: languageName(mine), from: languageName(from.language) }), t(`translator_${translatorEngine()}`), pick(tr)));
      }
    }
    // -- real subtitles you can download (and their machine translation), below the AI ones
    const downloads = el("section", { className: "subs-section" }, [
      el("div", { className: "subs-section-head" }, [el("strong", {}, [t("subs_section_downloads")]), el("small", {}, [t("subs_section_downloads_help")])]),
    ]);
    if (recommended.length) downloads.append(heading(t("subs_recommended")), ...recommended);

    // -- every subtitle, one language at a time
    if (languages.length) {
      downloads.append(heading(t("subs_all")));
      if (!languages.includes(subsTab)) subsTab = active && byLanguage.has(active.language) && !active.translatedFrom ? active.language : languages[0];
      const bar = el("div", { className: "subs-tabs", role: "tablist" });
      for (const code of languages) {
        const tab = el("button", { type: "button", className: "chip lang-chip", role: "tab", "aria-selected": String(code === subsTab), title: languageName(code) }, [
          SF.flag(code),
          el("span", {}, [languageName(code)]),
          el("small", {}, [String(byLanguage.get(code).length)]),
        ]);
        tab.addEventListener("click", () => {
          subsTab = code;
          renderSubsMenu();
        });
        bar.append(tab);
      }
      downloads.append(bar);
      const subs = byLanguage.get(subsTab);
      const rows = subs.map((c, i) => option(active?.id === c.id, "", readableRelease(c.release) || t("subs_version_n", { n: i + 1 }), sourceText(c), pick(c)));
      if (subsTab !== mine) {
        // Right here: this language translated into mine, one click.
        const tr = translatedCandidate(subs[0], mine);
        rows.unshift(option(active?.id === tr.id, mine, t("subs_translate_into", { from: languageName(subsTab), to: languageName(mine) }), t(`translator_${translatorEngine()}`), pick(tr)));
      }
      const current = rows.findIndex((row) => row.getAttribute("aria-checked") === "true");
      const shown = Math.max(5, current + 1);
      const list = el("div", { className: "subs-list" }, rows.slice(0, shown));
      if (rows.length > shown) list.append(el("details", { className: "subs-more" }, [el("summary", {}, [t("subs_more", { n: rows.length - shown })]), ...rows.slice(shown)]));
      downloads.append(list);
    } else {
      downloads.append(el("p", { className: "muted subs-help" }, [t("subs_not_found")]));
    }
    // -- AI subtitles first: written from the audio, translated into a language picked here
    if (state.aiAvailable) {
      const target = settings.aiTarget ?? "en";
      const select = el("select", { className: "subs-ai-target", "aria-label": t("ai_target") }, [
        el("option", { value: "" }, [t("ai_target_none")]),
        ...AI_LANGUAGES.map((code) => el("option", { value: code }, [languageName(code)])),
      ]);
      select.value = target;
      select.addEventListener("change", () => {
        SF.settings.set({ aiTarget: select.value });
        // Already showing AI subtitles: switch them to the new language right away.
        if (state.activeSub?.source === "ai") applySubtitle(aiCandidate(select.value));
        else renderSubsMenu();
      });
      const ai = aiCandidate(target);
      const spoken = aiCandidate("");
      const section = el("section", { className: "subs-section subs-section-ai" }, [
        el("div", { className: "subs-section-head" }, [
          el("strong", {}, [t("subs_section_ai")]),
          el("small", {}, [t("subs_section_ai_help")]),
        ]),
        el("label", { className: "subs-ai-row" }, [el("span", {}, [t("ai_target")]), select]),
        option(active?.id === ai.id, target, target ? t("subs_ai_into", { lang: languageName(target) }) : t("subs_ai_original"), target ? t("subs_ai_route", { lang: languageName(target), engine: t(`engine_${translatorEngine()}`) }) : t("subs_ai_original_help"), () => applySubtitle(ai), "AI"),
      ]);
      if (target) section.append(option(active?.id === spoken.id, "", t("subs_ai_original"), t("subs_ai_original_help"), () => applySubtitle(spoken), "AI"));
      options.push(section);
    }
    options.push(downloads);
    $("subs-options").replaceChildren(...options);
    $("subs-autosync").disabled = !active || active.source === "ai";
  }

  async function applySubtitle(candidate, attempt = 0) {
    clearTracks();
    state.activeSub = null;
    if (!candidate) {
      subsLabel.textContent = t("subs_off");
      renderSubsMenu();
      return;
    }
    if (candidate.source === "ai") return startAiSubtitles(candidate);
    subsLabel.textContent = candidate.translatedFrom ? t("subs_translating") : `${candidate.language_name}…`;
    // Another pick, "Off" or another episode meanwhile aborts this one (clearTracks).
    const request = (state.subRequest = new AbortController());
    const current = state.current;
    const outdated = () => request !== state.subRequest || current !== state.current;
    try {
      // Fetch first so a failed download can fall back instead of failing silently.
      const response = await fetch(candidate.url, { signal: request.signal });
      if (!response.ok) throw new Error(await response.text());
      const blob = await response.blob();
      if (outdated()) return;
      state.subRequest = null;
      const src = URL.createObjectURL(blob);
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
      state.subSync = null;
      state.subSyncChecks = [];
      subsLabel.textContent = candidate.language_name;
      // Subtitles from the torrent or matched by file hash are made for this file;
      // everything else gets aligned to the speech once playback has started.
      if (candidate.source !== "torrent" && !candidate.hash_match) {
        const whenPlaying = () => {
          if (current !== state.current || candidate !== state.activeSub) return;
          if (video.paused || pos() < 5) return setTimer("subsync", whenPlaying, 3000);
          syncSubtitles();
        };
        setTimer("subsync", whenPlaying, 4000);
      }
    } catch {
      if (outdated()) return; // aborted or replaced: nothing to fall back for
      const alternatives = state.subs.filter(
        (c) => c.language === candidate.language && c.id !== candidate.id,
      );
      if (attempt < 3 && alternatives[attempt])
        return applySubtitle(alternatives[attempt], attempt + 1);
      subsLabel.textContent = t("subs_failed");
    }
    renderSubsMenu();
  }

  // Cue time = original mapped by the automatic sync + the user's manual offset,
  // minus the start of a converted stream (whose clock begins at 0 there).
  function applyOffset() {
    const cues = video.querySelector("track")?.track.cues;
    if (!cues || !cues.length) return;
    Array.from(cues).forEach(placeCue);
  }

  function placeCue(cue) {
    cue.orig ??= [cue.startTime, cue.endTime];
    const shift = state.subOffset - (converted()?.offset || 0);
    // Not clamped to 0: after a seek in a converted stream the lines before its start
    // would all pile up at 0:00. Negative times are simply never shown.
    cue.startTime = mapTime(cue.orig[0]) + shift;
    cue.endTime = mapTime(cue.orig[1]) + shift;
  }

  // Subtitle time -> video time by the pieces the sync measured (sync.py `mapped`):
  // inside a piece, before the first and after the last t * scale + offset, between
  // two pieces a straight line from one to the other.
  function mapTime(t) {
    const pieces = state.subSync?.pieces;
    if (!pieces?.length) return t;
    let i = 0;
    while (i < pieces.length - 1 && pieces[i + 1].start <= t) i += 1;
    const p = pieces[i];
    const at = (piece, x) => x * piece.scale + piece.offset;
    if (t < p.start || t <= p.end || i === pieces.length - 1) return at(p, t);
    const q = pieces[i + 1];
    const a = at(p, p.end);
    return a + ((t - p.end) / (q.start - p.end)) * (at(q, q.start) - a);
  }

  // ------------------------------------------------------------- AI subtitles
  // Made on the server from the audio, one 30 s window at a time, a window ahead
  // of the playback position (same grid as the server's subtitle sync).

  const AI_WINDOW = 15; // short windows: the first lines arrive within a few seconds
  const languageName = (code) => {
    try {
      const name = languageNames?.of(code) || code;
      return name[0].toUpperCase() + name.slice(1);
    } catch {
      return code;
    }
  };
  // AI translation when the server has a Groq/OpenAI key (and it isn't turned off), else Google.
  const translatorEngine = () => {
    const choice = SF.settings.get().translation;
    return choice !== "google" && (state.translators || []).includes("ai") ? "ai" : "google";
  };
  const translatedCandidate = (c, target) => ({
    ...c,
    id: `${c.id}|${target}`,
    syncId: c.id, // the original text is what matches the speech
    url: `/api/subtitles/${c.id}.vtt?translate=${target}&engine=${translatorEngine()}`,
    language: target,
    language_name: `${languageName(target)} (${t("subs_machine")})`,
    translatedFrom: c.language,
  });
  const aiCandidate = (translate) => ({
    id: `ai-${translate || "spoken"}`,
    source: "ai",
    translate,
    language: translate || "",
    language_name: t("subs_ai_label"),
  });

  async function startAiSubtitles(candidate) {
    const blob = new Blob(["WEBVTT\n\n"], { type: "text/vtt" });
    const track = el("track", { kind: "subtitles", label: "AI", srclang: candidate.translate || "und", src: URL.createObjectURL(blob), default: true });
    video.append(track);
    track.track.mode = "showing";
    // `controller` is aborted together with the track: the server stops working on its windows.
    const ai = { candidate, track, current: state.current, windows: new Map(), inflight: 0, failures: 0, warned: false, controller: new AbortController() };
    state.ai = ai;
    state.activeSub = candidate;
    state.subSync = null;
    subsLabel.textContent = t("subs_ai_starting");
    renderSubsMenu();
    // Cues added before the (empty) file has loaded would be dropped when it loads.
    await new Promise((resolve) => {
      track.addEventListener("load", resolve, { once: true });
      setTimeout(resolve, 1500);
    });
    if (state.ai === ai) aiTick();
  }

  // "AI is listening..." over the video while the lines for this moment are still being made.
  function updateAiStatus() {
    const ai = state.ai;
    const box = $("ai-status");
    const waiting = ai && !ai.stopped && ai.current === state.current && ai.windows.get(Math.floor(pos() / AI_WINDOW)) !== "done";
    // Called on every timeupdate: touch the live region only when something changes,
    // or screen readers keep announcing it.
    if (box.hidden !== !waiting) box.hidden = !waiting;
    if (!waiting) return;
    const text = t(ai.slow ? "subs_ai_waiting_torrent" : "subs_ai_listening");
    const node = $("ai-status-text");
    if (node.textContent !== text) node.textContent = text;
  }

  function aiTick() {
    const ai = state.ai;
    updateAiStatus();
    if (!ai || ai.stopped || ai.current !== state.current || !state.current) return;
    // One window at a time, the one playing now first (parallel requests only slow
    // each other down); one that is nearly over is skipped in favour of the next.
    const now = pos();
    let k = Math.floor(now / AI_WINDOW);
    if (now - k * AI_WINDOW > AI_WINDOW - 3 && ai.windows.get(k) !== "done") k += 1;
    for (const n of [k, k + 1, k + 2]) {
      if (ai.windows.get(n) !== "done") {
        aiFetch(n);
        break;
      }
    }
  }
  video.addEventListener("timeupdate", aiTick);

  async function aiFetch(n) {
    const ai = state.ai;
    if (!ai || n < 0 || ai.windows.has(n) || ai.inflight >= 1) return;
    if (Date.now() < (ai.retryAt || 0)) return; // the server asked to wait
    if (dur() && n * AI_WINDOW >= dur()) return;
    const current = ai.current;
    ai.windows.set(n, "loading");
    ai.inflight += 1;
    const params = new URLSearchParams({
      start: String(n * AI_WINDOW),
      duration: String(AI_WINDOW),
      audio: String(current.audioTrack || 0),
      translate: ai.candidate.translate || "",
      engine: translatorEngine(),
      via: SF.settings.get().aiVia === "direct" ? "direct" : "english",
      language: ai.spoken || "",
    });
    let retryIn = 0;
    try {
      const response = await fetch(`/api/subtitles/ai/${current.hash}/${current.file.index}?${params}`, { signal: ai.controller.signal });
      const data = await response.json().catch(() => ({}));
      if (state.ai !== ai) return;
      if (!response.ok || !data.success) {
        ai.windows.delete(n);
        if ([429, 503, 504].includes(response.status)) {
          retryIn = (data.retry_after || 10) * 1000;
          ai.slow = response.status === 504;
          if (!ai.warned) toast(t(response.status === 429 ? "subs_ai_limit" : "subs_ai_slow"));
          ai.warned = true;
        } else if ([401, 501].includes(response.status) || ++ai.failures > (response.status === 502 ? 1 : 4)) {
          // 502: the speech server doesn't answer - say so instead of "listening" for a minute.
          ai.stopped = true;
          subsLabel.textContent = t("subs_failed");
          toast(t("subs_ai_failed", { error: data.error || response.statusText }));
        } else {
          retryIn = 5000;
        }
      } else {
        ai.failures = 0;
        ai.slow = false;
        ai.windows.set(n, "done");
        ai.spoken ||= data.language || ""; // later windows: known language, English route if useful
        const cues = ai.track.track;
        for (const line of data.lines || []) {
          const cue = new VTTCue(line.start, line.end, line.text);
          placeCue(cue);
          cues.addCue(cue);
        }
        if (subsLabel.textContent === t("subs_ai_starting")) subsLabel.textContent = t("subs_ai_label");
      }
    } catch {
      if (state.ai !== ai) return; // aborted together with its track
      ai.windows.delete(n);
      retryIn = 5000;
    } finally {
      ai.inflight -= 1;
      updateAiStatus();
    }
    if (ai.stopped) return;
    if (retryIn) {
      // Wait it out: timeupdate would otherwise ask again a moment later.
      ai.retryAt = Date.now() + retryIn - 100; // the timer below may fire a hair early
      setTimer("ai", aiTick, retryIn);
    } else aiTick();
  }

  // Align the active subtitle to the speech around the current position. The server
  // reports what it is doing as it goes ("Listening 2/5…"), shown over the video and
  // in the subtitles menu. `quiet`: a later re-check, shown only when it changes something.
  const RECHECK = 600; // seconds of film between re-checks (another cut, slow drift)

  async function syncSubtitles({ quiet = false } = {}) {
    const current = state.current;
    const sub = state.activeSub;
    if (!current || !sub || sub.source === "ai" || state.syncRequest) return;
    const request = (state.syncRequest = new AbortController());
    const outdated = () => request !== state.syncRequest || current !== state.current || sub !== state.activeSub;
    const position = pos();
    state.subSyncChecks.push(position);
    const button = $("subs-autosync");
    button.disabled = true;
    button.textContent = t("syncing_subs");
    if (!quiet) showSyncStatus(t("syncing_subs"), true);
    try {
      const result = await syncStream(
        {
          hash: current.hash,
          file_index: current.file.index,
          subtitle: sub.syncId || sub.id,
          position,
          audio: current.audioTrack || 0,
        },
        request.signal,
        (step) => {
          if (!quiet && !outdated()) showSyncStatus(syncStepText(step), true);
        },
      );
      if (outdated()) return;
      if (result.pieces?.length) {
        state.subSync = { pieces: result.pieces, offset: result.offset, scale: result.scale };
        applyOffset();
      }
      if (!quiet || (result.certain && result.changed)) showSyncStatus(syncResultText(result));
      setTimer("subsync-check", followSync, 30000);
    } catch {
      if (outdated()) return; // aborted: another subtitle, another episode
      if (!quiet) showSyncStatus(t("subs_sync_failed"));
    } finally {
      if (request === state.syncRequest) state.syncRequest = null;
      button.disabled = !state.activeSub || state.activeSub.source === "ai";
      button.textContent = t("sync_by_audio");
    }
  }

  // Once synced, look again wherever the film has got far from every checked place:
  // a release cut differently needs another offset after the cut.
  function followSync() {
    const sub = state.activeSub;
    if (!state.current || !sub || sub.source === "ai") return;
    const far = state.subSyncChecks.every((p) => Math.abs(pos() - p) >= RECHECK);
    if (far && !video.paused && !state.syncRequest) syncSubtitles({ quiet: true });
    else setTimer("subsync-check", followSync, 30000);
  }

  // POST to the sync endpoint and hand each progress line to `onStep` as it arrives;
  // resolves with the last line (the result).
  async function syncStream(body, signal, onStep) {
    const response = await fetch("/api/subtitles/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(body),
      signal,
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `HTTP ${response.status}`);
    }
    let result = null;
    let rest = "";
    const handle = (line) => {
      if (!line.trim()) return;
      const step = JSON.parse(line);
      if (step.done) result = step;
      else onStep(step);
    };
    if (response.body?.getReader) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const lines = (rest + decoder.decode(value, { stream: true })).split("\n");
        rest = lines.pop();
        lines.forEach(handle);
      }
    } else rest = await response.text(); // no streaming here: all lines at the end
    rest.split("\n").forEach(handle);
    if (!result?.success) throw new Error(result?.error || "sync failed");
    return result;
  }

  const syncStepText = (step) =>
    step.stage === "listening" ? t("sync_listening", { n: step.step, total: step.steps }) : t("sync_matching");

  // "Synced: +1.24 s · frame rate fixed" - the shift where the film is now.
  function syncSummary() {
    const pieces = state.subSync?.pieces || [];
    const parts = [t("sync_done", { s: formatShift(mapTime(pos()) - pos()) })];
    if (pieces.some((p) => Math.abs(p.scale - 1) > 0.001)) parts.push(t("sync_fps"));
    if (new Set(pieces.map((p) => Math.round(p.offset * 2))).size > 1) parts.push(t("sync_parts"));
    return parts.join(" · ");
  }

  function syncResultText(result) {
    if (result.certain) return result.applied ? syncSummary() : t("subs_already_synced");
    if (result.applied) return t("sync_unsure_kept", { s: syncSummary() });
    return t(result.baseline >= 0.45 ? "subs_already_synced" : "subs_sync_unsure");
  }

  // The sync's state over the video (looks like "AI is listening") and in the subtitles
  // menu; a final message leaves the video after a few seconds but stays in the menu.
  function showSyncStatus(text, busy = false) {
    const box = $("sync-status");
    const line = $("subs-sync-state");
    clearTimer("sync-status");
    box.hidden = !text;
    line.hidden = !text;
    $("sync-status-text").textContent = text;
    line.textContent = text;
    $("sync-dots").hidden = !busy;
    if (text && !busy) setTimer("sync-status", () => (box.hidden = true), 5000);
  }

  const formatShift = (seconds) => {
    const digits = Math.abs(seconds) < 10 ? 2 : 1; // the sync is exact to a few hundredths
    const text = `${seconds > 0 ? "+" : ""}${seconds.toFixed(digits)} s`;
    return SF.lang === "cs" ? text.replace(".", ",") : text;
  };

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
  $("subs-autosync").addEventListener("click", () => syncSubtitles());
  document
    .querySelectorAll("[data-sync]")
    .forEach((b) =>
      b.addEventListener("click", () => shiftSubtitles(Number(b.dataset.sync))),
    );

  // ============================================================= versions sheet

  const sheet = $("versions");
  let sheetTarget = null;
  let sheetSeq = 0; // latest search shown in the sheet (quality or query changed meanwhile)

  function closeSheet() {
    if (sheet.hidden) return;
    sheet.hidden = true;
    closeLayer(sheet);
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
    const seq = ++sheetSeq;
    sheetTarget = target;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    const opening = sheet.hidden;
    sheet.hidden = false;
    if (opening) openLayer(sheet, sheet.querySelector(".sheet-panel"));
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
      if (seq !== sheetSeq) return;
      renderVersions(data, target, { browserOnly });
    } catch (error) {
      if (seq === sheetSeq) status.textContent = t("search_failed", { error: error.message });
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
        dataset: { quality: v.quality || "", browser: String(v.browser), lang: v.czech || "orig" },
      });
      const button = el("button", { type: "button", className: "tool-btn version-play", "aria-label": t("play") }, [
        icon("play"),
        el("span", {}, [t("play")]),
      ]);
      item.addEventListener("click", (event) => {
        if (!event.target.closest("button, .version-confirm")) button.click();
      });
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
    const langOf = (v) => v.czech || "orig";
    const groups = SF.settings.czech() ? ["dub", "subs", "orig"] : ["orig", "dub", "subs"];
    const present = groups.filter((g) => main.some((v) => langOf(v) === g));
    if (present.length > 1) {
      // Clear sections instead of one long mixed list.
      list.replaceChildren(
        ...present.flatMap((g) => [
          el("li", { className: "version-group", dataset: { lang: g } }, [t(`versions_group_${g}`)]),
          ...main.filter((v) => langOf(v) === g).map(versionRow),
        ]),
      );
    } else {
      list.replaceChildren(...main.map(versionRow));
    }

    const qualities = [...new Set(main.map((v) => v.quality).filter(Boolean))];
    const mixedBrowser =
      main.some((v) => v.browser === true) &&
      main.some((v) => v.browser !== true);
    const filters = $("versions-filters");
    if (main.length > 4 && (qualities.length > 1 || mixedBrowser)) {
      let active = browserOnly && mixedBrowser ? "browser" : "all";
      const apply = () => {
        for (const row of list.querySelectorAll(".version")) {
          row.hidden =
            active === "browser"
              ? row.dataset.browser !== "true"
              : active !== "all" && row.dataset.quality !== active;
        }
        // A language section without visible versions disappears too.
        for (const head of list.querySelectorAll(".version-group")) {
          head.hidden = ![...list.querySelectorAll(`.version[data-lang="${head.dataset.lang}"]`)].some((row) => !row.hidden);
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
    // TV remotes: Down from the player (controls hidden) goes to its buttons, Up from the
    // buttons comes back (where the arrows seek again).
    if (event.key === "ArrowDown" && event.target === playerEl && playerEl.classList.contains("is-idle")) {
      const first = [...playerEl.querySelectorAll(".controls-row button")].find((b) => !b.disabled && b.offsetParent !== null);
      if (first) {
        event.preventDefault();
        first.focus();
        wakeControls();
        return;
      }
    }
    if (event.key === "ArrowUp" && event.target.closest(".controls-row button")) {
      event.preventDefault();
      playerEl.focus({ preventScroll: true });
      wakeControls();
      return;
    }
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
    // Arrows seek and change the volume only when no control has the focus: on a
    // control they move between the buttons (TV remotes, spatial navigation).
    if (key.startsWith("arrow") && ![document.body, video, playerEl, seek].includes(event.target)) return;
    event.preventDefault();
    actions[key]();
    wakeControls();
  });

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
