// Shared application state.
//
// Kept in its own module so the views can all reach it without importing each
// other, which would create cycles.

import { api, followJob } from './api.js';
import { toast, progressBar, el, icon, $ } from './ui.js';

export const state = {
  dataset: null,
  filters: [],        // structural filters from column menus
  search: '',         // the toolbar search box
  sort: [],
  hidden: new Set(),  // column names hidden from the grid and exports
  view: 'data',
  capabilities: {},
  job: null,
};

const listeners = new Map();

export function on(event, handler) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(handler);
  return () => listeners.get(event).delete(handler);
}

export function emit(event, payload) {
  for (const handler of listeners.get(event) || []) {
    try { handler(payload); } catch (error) { console.error(error); }
  }
}

// ------------------------------------------------------------------ derived

/** Every filter the server should apply, including the search box. */
export function activeFilters() {
  const all = [...state.filters];
  if (state.search.trim()) {
    all.push({
      op: 'any_contains',
      value: state.search.trim(),
      columns: state.dataset ? state.dataset.columns.map((c) => c.name) : [],
    });
  }
  return all;
}

export function visibleColumns() {
  if (!state.dataset) return [];
  return state.dataset.columns.filter((c) => !state.hidden.has(c.name));
}

export function visibleColumnNames() {
  return visibleColumns().map((c) => c.name);
}

export function columnByName(name) {
  return state.dataset?.columns.find((c) => c.name === name) || null;
}

export function isFiltered() {
  return state.filters.length > 0 || Boolean(state.search.trim());
}

// ------------------------------------------------------------------ actions

export function setDataset(dataset) {
  state.dataset = dataset;
  state.filters = [];
  state.search = '';
  state.sort = [];
  state.hidden = new Set();
  emit('dataset', dataset);
  emit('query');
}

export function patchDataset(dataset) {
  // After a transform: the table changed but the user's view intent stands.
  state.dataset = dataset;
  const names = new Set(dataset.columns.map((c) => c.name));
  state.filters = state.filters.filter((f) => !f.column || names.has(f.column));
  state.sort = state.sort.filter((s) => names.has(s.column));
  for (const name of [...state.hidden]) if (!names.has(name)) state.hidden.delete(name);
  emit('dataset', dataset);
  emit('query');
}

export function setFilters(filters) {
  state.filters = filters;
  emit('query');
}

export function addFilter(filter) {
  state.filters = [...state.filters.filter((f) => f.column !== filter.column), filter];
  emit('query');
}

export function removeFilter(index) {
  state.filters = state.filters.filter((_, i) => i !== index);
  emit('query');
}

export function clearFilters() {
  state.filters = [];
  state.search = '';
  emit('query');
}

export function setSearch(text) {
  state.search = text;
  emit('query');
}

export function toggleSort(columnName, additive = false) {
  const existing = state.sort.find((s) => s.column === columnName);
  let next;
  if (!existing) next = { column: columnName, direction: 'asc' };
  else if (existing.direction === 'asc') next = { column: columnName, direction: 'desc' };
  else next = null;                        // third click clears, like a spreadsheet

  if (additive) {
    state.sort = state.sort.filter((s) => s.column !== columnName);
    if (next) state.sort.push(next);
  } else {
    state.sort = next ? [next] : [];
  }
  emit('query');
}

export function setHidden(hidden) {
  state.hidden = hidden;
  emit('columns');
  emit('query');
}

// --------------------------------------------------------------------- jobs

/**
 * Run a server job, showing a progress strip in the top bar with a working
 * Cancel. Resolves with the finished job's result.
 */
export function runJob(startPromise, { title = 'Working', onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const slot = $('#job-slot');
    const bar = progressBar(true);
    const message = el('span', { class: 'msg' }, title);
    let follower = null;

    const strip = el('div', { class: 'job' },
      bar,
      message,
      el('button', {
        class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Cancel',
        title: 'Cancel',
        onclick: () => { message.textContent = 'Cancelling…'; follower?.cancel(); },
      }, icon('x', 13)),
    );
    slot.replaceChildren(strip);

    const done = () => { strip.remove(); state.job = null; };

    startPromise.then((job) => {
      state.job = job;
      follower = followJob(job.id, {
        onProgress: (event) => {
          if (typeof event.percent === 'number' && event.percent > 0) bar.update(event.percent);
          if (event.message) message.textContent = event.message;
          onProgress?.(event);
        },
        onDone: (finished) => {
          done();
          if (finished?.status === 'cancelled') {
            toast('Cancelled', { sub: title });
            reject(Object.assign(new Error('cancelled'), { cancelled: true }));
          } else {
            resolve(finished?.result);
          }
        },
        onError: (error) => { done(); reject(error); },
      });
    }).catch((error) => { done(); reject(error); });
  });
}

export { api };
