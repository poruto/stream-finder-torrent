// Profiles ("Who's watching?"): each person of the household has their own Continue
// watching and My list (stored per profile, see app.js and streamfinder/shared.py).
// The choice is remembered per device; with a single profile nothing of this shows.
(() => {
  "use strict";
  const { el, api, t } = SF;
  const button = document.getElementById("profile-btn");
  if (!button) return;
  const COLORS = [
    "#e5383b",
    "#f77f00",
    "#fcbf49",
    "#2a9d8f",
    "#3a86ff",
    "#8338ec",
    "#ff006e",
    "#06d6a0",
    "#118ab2",
    "#8d99ae",
    "#bc6c25",
    "#6a994e",
  ];
  const color = (p) => COLORS[(p.color || 0) % COLORS.length];
  const initial = (p) => (p.name || "?").trim().charAt(0).toUpperCase() || "?";
  const remember = (key, value) => {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      /* blocked storage: the choice lasts for this page only */
    }
  };
  const chosenBefore = () => {
    try {
      return localStorage.getItem("streamfinder.profile.chosen") === "1";
    } catch {
      return true;
    }
  };
  let profiles = [];
  let sheet = null;

  function switchTo(id) {
    remember("streamfinder.profile", id || null);
    remember("streamfinder.profile.chosen", "1");
    if (id === SF.profile) return close();
    location.reload(); // every store and row starts again with that profile's data
  }

  function avatar(p, className = "profile-avatar") {
    return el(
      "span",
      { className, style: `--profile:${color(p)}`, "aria-hidden": "true" },
      [initial(p)],
    );
  }

  function close() {
    sheet?.remove();
    sheet = null;
    button.focus({ preventScroll: true });
  }

  // The picker; `editing`: rename / recolour / remove instead of switching.
  function open({ editing = false, adding = false } = {}) {
    sheet?.remove();
    const panel = el("div", {
      className: "profile-panel",
      role: "dialog",
      "aria-modal": "true",
      "aria-label": t("profile_who"),
    });
    sheet = el("div", { className: "profile-sheet" }, [panel]);
    sheet.addEventListener(
      "click",
      (event) => event.target === sheet && close(),
    );
    sheet.addEventListener(
      "keydown",
      (event) => event.key === "Escape" && close(),
    );
    const grid = el(
      "div",
      { className: "profile-grid" },
      profiles.map((p) => {
        const choice = el(
          "button",
          {
            className: "profile-choice",
            type: "button",
            "aria-current": String(p.id === SF.profile),
          },
          [avatar(p), el("span", {}, [p.name])],
        );
        choice.addEventListener("click", () =>
          editing ? open({ editing: true, adding: p }) : switchTo(p.id),
        );
        return choice;
      }),
    );
    panel.append(
      el("h2", {}, [t(editing ? "profile_edit" : "profile_who")]),
      grid,
    );
    if (adding) panel.append(form(adding === true ? null : adding));
    const actions = el("div", { className: "profile-actions" });
    if (!adding) {
      if (!editing) {
        const add = el(
          "button",
          { className: "btn btn-ghost", type: "button" },
          [t("profile_add")],
        );
        add.addEventListener("click", () => open({ adding: true }));
        actions.append(add);
      }
      const edit = el(
        "button",
        { className: "btn btn-ghost", type: "button" },
        [t(editing ? "profile_done" : "profile_edit")],
      );
      edit.addEventListener("click", () => open({ editing: !editing }));
      actions.append(edit);
      panel.append(actions);
    }
    document.body.append(sheet);
    (
      panel.querySelector("input") ||
      panel.querySelector("[aria-current='true']") ||
      panel.querySelector("button")
    )?.focus();
  }

  // Add a profile (`existing` null) or edit one.
  function form(existing) {
    let picked = existing
      ? existing.color || 0
      : profiles.length % COLORS.length;
    const name = el("input", {
      type: "text",
      maxlength: "30",
      required: true,
      value: existing?.name || "",
      "aria-label": t("profile_name"),
      placeholder: t("profile_name"),
    });
    const swatches = el(
      "div",
      { className: "profile-colors", role: "group" },
      COLORS.map((c, i) => {
        const swatch = el("button", {
          type: "button",
          style: `--profile:${c}`,
          "aria-pressed": String(i === picked),
          "aria-label": c,
        });
        swatch.addEventListener("click", () => {
          picked = i;
          for (const other of swatches.children)
            other.setAttribute("aria-pressed", String(other === swatch));
        });
        return swatch;
      }),
    );
    const message = el("p", {
      className: "key-message",
      "aria-live": "polite",
    });
    const save = el("button", { className: "btn btn-play", type: "submit" }, [
      t("profile_save"),
    ]);
    const cancel = el(
      "button",
      { className: "btn btn-ghost", type: "button" },
      [t("cancel")],
    );
    cancel.addEventListener("click", () =>
      open({ editing: Boolean(existing) }),
    );
    const buttons = el("div", { className: "profile-actions" }, [save, cancel]);
    if (existing && existing.id) {
      const remove = el("button", { className: "key-remove", type: "button" }, [
        t("profile_remove"),
      ]);
      remove.addEventListener("click", async () => {
        if (!confirm(t("profile_confirm_remove", { name: existing.name })))
          return;
        profiles = (
          await api("/api/profiles", { action: "remove", id: existing.id })
        ).profiles;
        if (existing.id === SF.profile) return switchTo("");
        open({ editing: true });
      });
      buttons.append(remove);
    }
    const node = el("form", { className: "profile-form" }, [
      name,
      swatches,
      buttons,
      message,
    ]);
    node.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const reply = await api(
          "/api/profiles",
          existing
            ? {
                action: "edit",
                id: existing.id,
                name: name.value,
                color: picked,
              }
            : { action: "add", name: name.value, color: picked },
        );
        profiles = reply.profiles;
        refreshButton();
        if (existing) open({ editing: true });
        else switchTo(reply.id);
      } catch (error) {
        message.textContent = error.message;
      }
    });
    return node;
  }

  function refreshButton() {
    const current = profiles.find((p) => p.id === SF.profile) || profiles[0];
    button.replaceChildren(initial(current));
    button.style.setProperty("--profile", color(current));
    button.title = `${current.name} · ${t("profile_switch")}`;
    button.hidden = false;
  }

  button.addEventListener("click", () => open());
  api("/api/profiles")
    .then((data) => {
      profiles = data.profiles;
      refreshButton();
      // A household with several people: ask once on a new device.
      if (
        profiles.length > 1 &&
        !chosenBefore() &&
        document.getElementById("main")
      )
        open();
    })
    .catch(() => {});
})();
