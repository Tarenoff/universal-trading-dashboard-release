(() => {
  const button = document.querySelector("#global-settings-button");
  const menu = document.querySelector("#global-settings-menu");
  if (!button || !menu) return;

  function closeMenu() {
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
  }

  button.addEventListener("click", () => {
    menu.hidden = !menu.hidden;
    button.setAttribute("aria-expanded", String(!menu.hidden));
  });
  document.addEventListener("click", (event) => {
    if (!button.contains(event.target) && !menu.contains(event.target)) closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !menu.hidden) {
      closeMenu();
      button.focus();
    }
  });
})();
