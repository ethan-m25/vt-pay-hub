/**
 * phub-auth.js — PayHub cross-platform auth client
 * Include in any hub. Set PHUB_HUB_ID before loading.
 *
 * Usage:
 *   <script>window.PHUB_HUB_ID = 'ny';</script>
 *   <script src="/phub-auth.js"></script>
 *
 * Exposes:
 *   phub.user          — { id, email, name, avatar } or null
 *   phub.signIn()      — redirect to auth.payhub.fyi
 *   phub.signOut()     — clear token + refresh UI
 *   phub.getToken()    — current access token (string) or null
 *   phub.onReady(fn)   — call fn when auth state resolved
 */
(function () {
  'use strict';

  const AUTH_BASE = 'https://auth.payhub.fyi';
  const HUB_ID = window.PHUB_HUB_ID || '';
  const TOKEN_KEY = 'phub_access_token';
  const REFRESH_KEY = 'phub_refresh_token';
  const USER_KEY = 'phub_user';
  const SSO_SEEDED_KEY = 'phub_sso_seeded';

  let _user = null;
  let _ready = false;
  const _callbacks = [];

  function fire() {
    _ready = true;
    _callbacks.forEach(fn => fn(_user));
  }

  function load(key) {
    try { return localStorage.getItem(key); } catch { return null; }
  }

  function save(key, val) {
    try { localStorage.setItem(key, val); } catch {}
  }

  function clear() {
    try {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(REFRESH_KEY);
      localStorage.removeItem(USER_KEY);
      localStorage.removeItem(SSO_SEEDED_KEY);
    } catch {}
  }

  async function exchangeHandoff(token) {
    try {
      const r = await fetch(`${AUTH_BASE}/api/auth/handoff/exchange`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token }),
      });
      if (!r.ok) return null;
      return r.json();
    } catch { return null; }
  }

  async function refreshAccessToken() {
    const rt = load(REFRESH_KEY);
    if (!rt) return null;
    try {
      const r = await fetch(`${AUTH_BASE}/api/auth/token/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: rt }),
      });
      if (!r.ok) return null;
      const d = await r.json();
      if (d.ok && d.access_token) {
        save(TOKEN_KEY, d.access_token);
        return d.access_token;
      }
    } catch {}
    return null;
  }

  function parseJwtPayload(token) {
    try {
      const b64 = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      return JSON.parse(atob(b64));
    } catch { return null; }
  }

  function isExpired(token) {
    const p = parseJwtPayload(token);
    if (!p || !p.exp) return true;
    return p.exp < Math.floor(Date.now() / 1000) + 60; // 60s buffer
  }

  async function resolveToken() {
    let at = load(TOKEN_KEY);
    if (at && !isExpired(at)) return at;
    at = await refreshAccessToken();
    return at;
  }

  function seedSsoCookie() {
    if (load(SSO_SEEDED_KEY)) return;
    const rt = load(REFRESH_KEY);
    if (!rt) return;
    fetch(`${AUTH_BASE}/api/auth/token/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt }),
    }).then(r => r.ok ? r.json() : null).then(d => {
      if (d && d.ok) {
        save(TOKEN_KEY, d.access_token);
        if (d.refresh_token) save(REFRESH_KEY, d.refresh_token);
        save(SSO_SEEDED_KEY, '1');
      }
    }).catch(() => {});
  }

  async function trySessionCookie() {
    try {
      const r = await fetch(`${AUTH_BASE}/api/auth/session`, {
        credentials: 'include',
      });
      if (!r.ok) return null;
      const d = await r.json();
      if (d.ok && d.access_token) {
        save(TOKEN_KEY, d.access_token);
        save(REFRESH_KEY, d.refresh_token);
        if (d.user) save(USER_KEY, JSON.stringify(d.user));
        return d;
      }
    } catch {}
    return null;
  }

  async function init() {
    // 1. Check for incoming handoff token in URL
    const url = new URL(location.href);
    const handoffToken = url.searchParams.get('phub_token');
    if (handoffToken) {
      url.searchParams.delete('phub_token');
      history.replaceState(null, '', url.toString());

      const result = await exchangeHandoff(handoffToken);
      if (result && result.ok) {
        save(TOKEN_KEY, result.access_token);
        save(REFRESH_KEY, result.refresh_token);
        save(USER_KEY, JSON.stringify(result.user));
        _user = result.user;
        renderNav();
        fire();
        return;
      }
    }

    // 2. Try existing localStorage token
    let at = await resolveToken();

    // 3. Try shared SSO cookie (cross-hub auto-login)
    if (!at) {
      const session = await trySessionCookie();
      if (session) {
        _user = session.user;
        renderNav();
        fire();
        return;
      }
    }

    if (at) {
      seedSsoCookie(); // fire-and-forget: plant SSO cookie for cross-hub auto-login
      const cached = load(USER_KEY);
      if (cached) {
        try { _user = JSON.parse(cached); } catch {}
      }
      if (!_user) {
        try {
          const r = await fetch(`${AUTH_BASE}/api/auth/me?hub=${HUB_ID}`, {
            headers: { Authorization: `Bearer ${at}` },
          });
          if (r.ok) {
            const d = await r.json();
            _user = d.user || null;
            if (_user) save(USER_KEY, JSON.stringify(_user));
          }
        } catch {}
      }
    }

    renderNav();
    fire();
  }

  function signIn() {
    const returnTo = encodeURIComponent(location.href);
    location.href = `${AUTH_BASE}/signin?from=${encodeURIComponent(HUB_ID)}&return_to=${returnTo}`;
  }

  function signOut() {
    clear();
    _user = null;
    renderNav();
    _callbacks.forEach(fn => fn(null));
  }

  async function getToken() {
    return resolveToken();
  }

  // ── Nav rendering ──────────────────────────────────────────────────
  function renderNav() {
    const btn = document.getElementById('phubSignInBtn');
    const info = document.getElementById('phubUserInfo');
    if (!btn && !info) return;

    if (_user) {
      if (btn) btn.style.display = 'none';
      if (info) {
        info.style.display = '';
        info.innerHTML = `
          <span class="phub-user-name" title="${_user.email}">${_user.name || _user.email}</span>
          <button class="phub-signout-btn" onclick="phub.signOut()">Sign out</button>
        `;
      }
    } else {
      if (btn) btn.style.display = '';
      if (info) info.style.display = 'none';
    }
  }

  // ── Public API ─────────────────────────────────────────────────────
  window.phub = {
    get user() { return _user; },
    signIn,
    signOut,
    getToken,
    onReady(fn) {
      if (_ready) fn(_user);
      else _callbacks.push(fn);
    },
  };

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
