// HTTP client.
//
// The per-run token travels in a custom header. That is deliberate: a custom
// header forces a CORS preflight, and the server sends no CORS headers, so a
// page on any other origin cannot drive this API even though it is on a
// predictable loopback port. The original had no such check.

// The token arrives once in the launch URL. It is then kept in sessionStorage
// so that refreshing the page does not break the app, and removed from the
// address bar so it is not in history or in a screenshot. sessionStorage is
// per-tab and cleared when the tab closes, which matches the token's lifetime.
const KEY = 'grande.token';

function readToken() {
  const fromUrl = new URLSearchParams(location.search).get('t');
  if (fromUrl) {
    try { sessionStorage.setItem(KEY, fromUrl); } catch { /* private mode */ }
    history.replaceState(null, '', location.pathname);
    return fromUrl;
  }
  try { return sessionStorage.getItem(KEY) || ''; } catch { return ''; }
}

const TOKEN = readToken();

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(path, { method = 'GET', body, signal } = {}) {
  let response;
  try {
    response = await fetch(path, {
      method,
      signal,
      headers: {
        'X-Grande-Token': TOKEN,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (cause) {
    if (cause.name === 'AbortError') throw cause;
    throw new ApiError('Lost contact with Grande. Is the window that started it still open?', 0);
  }

  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON error body */ }

  if (!response.ok) {
    throw new ApiError(data?.error || text || `Request failed (${response.status})`, response.status);
  }
  return data;
}

const get = (path, opts) => request(path, opts);
const post = (path, body, opts) => request(path, { method: 'POST', body, ...opts });

export const api = {
  hello: () => get('/api/hello'),
  browse: (path) => get('/api/browse?path=' + encodeURIComponent(path || '')),
  sniff: (path) => post('/api/sniff', { path }),
  open: (options) => post('/api/open', options),
  reveal: (path) => post('/api/reveal', { path }),

  dataset: (id) => get(`/api/dataset/${id}`),
  closeDataset: (id) => request(`/api/dataset/${id}`, { method: 'DELETE' }),
  rows: (id, options, opts) => post(`/api/dataset/${id}/rows`, options, opts),
  distinct: (id, options) => post(`/api/dataset/${id}/distinct`, options),
  summary: (id, options) => post(`/api/dataset/${id}/summary`, options),
  profile: (id, options) => post(`/api/dataset/${id}/profile`, options),
  describeFilters: (id, filters) => post(`/api/dataset/${id}/describe-filters`, { filters }),

  pivot: (id, options, opts) => post(`/api/dataset/${id}/pivot`, options, opts),
  drill: (id, options) => post(`/api/dataset/${id}/drill`, options),

  transform: (id, op, params) => post(`/api/dataset/${id}/transform`, { op, params }),
  transformPreview: (id, op, params, opts) =>
    post(`/api/dataset/${id}/transform/preview`, { op, params }, opts),
  undo: (id) => post(`/api/dataset/${id}/undo`, {}),

  exportPlan: (id, options) => post(`/api/dataset/${id}/export/plan`, options),
  export: (id, options) => post(`/api/dataset/${id}/export`, options),

  sql: (id, query, limit) => post(`/api/dataset/${id}/sql`, { query, limit }),

  job: (jobId) => get(`/api/job/${jobId}`),
  cancelJob: (jobId) => post(`/api/job/${jobId}/cancel`, {}),
};

/**
 * Follow a job to completion over Server-Sent Events.
 *
 * The original documented SSE and implemented it server-side but never used it,
 * animating a fake percentage instead. This reports what the server has actually
 * finished, and falls back to polling if the event stream cannot be established.
 */
export function followJob(jobId, { onProgress, onDone, onError } = {}) {
  let finished = false;
  let source = null;

  const finish = (job) => {
    if (finished) return;
    finished = true;
    source?.close();
    if (job?.status === 'error') onError?.(new ApiError(job.error || 'That job failed.', 500));
    else onDone?.(job);
  };

  try {
    source = new EventSource(`/api/job/${jobId}/stream?t=${encodeURIComponent(TOKEN)}`);
    source.onmessage = (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      if (data.final) return finish(data.job);
      onProgress?.(data);
    };
    source.onerror = () => {
      // The stream dropped; fall back to polling rather than stalling the UI.
      source?.close();
      if (!finished) poll();
    };
  } catch {
    poll();
  }

  async function poll() {
    while (!finished) {
      try {
        const job = await api.job(jobId);
        if (job.finished) return finish(job);
        onProgress?.({ percent: job.percent, message: job.message, ...job.detail });
      } catch (err) {
        return finished || onError?.(err);
      }
      await new Promise((r) => setTimeout(r, 600));
    }
  }

  return {
    cancel: async () => {
      try { await api.cancelJob(jobId); } catch { /* already gone */ }
    },
    stop: () => { finished = true; source?.close(); },
  };
}
