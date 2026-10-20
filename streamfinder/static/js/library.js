// Storage on the Status page (admin): TorrServer's torrents and the app's cache, with
// clean-up buttons, and the backup. Without the admin login it only says how to get there.
(() => {
  "use strict";
  const { el, api, t } = SF;
  const $ = (id) => document.getElementById(id);
  const summary = $("library-summary");

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
  const date = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleDateString(SF.lang) : "");

  async function load() {
    let data;
    try {
      data = await api("/api/library");
    } catch (error) {
      summary.textContent = error.message.includes("401") || error.data ? t("library_login") : error.message;
      return;
    }
    summary.textContent = data.torrserver_error
      ? t("library_offline", { error: data.torrserver_error })
      : t("library_summary", { n: data.torrents.length, free: size(data.free) });
    const disk = [
      ["library_cache", data.disk.cache],
      ["library_subtitles", data.disk.subtitles],
      ["library_imdb", data.disk.imdb],
      ["library_whisper", data.disk.whisper],
    ].filter(([, bytes]) => bytes > 0);
    $("library-disk").replaceChildren(...disk.map(([key, bytes]) => el("li", {}, [el("strong", {}, [size(bytes)]), t(key)])));
    $("library-disk").hidden = !disk.length;
    $("library-actions").hidden = false;
    $("library-remove-all").disabled = !data.torrents.length;
    $("library-remove-all").dataset.count = data.torrents.length;
    $("library-list").replaceChildren(
      ...data.torrents.map((item) => {
        const remove = el("button", { className: "btn btn-ghost", type: "button" }, [t("library_remove")]);
        remove.addEventListener("click", async () => {
          remove.disabled = true;
          await api("/api/library/remove", { hash: item.hash }).catch(() => null);
          load();
        });
        return el("li", {}, [
          item.poster ? el("img", { src: item.poster, alt: "", loading: "lazy" }) : el("span", { className: "library-noposter" }),
          el("div", {}, [
            el("strong", { title: item.title }, [item.title || item.hash]),
            el("small", {}, [[size(item.size), date(item.added), item.active ? t("library_playing") : ""].filter(Boolean).join(" · ")]),
          ]),
          remove,
        ]);
      }),
    );
  }

  $("library-remove-all").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    if (!confirm(t("library_confirm_all", { n: button.dataset.count }))) return;
    button.disabled = true;
    await api("/api/library/remove", { all: true }).catch(() => null);
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
      message.textContent = t("backup_restored", { profiles: data.profiles, titles: data.titles, settings: data.settings.length });
    } catch (error) {
      message.textContent = error.message;
    }
  });
})();
