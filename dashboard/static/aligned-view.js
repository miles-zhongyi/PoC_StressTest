/**
 * Side-by-side JSON lines aligned row-by-row.
 */
function jsonLines(obj) {
  if (obj == null) return [""];
  return JSON.stringify(obj, null, 2).split("\n");
}

function escHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

/**
 * @param {HTMLElement} container
 * @param {Array<{title: string, obj?: object|null, lines?: string[], empty?: string}>} columns
 * @param {{ maxHeight?: string }} opts
 */
function renderAlignedColumns(container, columns, opts = {}) {
  const maxHeight = opts.maxHeight || "70vh";
  const lineSets = columns.map((c) => {
    if (c.lines) return c.lines.slice();
    if (c.obj != null) return jsonLines(c.obj);
    return [c.empty || ""];
  });
  const n = Math.max(1, ...lineSets.map((ls) => ls.length));
  lineSets.forEach((ls) => { while (ls.length < n) ls.push(""); });

  const cols = columns.length;
  const gridCols = `2.25rem ${columns.map(() => "1fr").join(" ")}`;

  let head = `<div class="align-head" style="grid-template-columns:${gridCols}">`
    + `<div class="align-ln"></div>`;
  columns.forEach((c) => {
    head += `<div class="align-h">${escHtml(c.title)}</div>`;
  });
  head += `</div>`;

  let rows = "";
  for (let i = 0; i < n; i++) {
    const cells = lineSets.map((ls) => ls[i] ?? "");
    let diff = false;
    if (cols === 3) {
      const ref = (cells[1] ?? "").trim();
      diff = cells.some((c, j) => j !== 1 && (c ?? "").trim() !== ref);
    } else if (cols >= 2) {
      diff = (cells[0] ?? "").trim() !== (cells[1] ?? "").trim();
    }
    rows += `<div class="align-row${diff ? " diff" : ""}" style="grid-template-columns:${gridCols}">`
      + `<div class="align-ln">${i + 1}</div>`;
    cells.forEach((text) => {
      rows += `<div class="align-cell">${escHtml(text)}</div>`;
    });
    rows += `</div>`;
  }

  container.innerHTML =
    `<div class="align-view" style="max-height:${escHtml(maxHeight)}">`
    + head
    + `<div class="align-scroll">${rows}</div>`
    + `</div>`;
}

/** Two-column shorthand: trace record vs template. */
function renderAlignedPair(container, leftObj, rightObj, opts = {}) {
  renderAlignedColumns(container, [
    { title: opts.leftTitle || "22_decoded sample", obj: leftObj },
    { title: opts.rightTitle || "Twin template", obj: rightObj },
  ], opts);
}

function renderAlignedPlaceholder(container, text) {
  container.innerHTML = `<div class="align-placeholder">${escHtml(text)}</div>`;
}
