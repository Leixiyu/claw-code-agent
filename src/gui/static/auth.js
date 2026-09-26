"use strict";

// API metadata describes each control; the server's two sets remain the policy.
window.harnessApplyVisibility = function () {
  const capabilities = window.harnessCapabilities;
  if (!capabilities) return;
  document.querySelectorAll('[data-api]').forEach(element => {
    element.hidden = !element.dataset.api.split('|').every(api => capabilities.http.includes(api));
  });
  document.querySelectorAll('[data-skill-list]').forEach(element => {
    element.hidden = !capabilities.skills.length || !capabilities.http.includes('GET /api/skills');
  });
};

window.requireHarnessLogin = async function () {
  const panel = document.querySelector('#login-panel');
  // Rendered session/task cards must obey the same capabilities as static controls.
  const observer = new MutationObserver(() => window.harnessApplyVisibility());
  observer.observe(document.querySelector('#app'), {childList: true, subtree: true});
  window.addEventListener('pagehide', () => observer.disconnect(), {once: true});
  let initialized = false;
  let resolveLogin;
  const loggedIn = new Promise(resolve => { resolveLogin = resolve; });
  const showUser = async (user) => {
    const capabilities = await window.harnessReadResponse(await window.harnessFetch('/api/capabilities'));
    window.harnessCapabilities = capabilities;
    document.querySelector('#signed-in-user').textContent = user.username;
    window.harnessApplyVisibility();
    panel.hidden = true;
    document.querySelector('#app').hidden = false;
  };
  window.addEventListener('harness-auth-expired', () => {
    document.querySelector('#usage-dialog')?.close();
    document.querySelector('#usage-request-rows')?.replaceChildren();
    panel.hidden = false;
    document.querySelector('#app').hidden = true;
    document.querySelector('#login-error').textContent = '登录已失效，请重新登录。';
  });
  document.querySelector('#login-form').onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const payload = await window.harnessReadResponse(await window.harnessFetch('/api/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: form.username.value, password: form.password.value }),
      }));
      form.password.value = '';
      // Re-authentication may switch users; never leave the previous user's UI visible.
      if (initialized) { window.location.reload(); return; }
      await showUser(payload.user);
      initialized = true;
      resolveLogin();
    } catch (error) { document.querySelector('#login-error').textContent = error.message; }
    finally { button.disabled = false; }
  };
  document.querySelector('#logout-user').onclick = async () => {
    try {
      await window.harnessReadResponse(await window.harnessFetch('/api/auth/logout', { method: 'POST' }));
      window.location.reload();
    } catch (error) { document.querySelector('#status-text').textContent = error.message; }
  };
  try {
    const response = await window.harnessFetch('/api/auth/me');
    if (response.ok) {
      await showUser(await response.json());
      initialized = true;
      return;
    }
    if (response.status !== 401) await window.harnessReadResponse(response);
  } catch (error) { document.querySelector('#login-error').textContent = error.message; }
  panel.hidden = false;
  document.querySelector('#app').hidden = true;
  return loggedIn;
};
