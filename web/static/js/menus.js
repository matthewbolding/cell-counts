// Menu bar behaviour for server-rendered pages: menus are <details> elements so
// they work without JS; this only adds the niceties of a desktop menu bar — one menu open at a
// time, hover-to-switch while a menu is open, and click-outside/Escape to close.
(() => {
  const menus = () => Array.from(document.querySelectorAll("details.menu"));

  document.addEventListener("click", (event) => {
    for (const menu of menus()) {
      if (!menu.contains(event.target)) menu.open = false;
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") for (const menu of menus()) menu.open = false;
  });

  for (const menu of menus()) {
    const summary = menu.querySelector("summary");
    // Pointing at another menu's title while one is open switches to it; the
    // click that usually follows must not toggle that menu closed again.
    let switchedByHover = false;
    menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      for (const other of menus()) if (other !== menu) other.open = false;
    });
    summary.addEventListener("mouseenter", () => {
      if (menus().some((m) => m.open && m !== menu)) {
        menu.open = true;
        switchedByHover = true;
      }
    });
    summary.addEventListener("mouseleave", () => { switchedByHover = false; });
    summary.addEventListener("click", (event) => {
      if (switchedByHover) event.preventDefault();
      switchedByHover = false;
    });
  }

  // Buttons that need a confirmation carry data-confirm; the form submits only if
  // the user agrees. (No inline handlers: the CSP forbids them.)
  document.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    const message = submitter?.dataset.confirm || event.target.dataset.confirm;
    if (message && !window.confirm(message)) event.preventDefault();
  });
})();
