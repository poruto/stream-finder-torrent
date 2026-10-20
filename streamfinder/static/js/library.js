// Storage and Backup on the Status page (admin): the disk at a glance, what the app's
// cache is made of, TorrServer's torrents as posters, clean-up buttons, and the backup.
(() => {
  "use strict";
  const { el, api, t } = SF;
  const $ = (id) => document.getElementById(id);
  const summary = $("library-summary");
  const SHOWN = 12; // torrents before "Show all"
  let showAll = false;
  let last = null;

  const size = (bytes) => {
    const units = ["B", "kB", "MB", "GB", "TB"];
    let value = bytes || 0;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const text = `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
    return SF.lang === "cs" ? text.replace(".", ",") : text;
  };
  const date = (seconds) =>
    seconds ? new Date(seconds * 1000).toLocaleDateString(SF.lang) : "";
  const share = (part, whole) =>
    `${Math.max(0, Math.min(100, (part / (whole || 1)) * 100)).toFixed(2)}%`;

  // The disk: other files, StreamFinder's cache, free space - one bar with a legend.
  function renderDisk(data) {
    const app = data.disk.cache + data.disk.whisper;
    const parts = [
      ["other", t("library_disk_other"), Math.max(0, data.disk_used - app)],
      ["app", "StreamFinder", app],
      ["free", t("library_disk_free"), data.free],
    ];
    $("storage-free").textContent = t("library_disk_summary", {
      free: size(data.free),
      total: size(data.disk_total),
    });
    $("storage-bar").replaceChildren(
      ...parts.map(([kind, , bytes]) =>
        el("span", {
          className: `storage-bar-${kind}`,
          style: `width:${share(bytes, data.disk_total)}`,
        }),
      ),
    );
    $("storage-bar").setAttribute(
      "aria-label",
      parts.map(([, label, bytes]) => `${label} ${size(bytes)}`).join(", "),
    );
    $("storage-legend").replaceChildren(
      ...parts.map(([kind, label, bytes]) =>
        el("li", {}, [
          el("i", { className: `storage-bar-${kind}`, "aria-hidden": "true" }),
          label,
          el("strong", {}, [size(bytes)]),
        ]),
      ),
    );
    $("storage-disk").hidden = !data.disk_total;
  }

  // What the cache holds, each part as a bar against the biggest one.
  function renderCache(data) {
    const subtitles = data.disk.subtitles;
    const imdb = data.disk.imdb;
    const parts = [
      [t("library_subtitles"), subtitles],
      [t("library_imdb"), imdb],
      [t("library_whisper"), data.disk.whisper],
      [
        t("library_cache_other"),
        Math.max(0, data.disk.cache - subtitles - imdb),
      ],
    ].filter(([, bytes]) => bytes > 0);
    const biggest = Math.max(1, ...parts.map(([, bytes]) => bytes));
    $("storage-cache-total").textContent = size(
      data.disk.cache + data.disk.whisper,
    );
    $("library-disk").replaceChildren(
      ...parts.map(([label, bytes]) =>
        el("li", {}, [
          el("span", {}, [label]),
          el("span", { className: "storage-part-bar", "aria-hidden": "true" }, [
            el("i", { style: `width:${share(bytes, biggest)}` }),
          ]),
          el("strong", {}, [size(bytes)]),
        ]),
      ),
    );
    $("storage-cache").hidden = !parts.length;
  }

  function renderTorrents(data) {
    const torrents = data.torrents;
    const total = torrents.reduce((sum, item) => sum + (item.size || 0), 0);
    $("storage-torrents-total").textContent = torrents.length
      ? t("library_torrents_summary", { n: torrents.length, size: size(total) })
      : "";
    $("storage-torrents-empty").hidden =
      Boolean(torrents.length) || Boolean(data.torrserver_error);
    $("library-remove-all").hidden = !torrents.length;
    $("library-remove-all").dataset.count = torrents.length;
    const more = $("storage-show-all");
    more.hidden = showAll || torrents.length <= SHOWN;
    more.textContent = t("library_show_all", { n: torrents.length });
    $("library-list").replaceChildren(
      ...(showAll ? torrents : torrents.slice(0, SHOWN)).map((item) => {
        const remove = el(
          "button",
          {
            className: "storage-remove",
            type: "button",
            "aria-label": `${t("library_remove")}: ${item.title || item.hash}`,
            title: t("library_remove"),
          },
          [SF.icon("close")],
        );
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          await api("/api/library/remove", { hash: item.hash }).catch(
            () => null,
          );
          load();
        });
        return el("li", { className: item.active ? "is-playing" : null }, [
          item.poster
            ? el("img", { src: item.poster, alt: "", loading: "lazy" })
            : el("span", { className: "library-noposter" }),
          item.active
            ? el("span", { className: "storage-playing" }, [
                t("library_playing"),
              ])
            : null,
          el("strong", { title: item.title }, [item.title || item.hash]),
          el("small", {}, [
            [size(item.size), date(item.added)].filter(Boolean).join(" · "),
          ]),
          remove,
        ]);
      }),
    );
  }

  function render(data) {
    last = data;
    summary.textContent = data.torrserver_error
      ? t("library_offline", { error: data.torrserver_error })
      : "";
    summary.hidden = !data.torrserver_error;
    renderDisk(data);
    renderCache(data);
    renderTorrents(data);
    $("panel-storage").dataset.loaded = "1"; // (the browser tests wait for it)
  }

  async function load() {
    try {
      render(await api("/api/library"));
    } catch (error) {
      summary.hidden = false;
      summary.textContent = error.data ? t("library_login") : error.message;
    }
  }

  $("storage-show-all").addEventListener("click", () => {
    showAll = true;
    if (last) renderTorrents(last);
  });
  $("library-remove-all").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!confirm(t("library_confirm_all", { n: button.dataset.count }))) return;
    button.disabled = true;
    await api("/api/library/remove", { all: true }).catch(() => null);
    button.disabled = false;
    load();
  });
  $("library-clear-subs").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const data = await api("/api/library/clear-subtitles", {});
      button.textContent = t("library_freed", { size: size(data.freed) });
    } catch {
      button.disabled = false;
    }
    load();
  });
  load();

  // Backup: one JSON file to download; restoring sends it back to be merged.
  const download = $("backup-download");
  const message = $("backup-message");
  $("backup-keys").addEventListener("change", (event) => {
    download.href = `/api/backup${event.currentTarget.checked ? "?keys=1" : ""}`;
  });
  $("backup-restore").addEventListener("click", () => $("backup-file").click());
  $("backup-file").addEventListener("change", async (event) => {
    const input = event.currentTarget;
    const [file] = input.files;
    input.value = "";
    if (!file || !confirm(t("backup_confirm", { name: file.name }))) return;
    let backup;
    try {
      backup = JSON.parse(await file.text());
    } catch {
      message.textContent = t("backup_invalid");
      return;
    }
    try {
      const data = await api("/api/backup/restore", backup);
      message.textContent = t("backup_restored", {
        profiles: data.profiles,
        titles: data.titles,
        settings: data.settings.length,
      });
    } catch (error) {
      message.textContent = error.message;
    }
  });
})();
