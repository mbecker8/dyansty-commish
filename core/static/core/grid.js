// Sortable, filterable tables: any <table class="grid">. Click a header to sort (again to reverse).
// The filter row has a box per column: text matches anywhere in the cell; numbers also take
// comparisons like ">=2028" or "<5". Works on the rendered cells, so the page is fine without it.
(function () {
  "use strict";

  const COMPARE = /^(>=|<=|>|<|=)\s*(-?\d+(?:\.\d+)?)$/;

  function cellText(cell) {
    return cell ? cell.textContent.trim() : "";
  }

  // "$12", "+5", "-3", "2027" → number; anything else (names, "—") → null.
  function asNumber(text) {
    const cleaned = text.replace(/[$,+\s]/g, "");
    return cleaned !== "" && !isNaN(cleaned) ? Number(cleaned) : null;
  }

  function matches(text, filter) {
    if (!filter) return true;
    const op = filter.match(COMPARE);
    if (op) {
      const value = asNumber(text);
      if (value === null) return false;
      const target = Number(op[2]);
      return { ">=": value >= target, "<=": value <= target, ">": value > target, "<": value < target, "=": value === target }[op[1]];
    }
    return text.toLowerCase().includes(filter.toLowerCase());
  }

  function compare(a, b) {
    const x = asNumber(a), y = asNumber(b);
    if (x !== null && y !== null) return x - y;
    if (x !== null) return -1;
    if (y !== null) return 1;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }

  function setup(table) {
    const head = table.tHead && table.tHead.rows[0];
    const body = table.tBodies[0];
    if (!head || !body) return;
    const headers = Array.from(head.cells);
    // Skip the "None." placeholder row.
    const rows = Array.from(body.rows).filter((r) => !r.querySelector("td[colspan]"));
    if (!rows.length) return;
    rows.forEach((r, i) => (r.dataset.order = i));

    const count = document.createElement("p");
    count.className = "grid-count muted";
    table.closest(".table-wrap").before(count);

    const filterRow = document.createElement("tr");
    filterRow.className = "filters";
    const inputs = headers.map((th) => {
      const cell = document.createElement("th");
      cell.className = th.className;
      const input = document.createElement("input");
      input.type = "search";
      input.placeholder = "Filter";
      input.setAttribute("aria-label", "Filter " + (th.textContent.trim() || "column"));
      input.addEventListener("input", applyFilters);
      cell.appendChild(input);
      filterRow.appendChild(cell);
      return input;
    });
    table.tHead.appendChild(filterRow);

    function applyFilters() {
      let shown = 0;
      rows.forEach((row) => {
        const ok = inputs.every((input, i) => matches(cellText(row.cells[i]), input.value.trim()));
        row.hidden = !ok;
        if (ok) shown++;
      });
      count.textContent = shown === rows.length ? rows.length + " rows" : shown + " of " + rows.length + " rows";
    }

    let sorted = { column: null, direction: 1 };
    headers.forEach((th, column) => {
      const label = th.textContent.trim();
      if (!label) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sort";
      button.textContent = label;
      th.textContent = "";
      th.appendChild(button);
      button.addEventListener("click", () => {
        const direction = sorted.column === column ? -sorted.direction : 1;
        sorted = { column, direction };
        headers.forEach((h) => h.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", direction === 1 ? "ascending" : "descending");
        rows
          .slice()
          .sort((a, b) => direction * compare(cellText(a.cells[column]), cellText(b.cells[column])) || a.dataset.order - b.dataset.order)
          .forEach((row) => body.appendChild(row));
      });
    });

    applyFilters();
  }

  document.addEventListener("DOMContentLoaded", () => document.querySelectorAll("table.grid").forEach(setup));
})();
