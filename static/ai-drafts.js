document.querySelectorAll("[data-generates-draft]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    const panel = form.closest(".draft-panel");
    if (panel.getAttribute("aria-busy") === "true") {
      event.preventDefault();
      return;
    }
    panel.setAttribute("aria-busy", "true");
    panel.querySelector(".draft-progress").hidden = false;
    panel.querySelectorAll("[data-generates-draft] button").forEach((button) => {
      button.disabled = true;
    });
  });
});

window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  document.querySelectorAll('.draft-panel[aria-busy="true"]').forEach((panel) => {
    panel.removeAttribute("aria-busy");
    panel.querySelector(".draft-progress").hidden = true;
    panel.querySelectorAll("[data-generates-draft] button").forEach((button) => {
      button.disabled = false;
    });
  });
});
