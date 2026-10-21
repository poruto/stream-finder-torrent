// AI subtitles in the player (file order: see core.js).
(() => {
  "use strict";

  const SF = window.SF;
  const { el, t } = SF;
  const P = SF.player;
  const { $, video, state, setTimer, toast, pos, dur } = P; // core.js
  const { subsLabel, renderSubsMenu, placeCue, translatorEngine, subtitleTime } = P; // subtitles.js

  // ------------------------------------------------------------- AI subtitles
  // Made on the server from the audio, one window at a time, a window ahead of the
  // playback position (same grid as the server's subtitle sync). Subtitles translated
  // by the AI come the same way: the stretch being watched first, instead of waiting
  // minutes for the whole film (a free Groq account translates ~8000 tokens a minute).

  const AI_WINDOW = 15; // short windows: the first lines arrive within a few seconds
  const TRANSLATE_WINDOW = 60; // seconds of subtitle per translation request

  function startAiSubtitles(candidate) {
    return startWindows(candidate, {
      size: AI_WINDOW,
      now: pos,
      label: "AI",
      starting: "subs_ai_starting",
      started: () => t("subs_ai_label"),
      waiting: (ai) => (ai.slow ? "subs_ai_waiting_torrent" : "subs_ai_listening"),
      failed: "subs_ai_failed",
      url: (ai, n) => {
        const params = new URLSearchParams({
          start: String(n * AI_WINDOW),
          duration: String(AI_WINDOW),
          audio: String(ai.current.audioTrack || 0),
          translate: candidate.translate || "",
          engine: translatorEngine(),
          via: SF.settings.get().aiVia === "direct" ? "direct" : "english",
          language: ai.spoken || "",
        });
        return `/api/subtitles/ai/${ai.current.hash}/${ai.current.file.index}?${params}`;
      },
    });
  }

  // A downloaded subtitle machine-translated by the AI, a window of its own timing at a time.
  function startTranslatedSubtitles(candidate) {
    return startWindows(candidate, {
      size: TRANSLATE_WINDOW,
      now: () => subtitleTime(pos()),
      label: candidate.language_name,
      starting: "subs_translating",
      started: () => candidate.language_name,
      waiting: () => "subs_translating",
      failed: "subs_translate_failed",
      url: (ai, n) => {
        const params = new URLSearchParams({ translate: candidate.language, engine: "ai", start: String(n * TRANSLATE_WINDOW), duration: String(TRANSLATE_WINDOW) });
        return `/api/subtitles/${encodeURIComponent(candidate.syncId)}/translated?${params}`;
      },
    });
  }

  async function startWindows(candidate, kind) {
    const blob = new Blob(["WEBVTT\n\n"], { type: "text/vtt" });
    const track = el("track", { kind: "subtitles", label: kind.label, srclang: candidate.translate || candidate.language || "und", src: URL.createObjectURL(blob), default: true });
    video.append(track);
    track.track.mode = "showing";
    // `controller` is aborted together with the track: the server stops working on its windows.
    const ai = { candidate, kind, track, current: state.current, windows: new Map(), inflight: 0, failures: 0, warned: false, controller: new AbortController() };
    state.ai = ai;
    state.activeSub = candidate;
    state.subSync = null;
    state.subSyncChecks = [];
    subsLabel.textContent = t(kind.starting);
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
    const waiting = ai && !ai.stopped && ai.current === state.current && ai.windows.get(Math.floor(ai.kind.now() / ai.kind.size)) !== "done";
    // Called on every timeupdate: touch the live region only when something changes,
    // or screen readers keep announcing it.
    if (box.hidden !== !waiting) box.hidden = !waiting;
    if (!waiting) return;
    const text = t(ai.kind.waiting(ai));
    const node = $("ai-status-text");
    if (node.textContent !== text) node.textContent = text;
  }

  function aiTick() {
    const ai = state.ai;
    updateAiStatus();
    if (!ai || ai.stopped || ai.current !== state.current || !state.current) return;
    // One window at a time, the one playing now first (parallel requests only slow
    // each other down); one that is nearly over is skipped in favour of the next.
    const { size } = ai.kind;
    const now = ai.kind.now();
    let k = Math.floor(now / size);
    if (now - k * size > size - 3 && ai.windows.get(k) !== "done") k += 1;
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
    if (dur() && n * ai.kind.size >= dur()) return;
    ai.windows.set(n, "loading");
    ai.inflight += 1;
    let retryIn = 0;
    try {
      const response = await fetch(ai.kind.url(ai, n), { signal: ai.controller.signal });
      const data = await response.json().catch(() => ({}));
      if (state.ai !== ai) return;
      if (!response.ok || !data.success) {
        ai.windows.delete(n);
        if ([429, 503, 504].includes(response.status)) {
          retryIn = (data.retry_after || 10) * 1000;
          ai.slow = response.status === 504;
          if (!ai.warned) toast(t(response.status === 429 ? "subs_ai_limit" : "subs_ai_slow"));
          ai.warned = true;
        } else if ([401, 404, 501].includes(response.status) || ++ai.failures > (response.status === 502 ? 1 : 4)) {
          // 502: the speech server doesn't answer - say so instead of "listening" for a minute.
          ai.stopped = true;
          subsLabel.textContent = t("subs_failed");
          toast(t(ai.kind.failed, { error: data.error || response.statusText }));
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
        if (subsLabel.textContent === t(ai.kind.starting)) subsLabel.textContent = ai.kind.started();
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

  // For the other player files.
  Object.assign(P, { startAiSubtitles, startTranslatedSubtitles });
})();
