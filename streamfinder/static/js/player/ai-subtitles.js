// AI subtitles in the player (file order: see core.js).
(() => {
  "use strict";

  const SF = window.SF;
  const { el, t } = SF;
  const P = SF.player;
  const { $, video, state, setTimer, toast, pos, dur } = P; // core.js
  const { subsLabel, renderSubsMenu, placeCue, translatorEngine } = P; // subtitles.js

  // ------------------------------------------------------------- AI subtitles
  // Made on the server from the audio, one 30 s window at a time, a window ahead
  // of the playback position (same grid as the server's subtitle sync).

  const AI_WINDOW = 15; // short windows: the first lines arrive within a few seconds

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

  // For the other player files.
  Object.assign(P, { startAiSubtitles });
})();
