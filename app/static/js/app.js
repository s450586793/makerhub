document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    const tab = button.getAttribute("data-tab");
    document.querySelectorAll("[data-tab]").forEach((item) => item.classList.remove("is-active"));
    document.querySelectorAll("[data-panel]").forEach((panel) => panel.classList.remove("is-active"));
    button.classList.add("is-active");
    const panel = document.querySelector(`[data-panel="${tab}"]`);
    if (panel) panel.classList.add("is-active");
  });
});
