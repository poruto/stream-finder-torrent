// New-episode notifications in Server settings (admin): ntfy topic or Telegram bot,
// saved through /api/notify, with a test message.
(() => {
  "use strict";
  const { api, t } = SF;
  const form = document.getElementById("notify-form");
  if (!form) return; // not logged in as admin
  const message = document.getElementById("notify-message");
  const state = document.getElementById("notify-state");
  const remove = document.getElementById("notify-remove");

  const show = (data) => {
    const on = Boolean(data.ntfy || data.telegram);
    state.textContent = on ? t("notify_on") : "";
    remove.hidden = !on;
    if (data.ntfy) form.ntfy_url.value = data.ntfy;
    if (data.checked)
      form.title = t("notify_last", {
        time: new Date(data.checked * 1000).toLocaleString(SF.lang),
      });
  };
  const send = async (body, done) => {
    message.textContent = "";
    try {
      show(await api("/api/notify", body));
      message.textContent = done;
      form.telegram_token.value = "";
    } catch (error) {
      message.textContent = error.message;
    }
  };
  const fields = () => ({
    ntfy_url: form.ntfy_url.value.trim(),
    telegram_token: form.telegram_token.value.trim(),
    telegram_chat: form.telegram_chat.value.trim(),
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    send(fields(), t("notify_saved"));
  });
  document
    .getElementById("notify-test")
    .addEventListener("click", () =>
      send({ ...fields(), test: true }, t("notify_test_sent")),
    );
  remove.addEventListener("click", () => {
    form.ntfy_url.value = "";
    send({ remove: ["ntfy", "telegram"] }, "");
  });
  api("/api/notify")
    .then(show)
    .catch(() => {});
})();
