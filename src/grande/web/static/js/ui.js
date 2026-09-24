// Small DOM and formatting helpers shared by every view.
// Everything that puts file-derived text on screen goes through textContent or
// `text()`, never innerHTML, so a column called <img onerror=...> is just a name.

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'html') node.innerHTML = value;           // only for our own markup
    else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** An <svg><use> reference into the sprite in index.html. */
export function icon(name, size) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'icon');
  if (size) { svg.style.width = size + 'px'; svg.style.height = size + 'px'; }
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ------------------------------------------------------------------ numbers

const nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function num(value) {
  if (value === null || value === undefined || value === '') return '';
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return Number.isInteger(n) ? nf0.format(n) : nf2.format(n);
}

export function count(n) {
  return nf0.format(Number(n) || 0);
}

/** Compact form for headline figures: 12,400,000 -> 12.4M */
export function compact(n) {
  const v = Number(n) || 0;
  const abs = Math.abs(v);
  if (abs >= 1e9) return (v / 1e9).toFixed(abs >= 1e10 ? 0 : 1) + 'B';
  if (abs >= 1e6) return (v / 1e6).toFixed(abs >= 1e7 ? 0 : 1) + 'M';
  if (abs >= 1e4) return (v / 1e3).toFixed(0) + 'K';
  return nf0.format(v);
}

export function bytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let i = -1, x = v;
  do { x /= 1024; i++; } while (x >= 1024 && i < units.length - 1);
  return `${x < 10 ? x.toFixed(1) : Math.round(x)} ${units[i]}`;
}

export function percent(x) {
  if (x === null || x === undefined) return '';
  const v = Number(x);
  if (!Number.isFinite(v)) return '';
  return (v * 100).toFixed(1) + '%';
}

export function duration(seconds) {
  const s = Number(seconds) || 0;
  if (s < 1) return Math.round(s * 1000) + ' ms';
  if (s < 60) return s.toFixed(1) + ' s';
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
}

// ------------------------------------------------------------------- toasts

export function toast(title, { sub = '', kind = '', timeout = 5200, action } = {}) {
  const host = $('#toasts');
  const iconName = kind === 'bad' ? 'alert' : kind === 'good' ? 'check' : 'info';
  const node = el('div', { class: `toast ${kind}` },
    icon(iconName),
    el('div', { class: 'body' },
      el('div', { class: 'title' }, title),
      sub ? el('div', { class: 'sub' }, sub) : null,
      action ? el('button', {
        class: 'btn btn-sm', style: { marginTop: '8px' },
        onclick: () => { action.run(); node.remove(); },
      }, action.label) : null,
    ),
    el('button', {
      class: 'btn-ghost btn-icon btn-sm', 'aria-label': 'Dismiss',
      onclick: () => node.remove(),
    }, icon('x', 13)),
  );
  host.append(node);
  if (timeout) setTimeout(() => node.remove(), timeout);
  return node;
}

// ------------------------------------------------------------------ dialogs

/** Open a modal. `build(close)` returns {title, body, footer}. */
export function dialog({ title, body, footer, wide = false, onClose } = {}) {
  const close = () => {
    scrim.remove();
    document.removeEventListener('keydown', onKey);
    onClose?.();
  };
  const onKey = (e) => { if (e.key === 'Escape') { e.stopPropagation(); close(); } };

  const panel = el('div', { class: `dialog ${wide ? 'wide' : ''}`, role: 'dialog', 'aria-modal': 'true' },
    el('div', { class: 'dialog-head' },
      el('h2', {}, title),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-ghost btn-icon', 'aria-label': 'Close', onclick: close }, icon('x', 15)),
    ),
    el('div', { class: 'dialog-body' }, body),
    footer ? el('div', { class: 'dialog-foot' }, footer) : null,
  );
  const scrim = el('div', {
    class: 'scrim',
    onclick: (e) => { if (e.target === scrim) close(); },
  }, panel);

  $('#layers').append(scrim);
  document.addEventListener('keydown', onKey);
  // Focus the first natural control so the keyboard works immediately.
  setTimeout(() => panel.querySelector('input, select, textarea, button:not([aria-label])')?.focus(), 30);
  return { close, panel };
}

// ----------------------------------------------------------------- popovers

let openPopover = null;

/** Anchor a floating panel to an element, flipping it to stay on screen. */
export function popover(anchor, content, { align = 'start', width } = {}) {
  closePopover();
  const node = el('div', { class: 'popover' }, content);
  if (width) node.style.width = width + 'px';
  $('#layers').append(node);

  const box = anchor.getBoundingClientRect();
  const size = node.getBoundingClientRect();
  const margin = 8;
  let left = align === 'end' ? box.right - size.width : box.left;
  left = Math.max(margin, Math.min(left, window.innerWidth - size.width - margin));
  let top = box.bottom + 4;
  if (top + size.height > window.innerHeight - margin) {
    top = Math.max(margin, box.top - size.height - 4);
  }
  node.style.left = left + 'px';
  node.style.top = top + 'px';

  const onDocDown = (e) => {
    if (!node.contains(e.target) && !anchor.contains(e.target)) closePopover();
  };
  const onKey = (e) => { if (e.key === 'Escape') closePopover(); };
  // Defer so the click that opened this does not immediately close it.
  setTimeout(() => {
    document.addEventListener('mousedown', onDocDown);
    document.addEventListener('keydown', onKey);
  }, 0);

  openPopover = {
    node,
    dispose: () => {
      node.remove();
      document.removeEventListener('mousedown', onDocDown);
      document.removeEventListener('keydown', onKey);
      anchor.removeAttribute?.('data-open');
    },
  };
  anchor.setAttribute?.('data-open', 'true');
  return openPopover;
}

export function closePopover() {
  openPopover?.dispose();
  openPopover = null;
}

// -------------------------------------------------------------------- misc

export function debounce(fn, wait = 220) {
  let timer;
  const wrapped = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export function kindLabel(kind) {
  return { number: '#', text: 'A', date: 'D', boolean: 'T/F' }[kind] || 'A';
}

/** A progress bar element with an `update(percent)` method. */
export function progressBar(indeterminate = false) {
  const fill = el('i');
  const bar = el('div', { class: `progress ${indeterminate ? 'indeterminate' : ''}` }, fill);
  bar.update = (pct) => {
    if (pct === null || pct === undefined) {
      bar.classList.add('indeterminate');
    } else {
      bar.classList.remove('indeterminate');
      fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
    }
  };
  bar.update(indeterminate ? null : 0);
  return bar;
}
