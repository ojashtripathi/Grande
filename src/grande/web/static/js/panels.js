// The Clean, Export and SQL views.

import { api } from './api.js';
import {
  el, icon, $, $$, num, count, compact, bytes, duration,
  toast, dialog, debounce, kindLabel,
} from './ui.js';
import {
  state, on, emit, activeFilters, visibleColumnNames, visibleColumns,
  patchDataset, isFiltered, runJob,
} from './store.js';

// ============================================================ CLEAN =========

const ACTIONS = [
  {
    id: 'remove_duplicates',
    title: 'Remove duplicate rows',
    blurb: 'Keep one row per combination of the columns you choose. Leave the list empty to compare whole rows.',
    build: (form) => [
      columnPicker(form, 'columns', 'Compare these columns', { multiple: true, optional: true }),
      select(form, 'keep', 'Keep', [['first', 'The first one'], ['last', 'The last one']]),
    ],
  },
  {
    id: 'find_replace',
    title: 'Find and replace',
    blurb: 'Replace text across one column or every text column.',
    build: (form) => [
      columnPicker(form, 'columns', 'In', { multiple: true, optional: true, optionalLabel: 'every text column' }),
      text(form, 'find', 'Find'),
      text(form, 'replace', 'Replace with', { placeholder: '(leave empty to delete)' }),
      check(form, 'match_case', 'Match case'),
      check(form, 'whole_cell', 'Match the whole cell only'),
      check(form, 'regex', 'Use a regular expression'),
    ],
  },
  {
    id: 'clean_text',
    title: 'Tidy text',
    blurb: 'Trim stray spaces, collapse runs of spaces, strip control characters, or change case.',
    build: (form) => [
      columnPicker(form, 'columns', 'Columns', { multiple: true }),
      check(form, 'trim', 'Trim leading and trailing spaces', true),
      check(form, 'collapse_spaces', 'Collapse repeated spaces'),
      check(form, 'remove_non_printing', 'Remove control characters'),
      select(form, 'case', 'Change case', [
        ['', 'Leave as is'], ['upper', 'UPPERCASE'], ['lower', 'lowercase'], ['proper', 'Proper Case'],
      ]),
    ],
  },
  {
    id: 'split_column',
    title: 'Split a column',
    blurb: "Excel's Text to Columns — break one column into several on a separator.",
    build: (form) => [
      columnPicker(form, 'column', 'Column'),
      text(form, 'delimiter', 'Split on', { placeholder: ', or - or a space', value: ',' }),
      number(form, 'into', 'Number of new columns', 2),
      check(form, 'keep_original', 'Keep the original column'),
    ],
  },
  {
    id: 'change_type',
    title: 'Change a column’s type',
    blurb: 'Shows you exactly which values will not convert, before it changes anything.',
    open: openChangeType,
  },
  {
    id: 'rename_column',
    title: 'Rename a column',
    blurb: '',
    build: (form) => [
      columnPicker(form, 'column', 'Column'),
      text(form, 'to', 'New name'),
    ],
  },
  {
    id: 'remove_columns',
    title: 'Delete columns',
    blurb: 'Removes them from the data. To hide them from view only, use the Columns button on the Data tab.',
    build: (form) => [columnPicker(form, 'columns', 'Columns', { multiple: true })],
  },
  {
    id: 'fill_blanks',
    title: 'Fill empty cells',
    blurb: '',
    build: (form) => [
      columnPicker(form, 'columns', 'Columns', { multiple: true }),
      text(form, 'value', 'Fill with', { placeholder: '0, N/A, Unknown…' }),
    ],
  },
  {
    id: 'keep_filtered',
    title: 'Apply the current filter permanently',
    blurb: 'Turns what you are looking at into the data itself — keep the matching rows, or delete them.',
    needsFilter: true,
    build: (form) => [
      select(form, 'invert', 'Then', [['', 'Keep only matching rows'], ['1', 'Delete matching rows']]),
    ],
  },
];

export function mountClean() {
  const host = $('#clean-actions');

  const draw = () => {
    if (!state.dataset) { host.replaceChildren(); return; }
    host.replaceChildren(...ACTIONS.map((action) => {
      const disabled = action.needsFilter && !isFiltered();
      return el('div', {
        style: {
          border: '1px solid var(--border)', borderRadius: 'var(--r-md)',
          background: 'var(--surface)', padding: 'var(--s-3) var(--s-4)',
          display: 'flex', alignItems: 'center', gap: 'var(--s-3)',
          opacity: disabled ? .5 : 1,
        },
      },
        el('div', { style: { flex: 1 } },
          el('div', { style: { fontWeight: 650 } }, action.title),
          action.blurb ? el('div', { class: 'hint', style: { marginTop: '2px' } }, action.blurb) : null,
          disabled ? el('div', { class: 'hint' }, 'Set a filter on the Data tab first.') : null),
        el('button', {
          class: 'btn', disabled,
          onclick: () => (action.open ? action.open() : openAction(action)),
        }, 'Set up…'));
    }));
  };

  $('#btn-undo').addEventListener('click', async () => {
    try {
      const dataset = await api.undo(state.dataset.id);
      patchDataset(dataset);
      toast('Undone');
      drawRecipe(dataset);
    } catch (error) {
      toast('Could not undo', { sub: error.message, kind: 'bad' });
    }
  });

  on('dataset', (dataset) => { draw(); drawRecipe(dataset); });
  on('query', debounce(draw, 200));
}

function openAction(action) {
  const form = {};
  const fields = action.build(form);
  const box = dialog({
    title: action.title,
    body: el('div', {}, ...fields),
    footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn', onclick: () => box.close() }, 'Cancel'),
      el('button', {
        class: 'btn btn-primary',
        onclick: async () => {
          const params = {};
          for (const [key, read] of Object.entries(form)) {
            const value = read();
            if (value !== undefined && value !== '') params[key] = value;
          }
          if (action.id === 'keep_filtered') {
            params.filters = activeFilters();
            params.invert = Boolean(params.invert);
          }
          box.close();
          try {
            const dataset = await api.transform(state.dataset.id, action.id, params);
            patchDataset(dataset);
            const last = dataset.recipe?.[dataset.recipe.length - 1];
            toast(last?.description || 'Done', {
              sub: last && last.rows_changed
                ? `${count(Math.abs(last.rows_changed))} rows ${last.rows_changed < 0 ? 'removed' : 'added'}`
                : `${count(dataset.row_count)} rows`,
              kind: 'good',
            });
            drawRecipe(dataset);
          } catch (error) {
            toast('That did not work', { sub: error.message, kind: 'bad' });
          }
        },
      }, 'Apply')),
  });
}

/**
 * Change a column's type, with a live preview.
 *
 * The old version applied the change and *then* said "600 values did not
 * convert and are now empty" — which is both too late and unhelpful, since it
 * never said which values or why. Here the counts and the actual offending
 * values appear before anything is touched, and the values can be kept.
 */
function openChangeType(preselect) {
  const columns = state.dataset?.columns || [];
  if (!columns.length) return;

  const column = el('select', { class: 'field', style: { width: '100%' } },
    ...columns.map((c) => el('option', {
      value: c.name, selected: c.name === preselect,
    }, `${c.name}  ·  ${c.type}`)));

  const to = el('select', { class: 'field', style: { width: '100%' } },
    ...[['date', 'Date'], ['datetime', 'Date and time'], ['number', 'Number'],
        ['integer', 'Whole number'], ['text', 'Text'], ['boolean', 'True/false']]
      .map(([v, label]) => el('option', { value: v }, label)));

  const dayFirst = el('select', { class: 'field', style: { width: '100%' } },
    el('option', { value: 'true' }, 'Day first — 03/05/2024 is 3 May'),
    el('option', { value: 'false' }, 'Month first — 03/05/2024 is 5 March'));

  const custom = el('input', {
    class: 'field', style: { width: '100%' },
    placeholder: 'leave empty to detect automatically — e.g. %d/%m/%Y',
  });
  const keepOriginal = el('input', { type: 'checkbox' });

  const dateOptions = el('div', {},
    el('div', { class: 'form-row' },
      el('label', { class: 'field-label' }, 'When the day and month are ambiguous'), dayFirst),
    el('div', { class: 'form-row' },
      el('label', { class: 'field-label' }, 'Exact format (optional)'), custom));

  const report = el('div', { style: { minHeight: '96px' } });
  const apply = el('button', { class: 'btn btn-primary' }, 'Change type');
  let latest = null;
  let pending = null;

  const refresh = debounce(async () => {
    const isDate = to.value === 'date' || to.value === 'datetime';
    dateOptions.style.display = isDate ? '' : 'none';
    report.replaceChildren(el('div', { class: 'hint' }, 'Checking every row…'));
    apply.disabled = true;

    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    try {
      const data = await api.transformPreview(state.dataset.id, 'change_type', {
        column: column.value,
        to: to.value,
        date_format: custom.value || null,
        day_first: dayFirst.value === 'true',
      }, { signal: controller.signal });
      latest = data;
      report.replaceChildren(renderPreview(data));
      apply.disabled = false;
    } catch (error) {
      if (error.name === 'AbortError') return;
      report.replaceChildren(el('div', { class: 'note note-bad' }, icon('alert', 15), error.message));
    }
  }, 220);

  for (const control of [column, to, dayFirst, custom]) {
    control.addEventListener('change', refresh);
    control.addEventListener('input', refresh);
  }

  const box = dialog({
    title: 'Change a column’s type',
    wide: true,
    body: el('div', {},
      el('div', { class: 'form-grid' },
        el('div', {}, el('label', { class: 'field-label' }, 'Column'), column),
        el('div', {}, el('label', { class: 'field-label' }, 'Change to'), to)),
      dateOptions,
      el('label', { class: 'check', style: { paddingLeft: 0 } }, keepOriginal,
        el('span', { class: 'v' }, 'Keep the original values in a second column')),
      el('div', { class: 'field-label', style: { marginTop: '16px' } }, 'What will happen'),
      report),
    footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn', onclick: () => box.close() }, 'Cancel'),
      apply),
  });

  apply.addEventListener('click', async () => {
    box.close();
    try {
      const dataset = await api.transform(state.dataset.id, 'change_type', {
        column: column.value,
        to: to.value,
        date_format: custom.value || null,
        day_first: dayFirst.value === 'true',
        keep_original: keepOriginal.checked,
      });
      patchDataset(dataset);
      const last = dataset.recipe?.[dataset.recipe.length - 1];
      toast(last?.description || 'Done', {
        kind: latest && latest.failed ? '' : 'good',
        sub: latest ? `${count(latest.converted)} of ${count(latest.filled)} values converted` : '',
      });
      drawRecipe(dataset);
    } catch (error) {
      toast('That did not work', { sub: error.message, kind: 'bad' });
    }
  });

  refresh();
}

function renderPreview(data) {
  const wrap = el('div');
  const ok = data.failed === 0;

  wrap.append(el('div', { class: `note ${ok ? 'note-good' : 'note-warn'}` },
    icon(ok ? 'check' : 'alert', 15),
    el('div', {},
      el('div', {}, el('b', {}, count(data.converted)), ` of ${count(data.filled)} values will convert`),
      data.failed
        ? el('div', { class: 'hint' },
            `${count(data.failed)} will not. Tick the box above to keep them, or they become empty.`)
        : null,
      data.blank ? el('div', { class: 'hint' }, `${count(data.blank)} cells are already empty.`) : null)));

  if (data.ambiguous) {
    wrap.append(el('div', { class: 'note note-warn', style: { marginTop: '8px' } },
      icon('alert', 15),
      el('div', {},
        el('div', {}, el('b', {}, count(data.ambiguous_rows)), ' rows are ambiguous'),
        el('div', { class: 'hint' },
          'They read differently depending on whether the day or the month comes '
          + 'first. Check the setting above matches how the file was written.'))));
  }

  if (data.failures?.length) {
    wrap.append(el('div', { class: 'field-label', style: { marginTop: '14px' } },
      'Values that will not convert'));
    wrap.append(el('div', { style: { display: 'flex', flexWrap: 'wrap', gap: '5px' } },
      ...data.failures.map((f) => el('span', { class: 'tag tag-warn', title: `${f.count} rows` },
        f.value === null || f.value === '' ? '(blank)' : String(f.value),
        el('span', { style: { opacity: .7, marginLeft: '4px' } }, `×${count(f.count)}`)))));
  }

  if (data.samples?.length) {
    wrap.append(el('div', { class: 'field-label', style: { marginTop: '14px' } }, 'Examples'));
    wrap.append(el('table', { style: { width: '100%', fontSize: '13px' } },
      ...data.samples.map((s) => el('tr', {},
        el('td', { style: { padding: '2px 0', fontFamily: 'var(--mono)', color: 'var(--text-muted)' } }, s.before),
        el('td', { style: { padding: '2px 0', width: '20px', color: 'var(--text-subtle)' } }, '→'),
        el('td', { style: { padding: '2px 0', fontFamily: 'var(--mono)', fontWeight: 600 } }, s.after)))));
  }
  return wrap;
}

function drawRecipe(dataset) {
  const host = $('#recipe-list');
  $('#btn-undo').disabled = !dataset?.can_undo;
  if (!dataset) { host.replaceChildren(); return; }
  const steps = dataset.recipe || [];
  if (!steps.length) {
    host.replaceChildren(el('div', { class: 'hint' },
      'Nothing changed yet. Every step you apply is listed here and can be undone.'));
    return;
  }
  host.replaceChildren(...steps.map((step, index) =>
    el('div', {
      style: {
        display: 'flex', gap: '8px', padding: '8px 0',
        borderBottom: '1px solid var(--border)',
      },
    },
      el('span', { class: 'tag' }, String(index + 1)),
      el('div', { style: { flex: 1 } },
        el('div', { style: { fontSize: '13px' } }, step.description),
        step.rows_changed
          ? el('div', { class: 'hint' },
              `${count(Math.abs(step.rows_changed))} rows ${step.rows_changed < 0 ? 'removed' : 'added'}`)
          : null))));
}

// --- small form helpers ------------------------------------------------------

function text(form, key, label, { placeholder = '', value = '' } = {}) {
  const input = el('input', { class: 'field', placeholder, value, style: { width: '100%' } });
  form[key] = () => input.value;
  return el('div', { class: 'form-row' }, el('label', { class: 'field-label' }, label), input);
}

function number(form, key, label, value) {
  const input = el('input', { class: 'field', type: 'number', value, min: 2, style: { width: '100%' } });
  form[key] = () => Number(input.value);
  return el('div', { class: 'form-row' }, el('label', { class: 'field-label' }, label), input);
}

function check(form, key, label, checked = false) {
  const input = el('input', { type: 'checkbox', checked });
  form[key] = () => input.checked || undefined;
  return el('label', { class: 'check', style: { paddingLeft: 0 } }, input, el('span', { class: 'v' }, label));
}

function select(form, key, label, options) {
  const node = el('select', { class: 'field', style: { width: '100%' } },
    ...options.map(([value, text_]) => el('option', { value }, text_)));
  form[key] = () => node.value;
  return el('div', { class: 'form-row' }, el('label', { class: 'field-label' }, label), node);
}

function columnPicker(form, key, label, { multiple = false, optional = false, optionalLabel = 'all' } = {}) {
  const columns = state.dataset?.columns || [];
  if (!multiple) {
    const node = el('select', { class: 'field', style: { width: '100%' } },
      ...columns.map((c) => el('option', { value: c.name }, `${c.name}  ·  ${c.type}`)));
    form[key] = () => node.value;
    return el('div', { class: 'form-row' }, el('label', { class: 'field-label' }, label), node);
  }
  const chosen = new Set();
  const list = el('div', {
    class: 'checklist',
    style: { border: '1px solid var(--border)', borderRadius: 'var(--r-md)', padding: '4px' },
  }, ...columns.map((column) => {
    const box = el('input', {
      type: 'checkbox',
      onchange: () => { box.checked ? chosen.add(column.name) : chosen.delete(column.name); },
    });
    return el('label', { class: 'check' }, box,
      el('span', { class: 'v' }, column.name),
      el('span', { class: 'n' }, kindLabel(column.kind)));
  }));
  form[key] = () => (chosen.size ? [...chosen] : undefined);
  return el('div', { class: 'form-row' },
    el('label', { class: 'field-label' }, label),
    optional ? el('div', { class: 'hint', style: { marginTop: 0, marginBottom: '5px' } },
      `Leave everything unticked for ${optionalLabel}.`) : null,
    list);
}

// =========================================================== EXPORT =========

const FORMATS = [
  ['xlsx', 'Excel workbook (.xlsx)', 'Split automatically so no file exceeds Excel’s row limit.'],
  ['csv', 'CSV (.csv)', 'Plain text, opens anywhere.'],
  ['tsv', 'Tab-separated (.tsv)', ''],
  ['parquet', 'Parquet (.parquet)', 'Compact and fast — about a fifth the size of CSV.'],
  ['json', 'JSON Lines (.jsonl)', 'One JSON object per row.'],
];

const EXCEL_MAX = 1_048_575;

export function mountExport() {
  const host = $('#export-form');
  on('dataset', () => draw());
  on('query', debounce(() => { if (state.view === 'export') draw(); }, 200));
  on('view', (name) => { if (name === 'export') draw(); });

  function draw() {
    if (!state.dataset) { host.replaceChildren(); return; }
    host.replaceChildren(buildExportForm());
  }
}

/** Shared by the Export view and the quick dialog. */
function buildExportForm({ onDone } = {}) {
  const dataset = state.dataset;
  const form = {};

  const format = el('select', { class: 'field', style: { width: '100%' } },
    ...FORMATS.map(([value, label]) => el('option', { value }, label)));
  const formatHint = el('div', { class: 'hint' });

  const directory = el('input', {
    class: 'field', style: { flex: 1 },
    value: dataset.source_path.replace(/[\\/][^\\/]+$/, ''),
  });
  const baseName = el('input', {
    class: 'field', style: { width: '100%' },
    value: dataset.display_name.replace(/\.[^.]+$/, ''),
  });
  const rowsPerFile = el('input', {
    class: 'field', type: 'number', value: EXCEL_MAX, min: 1, style: { width: '100%' },
  });
  const suffix = el('select', { class: 'field', style: { width: '100%' } },
    el('option', { value: 'alpha' }, 'name_a, name_b, name_c'),
    el('option', { value: 'numeric' }, 'name_1, name_2, name_3'),
    el('option', { value: 'part' }, 'name_part_1, name_part_2'),
    el('option', { value: 'padded' }, 'name_001, name_002'));

  const onlyFiltered = el('input', { type: 'checkbox', checked: true });
  const onlyVisible = el('input', { type: 'checkbox', checked: true });

  const planBox = el('div', { class: 'note', style: { marginTop: '16px' } });
  const splitRow = el('div', { class: 'form-grid' },
    el('div', {}, el('label', { class: 'field-label' }, 'Rows per file'), rowsPerFile,
      el('div', { class: 'hint' }, `Excel’s limit is ${count(EXCEL_MAX)}.`)),
    el('div', {}, el('label', { class: 'field-label' }, 'File naming'), suffix));

  const refreshPlan = debounce(async () => {
    try {
      const plan = await api.exportPlan(dataset.id, {
        format: format.value,
        directory: directory.value,
        base_name: baseName.value,
        rows_per_file: format.value === 'xlsx' ? Number(rowsPerFile.value) : Number(rowsPerFile.value),
        suffix_style: suffix.value,
        filters: onlyFiltered.checked ? activeFilters() : [],
        columns: onlyVisible.checked ? visibleColumnNames() : null,
      });
      const names = plan.filenames.slice(0, 4).join(', ')
        + (plan.filenames.length > 4 ? `, … (${plan.filenames.length} files)` : '');
      planBox.className = 'note';
      planBox.replaceChildren(icon('info', 15), el('div', {},
        el('div', {}, `${count(plan.row_count)} rows × ${plan.column_count} columns → `,
          el('b', {}, `${plan.part_count} file${plan.part_count === 1 ? '' : 's'}`)),
        el('div', { class: 'hint', style: { marginTop: '3px', fontFamily: 'var(--mono)' } }, names),
        ...(plan.warnings || []).map((w) => el('div', { class: 'hint' }, w))));
    } catch (error) {
      planBox.className = 'note note-bad';
      planBox.replaceChildren(icon('alert', 15), error.message);
    }
  }, 250);

  const syncFormat = () => {
    const isExcel = format.value === 'xlsx';
    splitRow.style.display = '';
    formatHint.textContent = FORMATS.find(([v]) => v === format.value)?.[2] || '';
    if (isExcel && Number(rowsPerFile.value) > EXCEL_MAX) rowsPerFile.value = EXCEL_MAX;
    refreshPlan();
  };
  format.addEventListener('change', syncFormat);
  for (const input of [directory, baseName, rowsPerFile, suffix, onlyFiltered, onlyVisible]) {
    input.addEventListener('input', refreshPlan);
    input.addEventListener('change', refreshPlan);
  }

  const goButton = el('button', { class: 'btn btn-primary' }, 'Export');
  goButton.addEventListener('click', async () => {
    goButton.disabled = true;
    try {
      const request = {
        format: format.value,
        directory: directory.value,
        base_name: baseName.value,
        rows_per_file: Number(rowsPerFile.value),
        suffix_style: suffix.value,
        filters: onlyFiltered.checked ? activeFilters() : [],
        sort: state.sort,
        columns: onlyVisible.checked ? visibleColumnNames() : null,
      };
      // Ask before replacing anything. The plan is fetched afresh rather than
      // trusted from the screen, which may predate the last keystroke; the
      // server refuses to replace files unless `overwrite` is set.
      const plan = await api.exportPlan(dataset.id, request);
      if (plan.existing?.length) {
        if (!(await confirmReplace(plan))) return;
        request.overwrite = true;
      }
      const result = await runJob(api.export(dataset.id, request), { title: 'Exporting…' });

      showResult(result);
      onDone?.();
    } catch (error) {
      if (!error.cancelled) toast('Export failed', { sub: error.message, kind: 'bad' });
    } finally {
      goButton.disabled = false;
    }
  });

  syncFormat();

  return el('div', {},
    el('div', { class: 'form-row' },
      el('label', { class: 'field-label' }, 'Format'), format, formatHint),
    el('div', { class: 'form-row' },
      el('label', { class: 'field-label' }, 'Save to folder'),
      el('div', { style: { display: 'flex', gap: '8px' } }, directory,
        el('button', {
          class: 'btn',
          onclick: () => pickFolder(directory.value).then((chosen) => {
            if (chosen) { directory.value = chosen; refreshPlan(); }
          }),
        }, 'Browse…'))),
    el('div', { class: 'form-row' },
      el('label', { class: 'field-label' }, 'File name'), baseName),
    splitRow,
    el('div', { class: 'form-row', style: { marginTop: '16px' } },
      el('label', { class: 'check', style: { paddingLeft: 0 } }, onlyFiltered,
        el('span', { class: 'v' }, 'Only the rows matching the current filter')),
      el('label', { class: 'check', style: { paddingLeft: 0 } }, onlyVisible,
        el('span', { class: 'v' }, 'Only the columns currently shown'))),
    planBox,
    el('div', { style: { display: 'flex', gap: '8px', marginTop: '20px' } }, goButton));
}

/** Ask before an export replaces files that are already in the folder. */
function confirmReplace(plan) {
  return new Promise((resolve) => {
    const names = plan.existing;
    const shown = names.slice(0, 5).join(', ')
      + (names.length > 5 ? `, … (${count(names.length)} files)` : '');
    const box = dialog({
      title: names.length === 1 ? 'Replace the existing file?' : `Replace ${count(names.length)} existing files?`,
      body: el('div', {},
        plan.replaces_source
          ? el('div', { class: 'note note-warn', style: { marginBottom: '12px' } }, icon('alert', 15),
            `${plan.replaces_source} is the file you opened. Replacing it overwrites your original with this export.`)
          : null,
        el('div', {}, 'Already in that folder: ', el('b', {}, shown))),
      footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
        el('span', { class: 'spacer' }),
        el('button', { class: 'btn', onclick: () => { resolve(false); box.close(); } }, 'Cancel'),
        el('button', { class: 'btn btn-primary', onclick: () => { resolve(true); box.close(); } },
          names.length === 1 ? 'Replace it' : 'Replace them')),
      onClose: () => resolve(false),
    });
  });
}

function showResult(result) {
  if (!result) return;
  const files = result.files || [];
  dialog({
    title: 'Export finished',
    body: el('div', {},
      el('div', { class: 'note note-good', style: { marginBottom: '16px' } }, icon('check', 15),
        el('div', {},
          el('div', {}, `${count(result.row_count)} rows written to ${files.length} file${files.length === 1 ? '' : 's'} in ${duration(result.seconds)}`),
          result.rows_per_second
            ? el('div', { class: 'hint' }, `${compact(result.rows_per_second)} rows per second`)
            : null)),
      ...(result.warnings || []).map((w) =>
        el('div', { class: 'note note-warn', style: { marginBottom: '8px' } }, icon('alert', 15), w)),
      el('div', { class: 'filelist' }, ...files.map((file) =>
        el('div', { class: 'filerow' },
          icon('file'),
          el('span', { class: 'nm', title: file.path }, file.name),
          el('span', { class: 'sz' }, `${count(file.rows)} rows · ${bytes(file.bytes)}`))))),
    footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
      el('button', {
        class: 'btn',
        onclick: () => api.reveal(result.directory).catch((e) =>
          toast('Could not open the folder', { sub: e.message, kind: 'bad' })),
      }, icon('open', 14), 'Open folder'),
      el('span', { class: 'spacer' })),
  });
}

async function pickFolder(startAt) {
  return new Promise((resolve) => {
    let current = startAt;
    const list = el('div', { class: 'filelist' });
    const crumb = el('div', { class: 'crumb' });

    const box = dialog({
      title: 'Choose a folder',
      body: el('div', {}, crumb, list),
      footer: el('div', { style: { display: 'flex', width: '100%', gap: '8px' } },
        el('span', { class: 'spacer' }),
        // Settle before closing: close() runs onClose, which resolves null, and
        // a promise keeps whichever value arrives first — so closing first
        // threw the chosen folder away.
        el('button', { class: 'btn', onclick: () => { resolve(null); box.close(); } }, 'Cancel'),
        el('button', {
          class: 'btn btn-primary',
          onclick: () => { resolve(current); box.close(); },
        }, 'Use this folder')),
      onClose: () => resolve(null),
    });

    const show = async (path) => {
      const data = await api.browse(path);
      current = data.path;
      crumb.textContent = data.path;
      const rows = [];
      if (data.parent) {
        rows.push(el('button', { class: 'filerow', onclick: () => show(data.parent) },
          icon('folder'), el('span', { class: 'nm' }, '..')));
      }
      for (const entry of data.entries.filter((e) => e.is_dir)) {
        rows.push(el('button', { class: 'filerow', onclick: () => show(entry.path) },
          icon('folder'), el('span', { class: 'nm' }, entry.name)));
      }
      list.replaceChildren(...rows);
    };
    show(startAt);
  });
}

/** Quick export from elsewhere in the app. */
export function openExportDialog({ pivot } = {}) {
  if (pivot) {
    // The pivot is already a small result set; write it straight out as a sheet.
    return exportPivotResult(pivot);
  }
  const box = dialog({
    title: 'Export',
    wide: true,
    body: buildExportForm({ onDone: () => box.close() }),
  });
}

async function exportPivotResult(result) {
  // Build a CSV client-side: a pivot is at most a few thousand cells.
  const header = [
    ...result.row_fields,
    ...result.columns.map((c) => (result.column_field ? `${c.label} — ${c.value_label}` : c.value_label)),
  ];
  const lines = [header];
  for (const row of result.rows) {
    lines.push([
      ...result.row_fields.map((_, i) => {
        const rolled = i >= result.row_fields.length - row.depth;
        return rolled ? (i === result.row_fields.length - row.depth ? 'Total' : '') : (row.keys[i] ?? '');
      }),
      ...row.cells.map((c) => (c === null || c === undefined ? '' : c)),
    ]);
  }
  if (result.grand_total) {
    lines.push([
      'Grand total', ...result.row_fields.slice(1).map(() => ''),
      ...result.grand_total.cells.map((c) => (c === null || c === undefined ? '' : c)),
    ]);
  }

  const csv = lines.map((row) => row.map((cell) => {
    const text_ = String(cell ?? '');
    return /[",\n]/.test(text_) ? `"${text_.replace(/"/g, '""')}"` : text_;
  }).join(',')).join('\r\n');

  // A BOM so Excel opens UTF-8 correctly — the original's client-side export
  // omitted it, which mangled every accented character and emoji.
  const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = el('a', { href: url, download: `${state.dataset.display_name.replace(/\.[^.]+$/, '')}-pivot.csv` });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  toast('Pivot exported', { sub: 'Saved to your Downloads folder.', kind: 'good' });
}

// ============================================================== SQL =========

export function mountSql() {
  const runButton = $('#sql-run');
  const textarea = $('#sql-text');
  const status = $('#sql-status');
  const wrap = $('#sql-wrap');

  const run = async () => {
    if (!state.dataset) return;
    runButton.disabled = true;
    status.textContent = 'Running…';
    try {
      const result = await api.sql(state.dataset.id, textarea.value, 2000);
      status.textContent = `${count(result.row_count)} rows in ${duration(result.seconds)}`
        + (result.truncated ? ' (truncated)' : '');
      if (!result.columns.length) {
        wrap.replaceChildren(el('div', { style: { padding: '20px' }, class: 'hint' }, 'No columns returned.'));
        return;
      }
      const table = el('table', { class: 'result' },
        el('thead', {}, el('tr', {},
          el('th', { class: 'rownum' }, '#'),
          ...result.columns.map((name) => el('th', { title: name }, name)))),
        el('tbody', {}, ...result.rows.map((row, index) =>
          el('tr', {},
            el('td', { class: 'rownum' }, count(index + 1)),
            ...row.map((value) => {
              const empty = value === null || value === undefined;
              const numeric = typeof value === 'number';
              return el('td', {
                class: [numeric ? 'num' : '', empty ? 'null' : ''].filter(Boolean).join(' '),
                // The grouped digits are what is readable; the tooltip keeps
                // the exact value for copying.
                title: empty ? '' : String(value),
              }, empty ? '—' : (numeric ? num(value) : String(value)));
            })))));
      wrap.replaceChildren(table);
    } catch (error) {
      status.textContent = '';
      wrap.replaceChildren(el('div', { style: { padding: '20px' } },
        el('div', { class: 'note note-bad' }, icon('alert', 15), error.message)));
    } finally {
      runButton.disabled = false;
    }
  };

  runButton.addEventListener('click', run);
  textarea.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      run();
    }
  });

  on('dataset', () => {
    wrap.replaceChildren(el('div', { style: { padding: '20px' }, class: 'hint' },
      'Your data is the table “data”. Results are read-only and capped at 2,000 rows.'));
    status.textContent = '';
  });
}
