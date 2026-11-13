"use strict";

const monthSelect = document.querySelector("#month-select");
monthSelect?.addEventListener("change", () => monthSelect.form.requestSubmit());

const records = Array.from(document.querySelectorAll(".record"));
const search = document.querySelector("#ledger-search");
const filterButtons = document.querySelectorAll(".filter");
const pageSize = 10;
let page = 0;
let activeFilter = "all";

function updateList() {
  const query = search.value.trim().toLocaleLowerCase();
  const matching = records.filter((record) =>
    (activeFilter === "all" || record.dataset[activeFilter] === "1") &&
    record.dataset.search.toLocaleLowerCase().includes(query)
  );
  const pages = Math.max(1, Math.ceil(matching.length / pageSize));
  page = Math.max(0, Math.min(page, pages - 1));
  const start = page * pageSize;
  const visible = new Set(matching.slice(start, start + pageSize));
  for (const record of records) record.hidden = !visible.has(record);
  const end = Math.min(start + pageSize, matching.length);
  document.querySelector("#result-range").textContent = matching.length
    ? matching.length + "건 중 " + (start + 1) + "–" + end + "건"
    : "0건";
  document.querySelector("#page-position").textContent = (page + 1) + " / " + pages;
  document.querySelector("#previous").disabled = page === 0;
  document.querySelector("#next").disabled = page >= pages - 1;
  document.querySelector("#empty-results").hidden = matching.length > 0;
}

if (search) {
  for (const button of filterButtons) {
    button.addEventListener("click", () => {
      activeFilter = button.dataset.filter;
      for (const filter of filterButtons) filter.setAttribute("aria-pressed", String(filter === button));
      page = 0;
      updateList();
    });
  }
  search.addEventListener("input", () => {
    page = 0;
    updateList();
  });
  document.querySelector("#previous").addEventListener("click", () => {
    page--;
    updateList();
  });
  document.querySelector("#next").addEventListener("click", () => {
    page++;
    updateList();
  });
  for (const button of document.querySelectorAll(".row-toggle")) {
    button.addEventListener("click", () => {
      const detail = document.getElementById(button.getAttribute("aria-controls"));
      detail.hidden = !detail.hidden;
      button.setAttribute("aria-expanded", String(!detail.hidden));
    });
  }
  updateList();
  document.documentElement.classList.add("enhanced");
}
