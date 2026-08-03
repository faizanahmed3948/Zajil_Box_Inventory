// ─────────────────────────────────────────────────────────────────────────
// api-client.js
//
// A drop-in replacement for the handful of Firebase (Firestore + Auth)
// functions this app used, backed by the Python (FastAPI) backend instead.
//
// It intentionally mirrors the same function names/shapes
// (initializeApp, getFirestore, collection, doc, onSnapshot, setDoc, ...)
// so none of the app's business logic below has to change - only the
// import line and the config object at the top of index.html changed.
// ─────────────────────────────────────────────────────────────────────────

const TOKEN_KEY = 'bt_api_token';

let apiBase = '';
let wsBase = '';

let token = localStorage.getItem(TOKEN_KEY) || null;
let currentUser = null;      // { uid, email, displayName }
let authListeners = [];
let authReady = false;
let authReadyWaiters = [];

let ws = null;
let wsShouldReconnect = false;
let wsReconnectDelay = 1000;
// channel -> Set of { kind: 'collection'|'doc', sort, cb }
const wsChannels = new Map();

function apiUrl(path) { return apiBase + path; }
function wsUrl() { return `${wsBase}/ws?token=${encodeURIComponent(token || '')}`; }

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(apiUrl(path), { ...opts, headers });
  let body = null;
  try { body = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const detail = body && body.detail;
    const err = new Error((detail && detail.message) || (typeof detail === 'string' ? detail : 'Request failed'));
    err.code = (detail && detail.code) || 'api/error';
    err.status = res.status;
    throw err;
  }
  return body;
}

function notifyAuthListeners() {
  authListeners.forEach(cb => cb(currentUser));
}

function setSession(newToken, user) {
  token = newToken;
  currentUser = user;
  if (newToken) localStorage.setItem(TOKEN_KEY, newToken);
  else localStorage.removeItem(TOKEN_KEY);
}

async function resolveInitialAuth() {
  if (token) {
    try {
      const me = await apiFetch('/api/auth/me');
      currentUser = { uid: me.uid, email: me.email, displayName: me.displayName || '' };
    } catch (_) {
      setSession(null, null);
    }
  }
  authReady = true;
  authReadyWaiters.forEach(fn => fn());
  authReadyWaiters = [];
  notifyAuthListeners();
  if (currentUser) connectWS();
}
const initialAuthPromise = resolveInitialAuth();

// ── WebSocket (real-time) ─────────────────────────────────────────────
function connectWS() {
  if (!token) return;
  wsShouldReconnect = true;
  try { if (ws) ws.close(); } catch (_) {}
  ws = new WebSocket(wsUrl());
  ws.onmessage = ev => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    const subs = wsChannels.get(msg.channel);
    if (!subs) return;
    subs.forEach(sub => sub.deliver(msg));
  };
  ws.onclose = () => {
    if (!wsShouldReconnect) return;
    setTimeout(connectWS, wsReconnectDelay);
    wsReconnectDelay = Math.min(wsReconnectDelay * 1.5, 15000);
  };
  ws.onopen = () => { wsReconnectDelay = 1000; };
}
function disconnectWS() {
  wsShouldReconnect = false;
  wsChannels.clear();
  try { if (ws) ws.close(); } catch (_) {}
  ws = null;
}

// ── App / Firestore / Auth handles (mostly markers - real state is module-level) ──
export function initializeApp(config) {
  apiBase = config.apiBase || '';
  wsBase = config.wsBase || apiBase.replace(/^http/, 'ws');
  return { apiBase, wsBase };
}
export function getFirestore(app) { return { __db: true }; }
export function getAuth(app) { return { __auth: true }; }

// ── Auth ───────────────────────────────────────────────────────────────
export function onAuthStateChanged(auth, cb) {
  authListeners.push(cb);
  if (authReady) queueMicrotask(() => cb(currentUser));
  else authReadyWaiters.push(() => cb(currentUser));
  return () => { authListeners = authListeners.filter(fn => fn !== cb); };
}

export async function signInWithEmailAndPassword(auth, email, password) {
  const res = await apiFetch('/api/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) });
  setSession(res.token, { uid: res.user.uid, email: res.user.email, displayName: res.user.displayName || '' });
  connectWS();
  notifyAuthListeners();
  return { user: currentUser };
}

export async function createUserWithEmailAndPassword(auth, email, password) {
  const res = await apiFetch('/api/auth/signup', { method: 'POST', body: JSON.stringify({ email, password }) });
  setSession(res.token, { uid: res.user.uid, email: res.user.email, displayName: res.user.displayName || '' });
  connectWS();
  notifyAuthListeners();
  return { user: currentUser };
}

export async function signOut(auth) {
  disconnectWS();
  setSession(null, null);
  notifyAuthListeners();
}

export async function updateProfile(user, { displayName }) {
  await apiFetch('/api/auth/me', { method: 'PATCH', body: JSON.stringify({ displayName }) });
  user.displayName = displayName;
  if (currentUser) currentUser.displayName = displayName;
}

// ── Firestore-like references ──────────────────────────────────────────
export function collection(db, name) { return { __type: 'collection', name }; }
export function doc(db, name, id) { return { __type: 'doc', name, id }; }
export function query(ref, ...clauses) {
  const orderClause = clauses.find(c => c && c.__type === 'orderBy');
  return { ...ref, _orderBy: orderClause || ref._orderBy };
}
export function orderBy(field, direction = 'asc') { return { __type: 'orderBy', field, direction }; }
export function serverTimestamp() { return Date.now(); }

function collectionPath(name) { return name === 'users' ? '/api/users' : `/api/${name}`; }
function docPath(name, id) { return name === 'users' ? `/api/users/${id}` : `/api/${name}/${id}`; }
function channelForDoc(name, id) { return `${name}/${id}`; }

function sortDocs(docs, orderClause) {
  if (!orderClause) return docs;
  const { field, direction } = orderClause;
  const sorted = [...docs].sort((a, b) => {
    const av = a[field], bv = b[field];
    if (av === bv) return 0;
    return av > bv ? 1 : -1;
  });
  if (direction === 'desc') sorted.reverse();
  return sorted;
}

function buildCollectionSnapshot(rawDocs, orderClause) {
  const docs = sortDocs(rawDocs, orderClause);
  return {
    forEach(cb) {
      docs.forEach(d => {
        const { id, ...rest } = d;
        cb({ id, data: () => rest });
      });
    },
  };
}

export async function addDoc(ref, data) {
  const res = await apiFetch(collectionPath(ref.name), { method: 'POST', body: JSON.stringify(data) });
  return { id: res.id };
}

export async function setDoc(ref, data, opts) {
  const method = opts && opts.merge ? 'PATCH' : 'PUT';
  await apiFetch(docPath(ref.name, ref.id), { method, body: JSON.stringify(data) });
}

export async function updateDoc(ref, data) {
  await apiFetch(docPath(ref.name, ref.id), { method: 'PATCH', body: JSON.stringify(data) });
}

export async function deleteDoc(ref) {
  await apiFetch(docPath(ref.name, ref.id), { method: 'DELETE' });
}

export async function getDoc(ref) {
  try {
    const data = await apiFetch(docPath(ref.name, ref.id));
    const { id, ...rest } = data;
    return { id: ref.id, exists: () => true, data: () => rest };
  } catch (e) {
    if (e.status === 404) return { id: ref.id, exists: () => false, data: () => undefined };
    throw e;
  }
}

export function onSnapshot(ref, onNext, onError) {
  if (ref.__type === 'doc') {
    const channel = channelForDoc(ref.name, ref.id);
    const sub = {
      deliver(msg) {
        onNext({ exists: () => !!msg.exists, data: () => msg.data || undefined, id: ref.id });
      },
    };
    if (!wsChannels.has(channel)) wsChannels.set(channel, new Set());
    wsChannels.get(channel).add(sub);

    getDoc(ref).then(onNext).catch(err => onError && onError(err));

    return () => { const s = wsChannels.get(channel); if (s) s.delete(sub); };
  }

  // collection (optionally wrapped by query()/orderBy())
  const channel = ref.name;
  const orderClause = ref._orderBy;
  const sub = {
    deliver(msg) { onNext(buildCollectionSnapshot(msg.docs || [], orderClause)); },
  };
  if (!wsChannels.has(channel)) wsChannels.set(channel, new Set());
  wsChannels.get(channel).add(sub);

  apiFetch(collectionPath(ref.name))
    .then(docs => onNext(buildCollectionSnapshot(docs, orderClause)))
    .catch(err => onError && onError(err));

  return () => { const s = wsChannels.get(channel); if (s) s.delete(sub); };
}
