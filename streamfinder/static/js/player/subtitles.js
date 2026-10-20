// Player subtitles (file order: see core.js): finding and loading them, the subtitles
// menu, the choice kept per title, cue timing and the sync to the speech.
(() => {
  "use strict";

  const SF = window.SF;
  const { el, icon, api, progress, t } = SF;
  const P = SF.player;
  const {
    ctx, $, progressKey, video, state, clearTimer, setTimer, toast, converted, pos,
  } = P; // core.js
  const { episodesBtn, episodesMenu, toggleMenu, languageNames } = P; // controls.js
  const { startAiSubtitles } = P.later; // ai-subtitles.js

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

  // ------------------------------------------------------------- per-title choices
  // Subtitles and audio picked by hand are kept with the title's progress (and so shared
  // by every device): the next episode starts with the same ones.
  const titlePrefs = () => progress.get(progressKey)?.prefs || {};
  function rememberPref(change) {
    progress.set(progressKey, { prefs: { ...titlePrefs(), ...change } });
    if (ctx.media_type === "tv") toast(t("remembered_for_show"));
  }
  function rememberSubtitle() {
    const sub = state.activeSub;
    if (!sub && subsLabel.textContent !== t("subs_off")) return; // it failed: not a choice
    rememberPref({
      subs: !sub
        ? { off: true }
        : sub.source === "ai"
          ? { ai: sub.translate || "" }
          : sub.translatedFrom
            ? { translate: sub.language, from: sub.translatedFrom }
            : { language: sub.language },
    });
  }
  // The remembered subtitle among this file's candidates; undefined = no (usable) choice.
  function rememberedSubtitle(candidates, best) {
    const pref = titlePrefs().subs;
    if (!pref) return undefined;
    if (pref.off) return null;
    if (pref.ai !== undefined) return state.aiAvailable ? aiCandidate(pref.ai) : undefined;
    if (pref.translate) {
      const from = candidates.find((c) => c.language === pref.from) || best;
      return from ? translatedCandidate(from, pref.translate) : undefined;
    }
    return candidates.find((c) => c.language === pref.language);
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
      const remembered = rememberedSubtitle(data.candidates, data.best);
      if (remembered !== undefined) return await applySubtitle(remembered);
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
      button.addEventListener("click", async () => {
        await onClick();
        rememberSubtitle();
      });
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

  // ------------------------------------------------------------- languages and candidates

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
        state.subSync = { pieces: result.pieces };
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

  // For the other player files.
  Object.assign(P, {
    subsLabel, clearTracks, titlePrefs, rememberPref, loadSubtitles, renderSubsMenu, applySubtitle,
    applyOffset, placeCue, translatorEngine, shiftSubtitles,
  });
})();
