// A windowed data grid over the whole dataset.
//
// Only the visible rows exist in the DOM; rows are fetched from the server in
// aligned blocks and cached. Sorting and filtering are server-side, so they
// apply to every row in the file. In the original, the grid held at most 1,000
// rows and "sort" reordered that slice in JavaScript while the UI advertised
// support for more than ten million.

import { el, icon, $, num, count, kindLabel, debounce } from './ui.js';

const ROW_H = 28;
const BLOCK = 500;      // rows fetched per request
const OVERSCAN = 12;    // rows rendered above and below the viewport
const MIN_W = 64;

/**
 * Browsers cap how tall an element may be — around 33.5M px in Chrome and about
 * half that in Firefox. At 28px a row that is only ~800,000 rows, so a scroller
 * sized at `rows × ROW_H` silently becomes unreachable past that point: dragging
 * the bar to the bottom of a 10M-row file would stop around row 800,000.
 *
 * Past this threshold the scrollbar stops representing pixels and starts
 * representing a position in the file: the scroll offset is mapped onto the row
 * range proportionally, and the rendered window is parked at the viewport. One
 * wheel notch then covers more than one row, which is the same bargain every
 * large grid makes.
 */
const MAX_SCROLL_PX = 4_000_000;

export class Grid {
  /**
   * @param {HTMLElement} host    scrolling container
   * @param {object} opts
   *   fetchRows(offset, limit) -> {rows: any[][]}
   *   onHeaderMenu(column, anchorEl)
   *   onSelect({row, col, value, column})
   */
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = opts;
    this.sizer = host.querySelector('.grid-sizer');
    this.table = host.querySelector('table.grid');

    this.columns = [];
    this.widths = new Map();
    this.total = 0;
    this.cache = new Map();     // absolute row index -> row array
    this.inflight = new Set();  // block indices being fetched
    this.sort = [];
    this.filteredColumns = new Set();
    this.profiles = new Map();
    this.selected = null;
    this.generation = 0;        // bumped on every data change to void stale fetches

    this.onScroll = this.onScroll.bind(this);
    host.addEventListener('scroll', this.onScroll, { passive: true });
    host.addEventListener('keydown', (e) => this.onKey(e));

    this.resizeObserver = new ResizeObserver(() => this.render());
    this.resizeObserver.observe(host);
  }

  destroy() {
    this.host.removeEventListener('scroll', this.onScroll);
    this.resizeObserver.disconnect();
    if (this.frame) cancelAnimationFrame(this.frame);
    clearTimeout(this.frameTimer);
  }

  // ------------------------------------------------------------ public API

  setColumns(columns) {
    this.columns = columns;
    for (const column of columns) {
      if (!this.widths.has(column.name)) {
        this.widths.set(column.name, defaultWidth(column));
      }
    }
    this.buildHead();
  }

  setTotal(total) {
    this.total = total || 0;
    const natural = this.total * ROW_H + ROW_H + 2;
    this.compressed = natural > MAX_SCROLL_PX;
    this.sizer.style.height = Math.min(natural, MAX_SCROLL_PX) + 'px';
  }

  /** Rows visible at once, excluding the sticky header. */
  get pageRows() {
    return Math.max(1, Math.ceil((this.host.clientHeight - ROW_H) / ROW_H));
  }

  /** Scroll so `index` is the first data row shown. */
  scrollToRow(index) {
    const row = Math.max(0, Math.min(index, Math.max(0, this.total - 1)));
    if (!this.compressed) {
      this.host.scrollTop = row * ROW_H;
      return;
    }
    const usable = this.sizer.offsetHeight - this.host.clientHeight;
    const span = Math.max(1, this.total - this.pageRows);
    this.host.scrollTop = Math.round((row / span) * usable);
  }

  /** Discard cached rows — call whenever filters, sort or the data change. */
  invalidate() {
    this.generation++;
    this.cache.clear();
    this.inflight.clear();
    this.render();
  }

  setSort(sort) {
    this.sort = sort || [];
    this.buildHead();
  }

  setFilteredColumns(names) {
    this.filteredColumns = new Set(names || []);
    this.buildHead();
  }

  setProfile(columnName, profile) {
    this.profiles.set(columnName, profile);
    this.buildHead();
  }

  scrollToTop() {
    this.host.scrollTop = 0;
  }

  // --------------------------------------------------------------- header

  buildHead() {
    if (!this.columns.length) return;
    const head = el('thead');
    const tr = el('tr');

    tr.append(el('th', {
      class: 'rownum',
      style: { width: rowNumWidth(this.total) + 'px', minWidth: rowNumWidth(this.total) + 'px' },
    }, el('div', { class: 'th-inner', style: { justifyContent: 'flex-end', cursor: 'default' } }, '#')));

    for (const column of this.columns) {
      const width = this.widths.get(column.name);
      const sortRule = this.sort.find((s) => s.column === column.name);
      const th = el('th', {
        style: { width: width + 'px', minWidth: width + 'px' },
        dataset: { column: column.name, filtered: this.filteredColumns.has(column.name) ? 'true' : 'false' },
        title: `${column.name} — ${column.type}`,
      });

      const menuBtn = el('button', {
        class: 'th-menu', 'aria-label': `Options for ${column.name}`,
        onclick: (e) => { e.stopPropagation(); this.opts.onHeaderMenu?.(column, menuBtn); },
      }, icon('more', 14));

      const inner = el('div', {
        class: 'th-inner',
        onclick: () => this.opts.onSort?.(column),
      },
        el('span', { class: 'th-kind' }, kindLabel(column.kind)),
        el('span', { class: 'th-name' }, column.name),
        sortRule ? icon(sortRule.direction === 'desc' ? 'down' : 'up', 13) : null,
        menuBtn,
      );
      if (sortRule) inner.querySelector('svg:last-of-type')?.classList.add('th-sort');

      th.append(inner, this.sparkline(column));
      th.append(this.resizeHandle(column, th));
      tr.append(th);
    }

    head.append(tr);
    this.table.querySelector('thead')?.remove();
    this.table.prepend(head);
    this.table.style.width = this.totalWidth() + 'px';
    this.sizer.style.width = this.totalWidth() + 'px';
  }

  /** A 14px distribution strip: histogram for numbers, top-value bars for text. */
  sparkline(column) {
    const profile = this.profiles.get(column.name);
    const strip = el('div', { class: 'th-spark' + (column.kind === 'number' ? '' : ' is-text') });
    if (!profile) return strip;

    let values = [];
    if (profile.histogram?.length) values = profile.histogram.map((b) => b.count);
    else if (profile.top?.length) values = profile.top.map((t) => t.count);
    if (!values.length) return strip;

    const max = Math.max(...values, 1);
    for (const value of values.slice(0, 24)) {
      strip.append(el('i', { style: { height: Math.max(1, Math.round((value / max) * 12)) + 'px' } }));
    }
    const pctNull = profile.rows ? (profile.rows - profile.count) / profile.rows : 0;
    strip.title = profile.kind === 'number'
      ? `min ${num(profile.min)} · max ${num(profile.max)} · ${Math.round(pctNull * 100)}% empty`
      : `${count(profile.distinct ?? 0)} distinct · ${Math.round(pctNull * 100)}% empty`;
    return strip;
  }

  resizeHandle(column, th) {
    const handle = el('div', {
      style: {
        position: 'absolute', top: 0, right: 0, width: '5px', height: '100%',
        cursor: 'col-resize', userSelect: 'none', zIndex: 5,
      },
      onmousedown: (event) => {
        event.preventDefault();
        event.stopPropagation();
        const startX = event.clientX;
        const startW = this.widths.get(column.name);
        const move = (e) => {
          const next = Math.max(MIN_W, startW + (e.clientX - startX));
          this.widths.set(column.name, next);
          th.style.width = th.style.minWidth = next + 'px';
          this.table.style.width = this.sizer.style.width = this.totalWidth() + 'px';
          this.applyWidths();
        };
        const up = () => {
          document.removeEventListener('mousemove', move);
          document.removeEventListener('mouseup', up);
        };
        document.addEventListener('mousemove', move);
        document.addEventListener('mouseup', up);
      },
      ondblclick: (event) => {
        event.stopPropagation();
        this.widths.set(column.name, defaultWidth(column));
        this.buildHead();
        this.render();
      },
    });
    th.style.position = 'relative';
    return handle;
  }

  applyWidths() {
    const body = this.table.querySelector('tbody');
    if (!body) return;
    const first = body.querySelector('tr');
    if (!first) return;
    [...first.children].forEach((cell, index) => {
      if (index === 0) return;
      const column = this.columns[index - 1];
      if (column) cell.style.width = cell.style.minWidth = this.widths.get(column.name) + 'px';
    });
  }

  totalWidth() {
    let total = rowNumWidth(this.total);
    for (const column of this.columns) total += this.widths.get(column.name) || 150;
    return total;
  }

  // ----------------------------------------------------------------- body

  onScroll() {
    // Coalesce bursts of scroll events into one render per frame.
    //
    // The timer is not belt-and-braces: requestAnimationFrame does not run at
    // all while the page is not being painted — a background tab, a window
    // behind another window — so a flag cleared only inside the callback stays
    // set for ever and the grid stops redrawing until reload. The timeout
    // guarantees the pending render is eventually flushed either way.
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => this.flushRender());
    this.frameTimer = setTimeout(() => this.flushRender(), 100);
  }

  flushRender() {
    if (this.frame) {
      cancelAnimationFrame(this.frame);
      this.frame = 0;
    }
    clearTimeout(this.frameTimer);
    this.frameTimer = 0;
    this.render();
  }

  visibleRange() {
    const top = this.host.scrollTop;
    const height = this.host.clientHeight;
    if (!this.compressed) {
      const first = Math.max(0, Math.floor((top - ROW_H) / ROW_H) - OVERSCAN);
      const last = Math.min(this.total, Math.ceil((top + height) / ROW_H) + OVERSCAN);
      return [first, last];
    }
    // Compressed: the scroll offset is a position in the file, not a pixel row.
    const usable = Math.max(1, this.sizer.offsetHeight - height);
    const ratio = Math.min(1, Math.max(0, top / usable));
    const page = this.pageRows;
    const anchor = Math.round(ratio * Math.max(0, this.total - page));
    return [
      Math.max(0, anchor - OVERSCAN),
      Math.min(this.total, anchor + page + OVERSCAN),
    ];
  }

  render() {
    if (!this.columns.length) return;
    const [first, last] = this.visibleRange();
    this.ensureLoaded(first, last);

    const body = el('tbody');
    for (let index = first; index < last; index++) {
      const row = this.cache.get(index);
      const tr = el('tr', { dataset: { row: index } });
      tr.append(el('td', {
        class: 'rownum',
        style: { width: rowNumWidth(this.total) + 'px', minWidth: rowNumWidth(this.total) + 'px' },
      }, count(index + 1)));

      for (let c = 0; c < this.columns.length; c++) {
        const column = this.columns[c];
        const width = this.widths.get(column.name);
        if (!row) {
          // Placeholder while the block is in flight — keeps scrolling smooth.
          tr.append(el('td', {
            style: { width: width + 'px', minWidth: width + 'px', color: 'var(--text-subtle)' },
          }, ''));
          continue;
        }
        const value = row[c];
        const isNull = value === null || value === undefined;
        const isNum = column.kind === 'number' && !isNull;
        const selected = this.selected && this.selected.row === index && this.selected.col === c;
        const td = el('td', {
          class: [isNum ? 'num' : '', isNull ? 'null' : '', selected ? 'sel' : ''].filter(Boolean).join(' '),
          style: { width: width + 'px', minWidth: width + 'px' },
          dataset: { row: index, col: c },
          title: isNull ? '' : String(value),
        }, isNull ? '—' : (isNum ? num(value) : String(value)));
        tr.append(td);
      }
      body.append(tr);
    }

    this.table.querySelector('tbody')?.remove();
    this.table.append(body);
    // Uncompressed, the window sits at its true pixel offset. Compressed, there
    // is no such offset, so it is parked just above the viewport instead.
    this.table.style.top = this.compressed
      ? Math.max(0, this.host.scrollTop - OVERSCAN * ROW_H) + 'px'
      : (first * ROW_H) + 'px';

    body.addEventListener('click', (event) => {
      const cell = event.target.closest('td[data-col]');
      if (!cell) return;
      this.select(Number(cell.dataset.row), Number(cell.dataset.col));
    });
    body.addEventListener('dblclick', (event) => {
      const cell = event.target.closest('td[data-col]');
      if (!cell) return;
      const row = this.cache.get(Number(cell.dataset.row));
      if (row) this.opts.onInspect?.(this.columns[Number(cell.dataset.col)], row[Number(cell.dataset.col)]);
    });
  }

  select(rowIndex, colIndex) {
    this.selected = { row: rowIndex, col: colIndex };
    const row = this.cache.get(rowIndex);
    this.render();
    this.opts.onSelect?.({
      row: rowIndex,
      col: colIndex,
      column: this.columns[colIndex],
      value: row ? row[colIndex] : undefined,
    });
  }

  onKey(event) {
    if (!this.selected) return;
    const page = this.pageRows;
    const moves = {
      ArrowDown: [1, 0], ArrowUp: [-1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1],
      PageDown: [page, 0], PageUp: [-page, 0],
    };
    if (event.key === 'Home') {
      event.preventDefault();
      this.scrollToRow(0);
      return this.select(0, this.selected.col);
    }
    if (event.key === 'End') {
      event.preventDefault();
      const last = Math.max(0, this.total - 1);
      this.scrollToRow(Math.max(0, this.total - page));
      return this.select(last, this.selected.col);
    }
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    const row = Math.max(0, Math.min(this.total - 1, this.selected.row + move[0]));
    const col = Math.max(0, Math.min(this.columns.length - 1, this.selected.col + move[1]));
    this.select(row, col);

    // Keep the cursor inside the viewport, going through the same mapping the
    // scrollbar uses so this behaves identically in both modes.
    const [first, last] = this.visibleRange();
    if (row < first + OVERSCAN) this.scrollToRow(Math.max(0, row - 1));
    else if (row > last - OVERSCAN - 1) this.scrollToRow(Math.max(0, row - page + 2));
  }

  // ---------------------------------------------------------------- data

  async ensureLoaded(first, last) {
    const firstBlock = Math.floor(first / BLOCK);
    const lastBlock = Math.floor(Math.max(first, last - 1) / BLOCK);
    for (let block = firstBlock; block <= lastBlock; block++) {
      if (this.inflight.has(block)) continue;
      if (this.cache.has(block * BLOCK)) continue;
      this.loadBlock(block);
    }
  }

  async loadBlock(block) {
    const generation = this.generation;
    this.inflight.add(block);
    try {
      const offset = block * BLOCK;
      const data = await this.opts.fetchRows(offset, BLOCK);
      if (generation !== this.generation) return;   // filters changed under us
      data.rows.forEach((row, i) => this.cache.set(offset + i, row));
      // Keep memory bounded on a long scroll through tens of millions of rows.
      if (this.cache.size > 60_000) this.trimCache();
      this.render();
    } catch (error) {
      if (error.name !== 'AbortError') this.opts.onError?.(error);
    } finally {
      this.inflight.delete(block);
    }
  }

  trimCache() {
    const [first, last] = this.visibleRange();
    const keepFrom = first - BLOCK * 8;
    const keepTo = last + BLOCK * 8;
    for (const key of this.cache.keys()) {
      if (key < keepFrom || key > keepTo) this.cache.delete(key);
    }
  }
}

function defaultWidth(column) {
  const base = { number: 122, date: 132, boolean: 84 }[column.kind] || 176;
  // Let a long header breathe rather than truncating it on first paint.
  return Math.min(300, Math.max(base, column.name.length * 8 + 54));
}

function rowNumWidth(total) {
  return Math.max(52, String(total || 0).length * 8 + 26);
}
