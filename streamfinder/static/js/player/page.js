// The detail page around the player (file order: see core.js): the hero with its
// Play button, My list, Watched, the page's episode list and the player's episode menu -
// and, as the last of the player files, the start.
(() => {
  "use strict";

  const SF = window.SF;
  const { el, icon, api, formatTime, progress, t } = SF;
  const P = SF.player;
  const {
    ctx, $, isTV, progressKey, detailUrl, state, hasSeason, movieTarget, episodeTarget, targetKey,
    shortLabel, isAired, findVersions, recommendedOf, qualityText, play,
  } = P; // core.js
  const { episodesBtn, closeMenus, updatePlayButton, applyVolume } = P; // controls.js
  const { openSheet } = P; // sheet.js

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

  // ?s=2&e=1 (an "Up next" card on the home page): the hero offers that episode first.
  const asked = new URLSearchParams(location.search);
  let askedEpisode =
    isTV && Number(asked.get("s")) > 0 && Number(asked.get("e")) > 0
      ? { season: Number(asked.get("s")), episode: Number(asked.get("e")), runtime: ctx.runtime }
      : null;

  function heroTarget() {
    if (!isTV) return movieTarget();
    if (askedEpisode) {
      const target = askedEpisode;
      askedEpisode = null; // later (after playing it) the stored progress decides again
      return target;
    }
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

  // Mark as watched (films; episodes have their own toggle in the list)
  const watchedBtn = $("watched-btn");
  const watchedInfo = (runtime) => ({ title: ctx.title, backdrop: ctx.backdrop, url: detailUrl, runtime });
  const renderWatchedButton = () => {
    const watched = SF.isFinished(progress.get(progressKey));
    watchedBtn.setAttribute("aria-pressed", String(watched));
    watchedBtn.title = watched ? t("unmark_watched") : t("mark_watched");
    $("watched-label").textContent = watched ? t("watched") : t("mark_watched");
  };
  if (watchedBtn) {
    watchedBtn.addEventListener("click", () => {
      SF.setWatched(progressKey, watchedInfo(ctx.runtime), null, watchedBtn.getAttribute("aria-pressed") !== "true");
      renderWatchedButton();
      SF.markWatchedPosters();
      refreshHero();
    });
    renderWatchedButton();
  }

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
        const watched = SF.isFinished(p);
        const more = el(
          "button",
          { className: "episode-more", type: "button", disabled: !aired },
          [t("versions")],
        );
        more.addEventListener("click", (event) => {
          event.stopPropagation();
          openSheet(target);
        });
        const mark = el(
          "button",
          {
            className: "episode-watched",
            type: "button",
            disabled: !aired,
            "aria-pressed": String(watched),
            title: t(watched ? "unmark_episode_watched" : "mark_episode_watched", { n: ep.episode }),
            "aria-label": t(watched ? "unmark_episode_watched" : "mark_episode_watched", { n: ep.episode }),
          },
          [icon("check")],
        );
        mark.addEventListener("click", (event) => {
          event.stopPropagation();
          SF.setWatched(progressKey, watchedInfo(target.runtime), ep, !watched);
          renderEpisodes();
          refreshHero();
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
              watched ? el("span", { className: "watched-badge", title: t("watched") }, [icon("check")]) : null,
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
            el("div", { className: "episode-actions" }, [mark, more]),
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

  // For the other player files.
  Object.assign(P, {
    storedFor, isResumable, refreshHero, renderEpisodes, resolveEpisode, nextEpisodeOf,
    renderPlayerEpisodes,
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
