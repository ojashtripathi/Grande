// The PivotTable, built to work the way Excel's does.
//
// Same model, same vocabulary, same gestures: a field list with checkboxes, the
// four areas (Filters, Columns, Rows, Values), drag between them, reorder within
// them, a field menu on each pill, Value Field Settings with "Summarize Values
// By" and "Show Values As", compact/outline/tabular layout, expand and collapse,
// Row Labels / Column Labels / Grand Total, and double-click for Show Details.
//
// What is deliberately *not* copied is the colour: this uses the app's palette
// rather than Excel's blue, so the two halves of the product look like one
// product. Everything a user's hands know is the same.

import { api } from './api.js';
import {
  el, icon, $, $$, num, count, compact, percent, toast, dialog,
  popover, closePopover, kindLabel, debounce,
} from './ui.js';
import { state, on, activeFilters, runJob } from './store.js';

/** Excel's "Summarize Values By" list, in Excel's order. */
const SUMMARIES = [
  ['sum', 'Sum'], ['count_all', 'Count'], ['avg', 'Average'],
  ['max', 'Max'], ['min', 'Min'], ['count', 'Count Numbers'],
  ['count_distinct', 'Distinct Count'], ['median', 'Median'], ['stddev', 'StdDev'],
];

/** Excel's "Show Values As" list. */
const SHOW_AS = [
  ['value', 'No Calculation'],
  ['percent_of_total', '% of Grand Total'],
  ['percent_of_column', '% of Column Total'],
  ['percent_of_row', '% of Row Total'],
  ['running_total', 'Running Total In'],
  ['rank', 'Rank Largest to Smallest'],
];

const DATE_GROUPS = [
  ['', 'No grouping'], ['year', 'Years'], ['quarter', 'Quarters'],
  ['month', 'Months'], ['week', 'Weeks'], ['day', 'Days'], ['hour', 'Hours'],
];

const ZONES = ['filters', 'columns', 'rows', 'values'];

/** The report layout. Each entry is {column, group, bin} or a value field. */
const layout = { filters: [], columns: [], rows: [], values: [] };

let result = null;
let inflight = null;
let collapsed = new Set();      // row keys the user has collapsed
let form = 'compact';
let showSubtotals = true;
let showGrandTotals = true;
let deferred = false;
let dirty = false;
let drag = null;                // {zone, index, column}

// ---------------------------------------------------------------- lifecycle

export function mountPivot() {
  for (const zone of ZONES) wireZone(zone);

  $('#pivot-search').addEventListener('input', debounce(drawFieldList, 150));
  $('#pivot-layout').addEventListener('change', (e) => { form = e.target.value; render(); });
  $('#pivot-expand').addEventListener('click', () => { collapsed.clear(); render(); });
  $('#pivot-collapse').addEventListener('click', collapseAll);
  $('#pivot-subtotals-btn').addEventListener('click', (e) => subtotalMenu(e.currentTarget));
  $('#pivot-totals-btn').addEventListener('click', (e) => grandTotalMenu(e.currentTarget));
  $('#pivot-sql').addEventListener('click', showSql);
  $('#pivot-export').addEventListener('click', exportPivot);
  $('#pivot-fields-toggle').addEventListener('click', togglePane);
  $('#pivot-pane-close').addEventListener('click', togglePane);
  $('#pivot-defer').addEventListener('change', (e) => {
    deferred = e.target.checked;
    $('#pivot-update').disabled = !deferred || !dirty;
    if (!deferred && dirty) compute();
  });
  $('#pivot-update').addEventListener('click', () => { compute(); });

  on('dataset', () => {
    for (const zone of ZONES) layout[zone] = [];
    collapsed = new Set();
    result = null;
    drawFieldList();
    drawZones();
    $('#pivot-empty').hidden = false;
    $('#pivot-wrap').replaceChildren($('#pivot-empty'));
    $('#pivot-filters').hidden = true;
    $('#pivot-note').replaceChildren();
  });
  on('query', debounce(() => { if (state.view === 'pivot' && hasLayout()) compute(); }, 150));
  on('view', (name) => { if (name === 'pivot' && !result && hasLayout()) compute(); });
}

const hasLayout = () => layout.rows.length || layout.columns.length || layout.values.length;

/**
 * A stable key for one row group, used to remember what is collapsed.
 *
 * JSON rather than joining on a sentinel character: a sentinel can appear
 * inside a real value, and picking an "impossible" one invites exactly the
 * escaping mistakes that put raw NUL bytes in this file once already.
 */
const keyOf = (path) => JSON.stringify(path);

function togglePane() {
  const pane = $('#pivot-pane');
  pane.classList.toggle('collapsed');
}

// -------------------------------------------------------------- field list

function drawFieldList() {
  const host = $('#pivot-fieldlist');
  if (!state.dataset) { host.replaceChildren(); return; }
  const term = ($('#pivot-search').value || '').toLowerCase();

  host.replaceChildren(...state.dataset.columns
    .filter((c) => c.name.toLowerCase().includes(term))
    .map((column) => {
      const used = isUsed(column.name);
      const box = el('input', {
        type: 'checkbox', checked: used,
        onchange: () => (box.checked ? autoPlace(column) : removeEverywhere(column.name)),
      });
      const row = el('label', {
        class: 'pane-field', draggable: 'true',
        title: `${column.name} — ${column.type}`,
        ondragstart: (e) => {
          drag = { zone: null, index: -1, column: column.name };
          e.dataTransfer.setData('text/plain', column.name);
          e.dataTransfer.effectAllowed = 'copy';
        },
        ondragend: () => { drag = null; clearDropMarks(); },
      },
        box,
        el('span', { class: 'k' }, kindLabel(column.kind)),
        el('span', { class: 'nm' }, column.name));
      return row;
    }));
}

function isUsed(name) {
  return ZONES.some((z) => layout[z].some((f) => f.column === name));
}

/** Excel's rule: text and dates go to Rows, numbers go to Values. */
function autoPlace(column) {
  if (column.kind === 'number') {
    layout.values.push({ column: column.name, agg: 'sum', show_as: 'value', label: null });
  } else if (column.kind === 'date') {
    layout.rows.push({ column: column.name, group: 'month', bin: null });
  } else {
    layout.rows.push({ column: column.name, group: null, bin: null });
  }
  changed();
}

function removeEverywhere(name) {
  for (const zone of ZONES) layout[zone] = layout[zone].filter((f) => f.column !== name);
  changed();
}

// -------------------------------------------------------------------- zones

function wireZone(zone) {
  const drop = $(`#zone-${zone}`);
  drop.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
    drop.classList.add('over');
    markInsertion(drop, e.clientY);
  });
  drop.addEventListener('dragleave', (e) => {
    if (!drop.contains(e.relatedTarget)) { drop.classList.remove('over'); clearDropMarks(); }
  });
  drop.addEventListener('drop', (e) => {
    e.preventDefault();
    drop.classList.remove('over');
    const at = insertionIndex(drop, e.clientY);
    clearDropMarks();
    const name = drag?.column || e.dataTransfer.getData('text/plain');
    if (name) moveField(name, drag?.zone ?? null, zone, at);
    drag = null;
  });
}

/** Where in the zone would a drop land, given the pointer's Y position. */
function insertionIndex(drop, clientY) {
  const pills = [...drop.querySelectorAll('.pivot-pill')];
  for (let i = 0; i < pills.length; i++) {
    const box = pills[i].getBoundingClientRect();
    if (clientY < box.top + box.height / 2) return i;
  }
  return pills.length;
}

function markInsertion(drop, clientY) {
  clearDropMarks();
  const pills = [...drop.querySelectorAll('.pivot-pill')];
  const at = insertionIndex(drop, clientY);
  if (pills[at]) pills[at].classList.add('drop-before');
  else if (pills.length) pills[pills.length - 1].classList.add('drop-after');
}

function clearDropMarks() {
  for (const node of $$('.drop-before, .drop-after')) {
    node.classList.remove('drop-before', 'drop-after');
  }
}

function moveField(name, fromZone, toZone, at = null) {
  const column = state.dataset?.columns.find((c) => c.name === name);
  if (!column) return;

  let entry = null;
  if (fromZone) {
    const index = layout[fromZone].findIndex((f) => f.column === name);
    if (index >= 0) entry = layout[fromZone].splice(index, 1)[0];
    // Dragging within one zone: the removal shifts everything after it.
    if (fromZone === toZone && at !== null && at > index) at -= 1;
  } else {
    // Excel allows the same field in both Rows and Values; elsewhere it moves.
    for (const zone of ZONES) {
      if (zone === 'values' && toZone !== 'values') continue;
      if (zone !== 'values' && toZone === 'values') continue;
      layout[zone] = layout[zone].filter((f) => f.column !== name);
    }
  }

  if (!entry || (toZone === 'values') !== (fromZone === 'values')) {
    entry = toZone === 'values'
      ? { column: name, agg: column.kind === 'number' ? 'sum' : 'count_all', show_as: 'value', label: null }
      : { column: name, group: column.kind === 'date' ? 'month' : null, bin: null };
  }

  const target = layout[toZone];
  if (at === null || at > target.length) target.push(entry);
  else target.splice(at, 0, entry);
  changed();
}

function drawZones() {
  for (const zone of ZONES) {
    const drop = $(`#zone-${zone}`);
    const entries = layout[zone];
    if (!entries.length) {
      drop.replaceChildren(el('span', { class: 'area-empty' },
        zone === 'values' ? 'Σ Values' : zone === 'filters' ? 'Drop fields here' : 'Drop fields here'));
      continue;
    }
    drop.replaceChildren(...entries.map((entry, index) => pill(zone, entry, index)));
  }
  $('#pivot-update').disabled = !deferred || !dirty;
}

function pill(zone, entry, index) {
  const label = zone === 'values' ? valueLabel(entry) : fieldLabel(entry);
  const node = el('div', {
    class: 'pivot-pill', draggable: 'true', title: label,
    ondragstart: (e) => {
      drag = { zone, index, column: entry.column };
      e.dataTransfer.setData('text/plain', entry.column);
      e.dataTransfer.effectAllowed = 'move';
      node.classList.add('dragging');
    },
    ondragend: () => { node.classList.remove('dragging'); drag = null; clearDropMarks(); },
    onclick: (e) => { e.preventDefault(); fieldMenu(zone, entry, index, node); },
  },
    el('span', { class: 'nm' }, label),
    el('button', {
      class: 'caret', 'aria-label': `Options for ${entry.column}`,
      onclick: (e) => { e.stopPropagation(); fieldMenu(zone, entry, index, node); },
    }, icon('down', 11)));
  return node;
}

function fieldLabel(entry) {
  if (entry.group) return `${entry.column} (${entry.group}s)`;
  if (entry.bin) return `${entry.column} (bands of ${entry.bin})`;
  return entry.column;
}

function valueLabel(entry) {
  if (entry.label) return entry.label;
  const word = SUMMARIES.find(([id]) => id === entry.agg)?.[1] || entry.agg;
  return entry.agg === 'count_all' ? `Count of ${entry.column}` : `${word} of ${entry.column}`;
}

// --------------------------------------------------------------- field menu

function fieldMenu(zone, entry, index, anchor) {
  const menu = el('div');
  const entries = layout[zone];
  const move = (to) => { closePopover(); moveField(entry.column, zone, to); };
  const reorder = (at) => {
    closePopover();
    entries.splice(index, 1);
    entries.splice(Math.max(0, Math.min(at, entries.length)), 0, entry);
    changed();
  };

  menu.append(el('div', { class: 'menu-head' }, entry.column));
  if (entries.length > 1) {
    menu.append(
      item('up', 'Move Up', () => reorder(index - 1), index === 0),
      item('down', 'Move Down', () => reorder(index + 1), index === entries.length - 1),
      item('up', 'Move to Beginning', () => reorder(0), index === 0),
      item('down', 'Move to End', () => reorder(entries.length), index === entries.length - 1),
      el('div', { class: 'menu-sep' }),
    );
  }
  for (const zoneName of ZONES) {
    if (zoneName === zone) continue;
    const words = {
      filters: 'Move to Report Filter', columns: 'Move to Column Labels',
      rows: 'Move to Row Labels', values: 'Move to Values',
    };
    menu.append(item('right', words[zoneName], () => move(zoneName)));
  }
  menu.append(el('div', { class: 'menu-sep' }));

  if (zone === 'values') {
    menu.append(item('sum', 'Value Field Settings…', () => {
      closePopover();
      valueFieldSettings(entry);
    }));
  } else {
    const column = state.dataset?.columns.find((c) => c.name === entry.column);
    if (column?.kind === 'date' || column?.kind === 'number') {
      menu.append(item('pivot', 'Group…', () => { closePopover(); groupSettings(entry, column); }));
    }
  }
  menu.append(item('x', 'Remove Field', () => {
    closePopover();
    entries.splice(index, 1);
    changed();
  }));

  popover(anchor, menu, { width: 244 });
}

function item(iconName, label, onClick, disabled = false) {
  return el('button', {
    class: 'menu-item', disabled,
    style: disabled ? { opacity: .4, cursor: 'default' } : null,
    onclick: disabled ? null : onClick,
  }, icon(iconName, 15), label);
}

/** Excel's Value Field Settings dialog, with its two tabs. */
function valueFieldSettings(entry) {
  const name = el('input', { class: 'field', style: { width: '100%' }, value: valueLabel(entry) });
  const summary = el('select', { class: 'field', size: 8, style: { width: '100%' } },
    ...SUMMARIES.map(([id, label]) => el('option', { value: id, selected: entry.agg === id }, label)));
  const showAs = el('select', { class: 'field', style: { width: '100%' } },
    ...SHOW_AS.map(([id, label]) => el('option', { value: id, selected: entry.show_as === id }, label)));

  const box = dialog({
    title: 'Value Field Settings',
    body: el('div', {},
      el('div', { class: 'form-row' },
        el('label', { class: 'field-label' }, 'Source Name'),
        el('div', { style: { fontFamily: 'var(--mono)', fontSize: '13px' } }, entry.column)),
      el('div', { class: 'form-row' },
        el('label', { class: 'field-label' }, 'Custom Name'), name),
      el('div', { class: 'form-row' },
        el('label', { class: 'field-label' }, 'Summarize Values By'),
        el('div', { class: 'hint', style: { marginTop: 0, marginBottom: '6px' } },
          'Choose how the values are calculated.'),
        summary),
      el('div', { class: 'form-row' },
        el('label', { class: 'field-label' }, 'Show Values As'), showAs)),
    footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn', onclick: () => box.close() }, 'Cancel'),
      el('button', {
        class: 'btn btn-primary',
        onclick: () => {
          const previousAuto = valueLabel(entry);
          entry.agg = summary.value;
          entry.show_as = showAs.value;
          // Keep a name the user actually typed; drop one we generated.
          entry.label = (name.value && name.value !== previousAuto) ? name.value : null;
          box.close();
          changed();
        },
      }, 'OK')),
  });
}

/** Excel's Grouping dialog — by date part, or into numeric bands. */
function groupSettings(entry, column) {
  const isDate = column.kind === 'date';
  const group = el('select', { class: 'field', size: 7, style: { width: '100%' } },
    ...DATE_GROUPS.map(([id, label]) => el('option', { value: id, selected: (entry.group || '') === id }, label)));
  const size = el('input', {
    class: 'field', type: 'number', min: '0', step: 'any',
    style: { width: '100%' }, value: entry.bin ?? '',
    placeholder: 'e.g. 100',
  });

  const box = dialog({
    title: 'Grouping',
    body: isDate
      ? el('div', {},
          el('label', { class: 'field-label' }, 'By'), group,
          el('div', { class: 'hint' },
            'Add the same date field more than once — Years, then Months — to nest it, as Excel does.'))
      : el('div', {},
          el('label', { class: 'field-label' }, 'Band size'), size,
          el('div', { class: 'hint' }, 'Values are grouped into equal bands of this width.')),
    footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn', onclick: () => box.close() }, 'Cancel'),
      el('button', {
        class: 'btn btn-primary',
        onclick: () => {
          if (isDate) entry.group = group.value || null;
          else entry.bin = size.value ? Number(size.value) : null;
          box.close();
          changed();
        },
      }, 'OK')),
  });
}

function subtotalMenu(anchor) {
  popover(anchor, el('div', {},
    el('div', { class: 'menu-head' }, 'Subtotals'),
    item('check', 'Show all Subtotals', () => { closePopover(); showSubtotals = true; changed(); }),
    item('x', 'Do Not Show Subtotals', () => { closePopover(); showSubtotals = false; changed(); }),
  ), { width: 232 });
}

function grandTotalMenu(anchor) {
  popover(anchor, el('div', {},
    el('div', { class: 'menu-head' }, 'Grand Totals'),
    item('check', 'On for Rows and Columns', () => { closePopover(); showGrandTotals = true; render(); }),
    item('x', 'Off for Rows and Columns', () => { closePopover(); showGrandTotals = false; render(); }),
  ), { width: 232 });
}

function collapseAll() {
  if (!result) return;
  const walk = (nodes) => {
    for (const node of nodes.values()) {
      if (node.children.size) {
        collapsed.add(keyOf(node.path));
        walk(node.children);
      }
    }
  };
  walk(buildTree(result.rows, result.row_fields.length));
  render();
}

// ------------------------------------------------------------------ compute

function changed() {
  drawFieldList();
  drawZones();
  renderReportFilters();
  dirty = true;
  if (deferred) {
    $('#pivot-update').disabled = false;
    return;
  }
  compute();
}

async function compute() {
  if (!state.dataset) return;
  const wrap = $('#pivot-wrap');

  if (!hasLayout()) {
    $('#pivot-empty').hidden = false;
    wrap.replaceChildren($('#pivot-empty'));
    result = null;
    dirty = false;
    return;
  }

  inflight?.abort();
  const controller = new AbortController();
  inflight = controller;

  wrap.replaceChildren(el('div', { class: 'pivot-busy' },
    el('span', { class: 'spin', style: { display: 'inline-flex' } }, icon('loader', 16)),
    'Calculating…'));

  try {
    const data = await api.pivot(state.dataset.id, {
      rows: layout.rows.map(cleanField),
      columns: layout.columns.map(cleanField),
      values: layout.values.length
        ? layout.values.map((v) => ({
            column: v.column, agg: v.agg, show_as: v.show_as, label: v.label || undefined,
          }))
        : [{ agg: 'count_all', show_as: 'value' }],
      filters: allFilters(),
      subtotals: showSubtotals,
    }, { signal: controller.signal });
    result = data;
    dirty = false;
    $('#pivot-update').disabled = true;
    render();
  } catch (error) {
    if (error.name === 'AbortError') return;
    wrap.replaceChildren(el('div', { style: { padding: '20px' } },
      el('div', { class: 'note note-bad' }, icon('alert', 15), error.message)));
  } finally {
    if (inflight === controller) inflight = null;
  }
}

const cleanField = (f) => ({ column: f.column, group: f.group || null, bin: f.bin || null });

/** Grid filters plus the report filters chosen in the Filters area. */
function allFilters() {
  const out = [...activeFilters()];
  for (const entry of layout.filters) {
    if (entry.selected && entry.selected.length) {
      out.push({ column: entry.column, op: 'in', values: entry.selected });
    }
  }
  return out;
}

// ----------------------------------------------------------- report filters

function renderReportFilters() {
  const host = $('#pivot-filters');
  if (!layout.filters.length) { host.hidden = true; host.replaceChildren(); return; }
  host.hidden = false;
  host.replaceChildren(...layout.filters.map((entry) => {
    const chosen = entry.selected?.length
      ? (entry.selected.length === 1 ? entry.selected[0] : `(${entry.selected.length} items)`)
      : '(All)';
    const button = el('button', {
      class: 'filter-box',
      onclick: () => reportFilterMenu(entry, button),
    }, el('span', { class: 'v' }, chosen), icon('down', 12));
    return el('div', { class: 'filter-row' },
      el('span', { class: 'filter-name' }, entry.column), button);
  }));
}

async function reportFilterMenu(entry, anchor) {
  const list = el('div', { class: 'checklist' }, el('div', { class: 'hint' }, 'Loading…'));
  const chosen = new Set(entry.selected || []);
  const body = el('div', {}, el('div', { class: 'menu-head' }, entry.column), list,
    el('div', { style: { display: 'flex', gap: '6px', marginTop: '6px' } },
      el('button', {
        class: 'btn btn-sm btn-primary', style: { flex: 1 },
        onclick: () => { closePopover(); entry.selected = [...chosen]; changed(); },
      }, 'OK'),
      el('button', {
        class: 'btn btn-sm',
        onclick: () => { closePopover(); entry.selected = []; changed(); },
      }, 'Clear')));
  popover(anchor, body, { width: 264 });

  try {
    const data = await api.distinct(state.dataset.id, { column: entry.column, limit: 200 });
    list.replaceChildren(...data.values.map((v) => {
      const box = el('input', {
        type: 'checkbox', checked: chosen.has(v.value),
        onchange: () => (box.checked ? chosen.add(v.value) : chosen.delete(v.value)),
      });
      return el('label', { class: 'check' }, box,
        el('span', { class: 'v' }, v.value ?? '(blank)'),
        el('span', { class: 'n' }, compact(v.count)));
    }));
  } catch (error) {
    list.replaceChildren(el('div', { class: 'hint' }, error.message));
  }
}

// ------------------------------------------------------------------- render

function render() {
  if (!result) return;
  const wrap = $('#pivot-wrap');
  const rowFields = result.row_fields;
  const colFields = result.column_fields;
  const values = result.values;
  const asPercent = values.map((v) => v.show_as.startsWith('percent'));

  const notes = [];
  if (result.has_other) {
    notes.push(el('span', {
      class: 'tag tag-warn',
      title: 'The remaining categories are grouped into “Other”, so the totals still add up.',
    }, 'Grouped into Other'));
  }
  if (result.truncated) notes.push(el('span', { class: 'tag tag-warn' }, `First ${count(result.row_count)} groups`));
  $('#pivot-note').replaceChildren(...notes);

  const table = el('table', { class: `pivot form-${form}` });

  // ---- header -------------------------------------------------------------
  const head = el('thead');
  const labelColumns = form === 'compact' ? 1 : Math.max(1, rowFields.length);

  if (colFields.length) {
    // Excel's arrangement: a "Column Labels" banner, then one header row per
    // column field, then the measure names when there is more than one.
    const measureRow = values.length > 1 ? 1 : 0;
    const headerRows = 1 + colFields.length + measureRow;

    const banner = el('tr');
    banner.append(el('th', {
      class: 'corner', rowspan: headerRows, colspan: labelColumns,
    }, form === 'compact' ? 'Row Labels' : (rowFields.map((f) => f.label).join(' / ') || 'Row Labels')));
    banner.append(el('th', { class: 'collabel', colspan: result.columns.length }, 'Column Labels'));
    head.append(banner);

    for (let level = 0; level < colFields.length; level++) {
      const tr = el('tr');
      // Merge neighbouring headers that carry the same label, so a two-field
      // column layout reads as groups rather than repetition.
      const runs = [];
      for (const meta of result.columns) {
        const text = meta.role === 'cell'
          ? (meta.labels[level] ?? '(blank)')
          : (level === 0 ? meta.labels[0] : '');
        const cls = meta.role === 'row_total' ? 'grand' : meta.role === 'other' ? 'other' : '';
        const last = runs[runs.length - 1];
        if (last && last.text === text && last.cls === cls && meta.role === 'cell' && last.role === 'cell') {
          last.span += 1;
        } else {
          runs.push({ text, cls, span: 1, role: meta.role });
        }
      }
      for (const run of runs) tr.append(el('th', { class: run.cls, colspan: run.span }, run.text));
      head.append(tr);
    }

    if (measureRow) {
      const tr = el('tr');
      for (const meta of result.columns) tr.append(el('th', { class: 'measure' }, meta.value_label));
      head.append(tr);
    }
  } else {
    const tr = el('tr');
    if (form === 'compact') {
      tr.append(el('th', { class: 'corner' }, 'Row Labels'));
    } else {
      for (const field of rowFields) tr.append(el('th', { class: 'corner' }, field.label));
      if (!rowFields.length) tr.append(el('th', { class: 'corner' }, ''));
    }
    for (const meta of result.columns) tr.append(el('th', {}, meta.value_label));
    head.append(tr);
  }
  table.append(head);

  // ---- body ---------------------------------------------------------------
  // ROLLUP returns each group's subtotal *after* its members. Excel shows the
  // group first, carrying its own totals, with the members indented beneath —
  // so the flat result is rebuilt into a tree and walked parents-first.
  const body = el('tbody');
  const tree = buildTree(result.rows, rowFields.length);

  const emit = (node, level) => {
    const key = keyOf(node.path);
    const isCollapsed = collapsed.has(key);
    const hasChildren = node.children.size > 0;
    const tr = el('tr', { class: hasChildren ? 'subtotal' : '' });

    const label = display(node.path[level], rowFields[level]);
    if (form === 'compact') {
      const cell = el('td', {
        class: 'rowlabel', style: { paddingLeft: (8 + level * 18) + 'px' },
      });
      if (hasChildren) {
        cell.append(el('button', {
          class: 'twisty',
          'aria-label': isCollapsed ? `Expand ${label}` : `Collapse ${label}`,
          onclick: (e) => {
            e.stopPropagation();
            isCollapsed ? collapsed.delete(key) : collapsed.add(key);
            render();
          },
        }, isCollapsed ? '+' : '−'));
      }
      cell.append(el('span', {}, label));
      tr.append(cell);
    } else {
      for (let i = 0; i < Math.max(1, rowFields.length); i++) {
        tr.append(el('td', { class: 'rowlabel' }, i === level ? label : ''));
      }
    }

    appendCells(tr, node.row, node);
    body.append(tr);

    if (hasChildren && !isCollapsed) {
      for (const child of node.children.values()) emit(child, level + 1);
      // Outline and tabular forms repeat the group as an explicit Total row,
      // the way Excel does when subtotals sit at the bottom.
      if (form !== 'compact' && showSubtotals && node.row) {
        const totalRow = el('tr', { class: 'subtotal' });
        for (let i = 0; i < Math.max(1, rowFields.length); i++) {
          totalRow.append(el('td', { class: 'rowlabel' }, i === level ? `${label} Total` : ''));
        }
        appendCells(totalRow, node.row, node);
        body.append(totalRow);
      }
    }
  };

  function appendCells(tr, row, node) {
    const cells = row ? row.cells : [];
    result.columns.forEach((meta, index) => {
      if (!showGrandTotals && meta.role === 'row_total' && colFields.length) return;
      tr.append(el('td', {
        class: meta.role === 'row_total' ? 'grand' : '',
        ondblclick: row ? () => drill(node, meta) : null,
        title: row ? 'Double-click to see the rows behind this figure' : '',
      }, formatCell(cells[index], asPercent[meta.value_index])));
    });
  }

  for (const node of tree.values()) emit(node, 0);
  table.append(body);

  if (result.grand_total && showGrandTotals) {
    const foot = el('tfoot');
    const tr = el('tr', { class: 'grandrow' });
    tr.append(el('td', { class: 'rowlabel', colspan: labelColumns }, 'Grand Total'));
    result.grand_total.cells.forEach((value, index) => {
      const meta = result.columns[index];
      if (!showGrandTotals && meta.role === 'row_total' && colFields.length) return;
      tr.append(el('td', { class: meta.role === 'row_total' ? 'grand' : '' },
        formatCell(value, asPercent[meta.value_index])));
    });
    foot.append(tr);
    table.append(foot);
  }

  wrap.replaceChildren(table);
}

/**
 * Turn ROLLUP's flat output into the nesting Excel displays.
 *
 * A row with depth `d` describes the group formed by its first `levels - d`
 * keys: depth 0 is a leaf, and anything above is that group's subtotal. Both
 * land on the same tree node, so a group carries its own totals and its
 * members sit beneath it.
 */
function buildTree(rows, levels) {
  const root = new Map();
  for (const row of rows) {
    const path = row.keys.slice(0, levels - row.depth);
    if (!path.length) continue;
    let level = root;
    let node = null;
    for (let i = 0; i < path.length; i++) {
      const key = String(path[i]);
      if (!level.has(key)) {
        level.set(key, { path: path.slice(0, i + 1), row: null, children: new Map() });
      }
      node = level.get(key);
      level = node.children;
    }
    if (node) node.row = row;
  }
  return root;
}

function display(value, field) {
  if (value === null || value === undefined) return '(blank)';
  if (field?.group) return formatPeriod(value, field.group);
  if (field?.bin) {
    const low = Number(value);
    return `${num(low)} – ${num(low + Number(field.bin))}`;
  }
  return String(value);
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Excel's own labels: "2024", "Q1 2024", "Mar 2024". */
function formatPeriod(value, group) {
  const text = String(value);
  const date = new Date(text.includes('T') ? text : text.replace(' ', 'T'));
  if (Number.isNaN(date.getTime())) return text;
  const year = date.getFullYear();
  switch (group) {
    case 'year': return String(year);
    case 'quarter': return `Q${Math.floor(date.getMonth() / 3) + 1} ${year}`;
    case 'month': return `${MONTHS[date.getMonth()]} ${year}`;
    case 'week': return `Week of ${MONTHS[date.getMonth()]} ${date.getDate()}, ${year}`;
    case 'day': return `${date.getDate()} ${MONTHS[date.getMonth()]} ${year}`;
    case 'hour': return `${String(date.getHours()).padStart(2, '0')}:00`;
    default: return text;
  }
}

function formatCell(value, asPercent) {
  if (value === null || value === undefined) return '';
  if (asPercent) return percent(value);
  return typeof value === 'number' ? num(value) : String(value);
}

// -------------------------------------------------------------- show details

async function drill(node, meta) {
  const keys = node.path;
  const body = el('div', {}, el('div', { class: 'hint' }, 'Fetching the rows behind this figure…'));
  const label = [
    ...keys.map((k, i) => display(k, result.row_fields[i])),
    ...(meta.role === 'cell' ? meta.labels.filter(Boolean) : []),
  ].join(' · ');
  dialog({ title: label || 'Show Details', wide: true, body });

  try {
    const data = await api.drill(state.dataset.id, {
      row_fields: result.row_fields.slice(0, keys.length).map(cleanField),
      keys,
      column_fields: meta.role === 'cell' ? result.column_fields.map(cleanField) : [],
      categories: meta.role === 'cell' ? result.categories[meta.category_index] : [],
      filters: allFilters(),
      limit: 200,
    });
    const table = el('table', { class: 'result' },
      el('thead', {}, el('tr', {}, ...data.columns.map((n) => el('th', { title: n }, n)))),
      el('tbody', {}, ...data.rows.map((record) =>
        el('tr', {}, ...record.map((v) => {
          const empty = v === null || v === undefined;
          const numeric = typeof v === 'number';
          return el('td', {
            class: [numeric ? 'num' : '', empty ? 'null' : ''].filter(Boolean).join(' '),
            title: empty ? '' : String(v),
          }, empty ? '—' : (numeric ? num(v) : String(v)));
        })))));
    body.replaceChildren(
      el('div', { class: 'hint', style: { marginBottom: '10px' } },
        `${count(data.filtered_rows ?? data.returned)} rows — showing the first ${count(data.returned)}.`),
      el('div', { style: { overflow: 'auto', maxHeight: '54vh', border: '1px solid var(--border)' } }, table));
  } catch (error) {
    body.replaceChildren(el('div', { class: 'note note-bad' }, icon('alert', 15), error.message));
  }
}

// -------------------------------------------------------------------- misc

function showSql() {
  if (!result) return toast('Build a PivotTable first');
  dialog({
    title: 'The query behind this PivotTable',
    wide: true,
    body: el('pre', {
      style: {
        margin: 0, padding: '14px', background: 'var(--bg-sunken)',
        border: '1px solid var(--border)', borderRadius: 'var(--r-md)',
        overflow: 'auto', maxHeight: '58vh', whiteSpace: 'pre-wrap',
        fontFamily: 'var(--mono)', fontSize: '12.5px', lineHeight: 1.6,
      },
    }, result.sql),
    footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
      el('button', {
        class: 'btn',
        onclick: () => navigator.clipboard?.writeText(result.sql).then(() => toast('Copied')),
      }, 'Copy'),
      el('span', { class: 'spacer' })),
  });
}

async function exportPivot() {
  if (!result) return toast('Build a PivotTable first');
  const rowFields = result.row_fields;
  const header = [
    ...(form === 'compact' ? ['Row Labels'] : rowFields.map((f) => f.label)),
    ...result.columns.map((c) => {
      const parts = [...(c.labels || []).filter(Boolean)];
      if (result.values.length > 1) parts.push(c.value_label);
      return parts.join(' — ') || c.value_label;
    }),
  ];
  const lines = [header];

  // Export exactly what is on screen, collapsed groups included.
  const walk = (nodes, level) => {
    for (const node of nodes.values()) {
      const label = display(node.path[level], rowFields[level]);
      const labels = form === 'compact'
        ? ['  '.repeat(level) + label]
        : rowFields.map((f, i) => (i === level ? label : ''));
      lines.push([...labels, ...(node.row?.cells || []).map((c) => (c ?? ''))]);
      if (node.children.size && !collapsed.has(keyOf(node.path))) {
        walk(node.children, level + 1);
      }
    }
  };
  walk(buildTree(result.rows, rowFields.length), 0);
  if (result.grand_total && showGrandTotals) {
    lines.push([
      'Grand Total', ...(form === 'compact' ? [] : rowFields.slice(1).map(() => '')),
      ...result.grand_total.cells.map((c) => (c ?? '')),
    ]);
  }

  const csv = lines.map((row) => row.map((cell) => {
    const text = String(cell ?? '');
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  }).join(',')).join('\r\n');

  // A BOM so Excel reads UTF-8 correctly; the original's export omitted it and
  // mangled every accented character.
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = el('a', {
    href: url,
    download: `${state.dataset.display_name.replace(/\.[^.]+$/, '')}-pivot.csv`,
  });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  toast('PivotTable exported', { sub: 'Saved to your Downloads folder.', kind: 'good' });
}

export { layout as pivotLayout };
