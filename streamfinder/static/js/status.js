// Service status: every check from /api/health (streamfinder/health.py), grouped, with
// how long each answer took. Checked again on the button and every minute while open.
(() => {
  "use strict";
  const { el, api, t } = SF;
  const root = document.getElementById("health");
  const summary = document.getElementById("health-summary");
  const refresh = document.getElementById("health-refresh");
  if (!root) return;

  // Tabs (the admin's page): Services, Storage, Backup; the address remembers the tab
  // (#storage), so a reload or a shared link opens it again.
  const tabs = [...document.querySelectorAll(".status-tabs [role=tab]")];
  function showTab(tab, focus = false) {
    for (const other of tabs) {
      const on = other === tab;
      other.setAttribute("aria-selected", String(on));
      other.tabIndex = on ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !on;
    }
    if (focus) tab.focus();
    const name = tab.id.replace("tab-", "");
    history.replaceState(history.state, "", name === "services" ? location.pathname : `#${name}`);
  }
  for (const tab of tabs) {
    tab.addEventListener("click", () => showTab(tab));
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      showTab(tabs[(tabs.indexOf(tab) + step + tabs.length) % tabs.length], true);
    });
  }
  const initial = tabs.find((tab) => `#${tab.id.replace("tab-", "")}` === location.hash);
  if (initial) showTab(initial);

  const row = (check) =>
    el("li", { "data-state": check.state, "data-check": check.id }, [
      el("span", { className: "dot", "aria-hidden": "true" }),
      el("div", {}, [
        el("strong", {}, [check.name]),
        el("p", {}, [
          check.state === "off"
            ? `${t("health_off")} · ${check.detail}`
            : check.detail,
        ]),
      ]),
      check.ms !== null && check.state !== "off"
        ? el("span", { className: "status-ms" }, [`${check.ms} ms`])
        : null,
    ]);

  function render(data) {
    const groups = new Map();
    for (const check of data.checks) {
      if (!groups.has(check.group)) groups.set(check.group, []);
      groups.get(check.group).push(check);
    }
    root.replaceChildren(
      ...[...groups].map(([group, checks]) =>
        el("section", { className: "health-group" }, [
          el("h2", {}, [data.groups[group] || group]),
          el("ul", { className: "status-list" }, checks.map(row)),
        ]),
      ),
    );
    const { ok, warn, fail } = data.summary;
    const time = new Date(data.checked * 1000).toLocaleTimeString(SF.lang, {
      hour: "2-digit",
      minute: "2-digit",
    });
    const state =
      warn || fail
        ? t("health_summary", { ok, warn, fail })
        : t("health_all_ok");
    summary.textContent = `${state} · ${t("health_checked", { time })}`;
    root.setAttribute("aria-busy", "false");
  }

  async function load(fresh = false) {
    refresh.disabled = true;
    root.setAttribute("aria-busy", "true");
    if (fresh) summary.textContent = t("health_checking");
    try {
      render(await api(`/api/health${fresh ? "?fresh=1" : ""}`));
    } catch (error) {
      summary.textContent = error.message;
    } finally {
      refresh.disabled = false;
    }
  }

  refresh.addEventListener("click", () => load(true));
  setInterval(() => document.visibilityState === "visible" && load(), 300000);
  load();
})();
