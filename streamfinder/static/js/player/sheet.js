// The versions sheet (file order: see core.js): every version found for a film or
// episode, the picks, filters, a custom search and a magnet link of your own.
(() => {
  "use strict";

  const SF = window.SF;
  const { el, icon, t } = SF;
  const P = SF.player;
  const {
    ctx, $, playerEl, state, hasSeason, findVersions, byHash, recommendedOf, availabilityText,
    qualityText, facts, openLayer, closeLayer, actionButton, play,
  } = P; // core.js
  const { refreshHero } = P.later; // page.js

  // ============================================================= versions sheet

  const sheet = $("versions");
  const sheetHome = sheet.parentElement;
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
    // Only the full-screen element is drawn: in full screen the sheet opens inside it.
    const host = document.fullscreenElement || sheetHome;
    if (sheet.parentElement !== host && sheet.hidden) host.append(sheet);
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

  // For the other player files.
  Object.assign(P, { closeSheet, openSheet });
})();
