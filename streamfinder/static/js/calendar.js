// The Calendar tab: upcoming episodes and releases of the titles this household follows
// (progress and My list are shared by every device, see app.js).
(() => {
  "use strict";
  const { el, api, t } = SF;
  const status = document.getElementById("calendar-status");
  const root = document.getElementById("calendar");

  function followed() {
    const shows = new Set();
    const movies = new Set();
    for (const key of Object.keys(SF.progress.all()))
      if (key.startsWith("tv:")) shows.add(key.slice(3));
    for (const item of SF.myList.all())
      (item.type === "tv" ? shows : movies).add(String(item.id));
    return { shows: [...shows], movies: [...movies] };
  }

  const dayLabel = (iso, today) => {
    const day = new Date(`${iso}T12:00:00`);
    const days = Math.round((day - new Date(`${today}T12:00:00`)) / 86400000);
    if (days === 0) return t("calendar_today");
    if (days === 1) return t("calendar_tomorrow");
    return new Intl.DateTimeFormat(SF.lang, {
      weekday: "long",
      day: "numeric",
      month: "long",
    }).format(day);
  };

  const describe = (event) => {
    if (event.kind === "cinema") return t("calendar_cinema");
    if (event.kind === "digital") return t("calendar_digital");
    if (event.last) return t("season_episodes", { s: event.season, from: event.episode, to: event.last });
    const number = t("season_episode", { s: event.season, e: event.episode });
    return event.name ? `${number} · ${event.name}` : number;
  };

  // A whole season released on one day is one row ("Season 2, episodes 1-10").
  function mergeDrops(list) {
    const merged = [];
    for (const event of list) {
      const previous = merged[merged.length - 1];
      if (event.kind === "episode" && previous?.kind === "episode" && previous.id === event.id && previous.season === event.season) {
        previous.last = event.episode;
        previous.premiere ||= event.premiere;
      } else merged.push({ ...event });
    }
    return merged;
  }

  function render(events, today) {
    const days = new Map();
    for (const event of events) {
      if (!days.has(event.date)) days.set(event.date, []);
      days.get(event.date).push(event);
    }
    for (const [date, list] of days) days.set(date, mergeDrops(list));
    root.replaceChildren(
      ...[...days].map(([date, list]) =>
        el("section", { className: "calendar-day" }, [
          el("h2", { className: date === today ? "is-today" : null }, [
            dayLabel(date, today),
          ]),
          el(
            "ul",
            {},
            list.map((event) =>
              el("li", {}, [
                el("a", { className: "calendar-item", href: event.url }, [
                  event.poster
                    ? el("img", { src: event.poster, alt: "", loading: "lazy" })
                    : el("span", { className: "calendar-noposter" }),
                  el("span", { className: "calendar-text" }, [
                    el("strong", {}, [event.title]),
                    el("small", {}, [describe(event)]),
                  ]),
                  event.premiere
                    ? el("span", { className: "calendar-tag" }, [
                        t("calendar_season_premiere"),
                      ])
                    : null,
                  event.kind === "digital"
                    ? el("span", { className: "calendar-tag is-digital" }, [
                        "HD",
                      ])
                    : null,
                ]),
              ]),
            ),
          ),
        ]),
      ),
    );
  }

  async function load() {
    const { shows, movies } = followed();
    if (!shows.length && !movies.length) {
      status.textContent = t("calendar_empty");
      return;
    }
    try {
      const data = await api(
        `/api/calendar?shows=${shows.join(",")}&movies=${movies.join(",")}`,
      );
      status.hidden = Boolean(data.events.length);
      status.textContent = t("calendar_empty");
      render(data.events, data.today);
    } catch (error) {
      status.textContent = error.message;
    }
  }
  load();
  // Another device's progress or list arriving (app.js syncs on every page) may add titles.
  document.addEventListener("sf:shared", load);
})();
