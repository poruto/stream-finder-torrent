// Player controls (file order: see core.js): the menus, the audio track menu, the
// timeline, volume, media keys, fullscreen, picture-in-picture, casting and the keyboard.
(() => {
  "use strict";

  const SF = window.SF;
  const { el, icon, formatTime, t } = SF;
  const P = SF.player;
  const {
    $, video, playerEl, state, setTimer, setMediaAction, closePlayer, toast, converted, pos, dur,
    startConverted, clampPosition, loadedAt, seekTo,
  } = P; // core.js
  const { rememberPref, applySubtitle, shiftSubtitles } = P.later; // subtitles.js
  const { closeSheet } = P.later; // sheet.js
  const { renderPlayerEpisodes } = P.later; // page.js

  const episodesBtn = $("episodes-btn");
  const episodesMenu = $("episodes-menu");
  const sheet = $("versions");

  // ============================================================= menus

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

  // ============================================================= audio tracks

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
    if (track.language) rememberPref({ audio: track.language.toLowerCase() });
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

  // For the other player files.
  Object.assign(P, {
    episodesBtn, episodesMenu, wakeControls, fitPlayer, closeMenus, toggleMenu, languageNames,
    renderAudioMenu, updatePlayButton, updateTimeline, applyVolume,
  });
})();
