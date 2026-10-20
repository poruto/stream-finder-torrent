// Detail page: one-click playback of the recommended version, episode list,
// version picker, a full-screen player with custom controls, automatic
// subtitles, resume, autoplay of the next episode and automatic fallback to a
// faster source when a torrent is slow.
//
// Classic scripts sharing one object, SF.player, loaded in this order by detail.html:
//   core.js          state, targets, versions, the player shell, playback, self-healing
//   controls.js      menus, audio tracks, timeline, volume, media keys, fullscreen, keyboard
//   subtitles.js     finding, loading and timing subtitles, their menu and the sync
//   ai-subtitles.js  subtitles written from the audio on the server
//   sheet.js         the versions sheet
//   page.js          hero, My list, the episode lists - and the start
// What earlier files put in SF.player is used directly; functions of later files through
// `P.later`, which looks them up on each call (by then every file has loaded).
(() => {
  "use strict";

  const SF = window.SF;
  const { el, api, progress, t } = SF;
  const P = (SF.player = {});
  P.later = new Proxy({}, { get: (_, name) => (...args) => P[name](...args) });
  const {
    wakeControls, fitPlayer, closeMenus, renderAudioMenu, updatePlayButton, updateTimeline,
    applyVolume,
  } = P.later; // controls.js
  const { clearTracks, titlePrefs, loadSubtitles, applyOffset } = P.later; // subtitles.js
  const { closeSheet, openSheet } = P.later; // sheet.js
  const {
    storedFor, isResumable, refreshHero, renderEpisodes, resolveEpisode, nextEpisodeOf,
  } = P.later; // page.js

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
    subSync: null, // { pieces } from the server's subtitle sync
    subSyncChecks: [], // positions the active subtitle was synced at
    syncRequest: null, // AbortController of the subtitle sync in progress
    tried: new Set(), // info hashes already attempted for this title/episode
    switches: 0,
    stallStart: null,
    switching: false,
    prefetched: false,
    preloaded: null, // hash of the next episode's torrent warmed up in TorrServer
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

  // Can this browser decode HEVC (x265)? Data saver then picks small x265 versions.
  const hevcVideo = 'video/mp4; codecs="hvc1.1.6.L93.B0"';
  const hevc = (() => {
    try {
      return Boolean(window.MediaSource?.isTypeSupported?.(hevcVideo) || document.createElement("video").canPlayType(hevcVideo));
    } catch {
      return false;
    }
  })();

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
        hevc,
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
        [
          v.per_episode ? t("per_episode", { size: v.per_episode }) : v.size,
          v.gb_per_hour ? t("per_hour", { size: v.gb_per_hour.toLocaleString(SF.lang, { maximumFractionDigits: 1 }) }) : "",
        ]
          .filter(Boolean)
          .join(" · "),
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
    dropPreloaded(state.current?.hash);
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
      dropPreloaded(data.hash);
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
    const remembered = titlePrefs().audio;
    if (remembered) {
      const index = tracks.findIndex((a) => (a.language || "").toLowerCase() === remembered);
      if (index >= 0) return index;
    }
    if (SF.settings.czech()) {
      const czech = tracks.findIndex((a) => ["cze", "ces", "cs", "slk", "slo", "sk"].includes((a.language || "").toLowerCase()));
      if (czech > 0) return czech;
    }
    return 0;
  }

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
      if (next)
        findVersions(next)
          .then((data) => preloadNext(next, data))
          .catch(() => {});
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

  // Episodes started by the countdown since the last press of a key or a click.
  const STILL_WATCHING_AFTER = 3;
  state.autoPlays = 0;
  const someoneIsThere = () => (state.autoPlays = 0);
  playerEl.addEventListener("pointerdown", someoneIsThere, true);
  document.addEventListener("keydown", someoneIsThere, true);

  // The next episode's recommended version is started in TorrServer while this one plays
  // and the start of its file downloaded, so the countdown ends in a picture, not a
  // spinner. Not with Data saver (it would download an episode that may never be
  // watched) or when episodes don't play on by themselves.
  async function preloadNext(next, data) {
    const current = state.current;
    const best = recommendedOf(data);
    if (!best?.magnet || !SF.settings.get().autoNext || SF.settings.quality() === "saver") return;
    try {
      const reply = await api("/api/preload", {
        magnet: best.magnet,
        season: next.season,
        episode: next.episode,
        title: `${ctx.title} S${String(next.season).padStart(2, "0")}E${String(next.episode).padStart(2, "0")}`,
        poster: ctx.poster,
      });
      if (current === state.current) state.preloaded = reply.hash;
      else if (reply.hash !== state.current?.hash) releaseTorrent(reply.hash);
    } catch {
      /* the next episode just starts the usual way */
    }
  }
  // A warmed-up torrent nobody is going to play (another version won, or the player closed).
  function dropPreloaded(keep) {
    if (state.preloaded && state.preloaded !== keep) releaseTorrent(state.preloaded);
    state.preloaded = null;
  }

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
    // Several episodes in a row without anyone touching anything: ask before the next
    // one (asleep on the sofa - no need to download the rest of the season).
    if (state.autoPlays >= STILL_WATCHING_AFTER) {
      showMessage(t("still_watching"), t("season_episode", { s: next.season, e: next.episode }), [
        actionButton(t("still_watching_yes"), () => play(next, { resume: false }), true),
        actionButton(t("close"), closePlayer),
      ]);
      return;
    }
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
        state.autoPlays += 1;
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

  // For the other player files.
  Object.assign(P, {
    ctx, $, isTV, progressKey, detailUrl, video, playerEl, state, clearTimer, setTimer, hasSeason,
    movieTarget, episodeTarget, targetKey, shortLabel, isAired, findVersions, byHash, recommendedOf,
    availabilityText, qualityText, facts, openLayer, closeLayer, setMediaAction, closePlayer,
    actionButton, toast, play, converted, pos, dur, startConverted, clampPosition, loadedAt, seekTo,
  });
})();
