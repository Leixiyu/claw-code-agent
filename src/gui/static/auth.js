"use strict";

window.requireHarnessLogin = async function () {
  const panel = document.querySelector('#login-panel');
  const showUser = (user) => {
    document.querySelector('#signed-in-user').textContent = user.username;
    panel.hidden = true;
    document.querySelector('#app').hidden = false;
    // Original developer/admin panels are not end-user prototype features.
    document.querySelector('#settings-form').closest('.sidebar-section').hidden = true;
    document.querySelectorAll('.view-tab').forEach(tab => {
      if (tab.dataset.view !== 'chat') tab.hidden = true;
    });
  };
  document.querySelector('#logout-user').onclick = async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    window.location.reload();
  };
  const response = await fetch('/api/auth/me');
  if (response.ok) { showUser(await response.json()); return; }
  panel.hidden = false;
  document.querySelector('#app').hidden = true;
  return new Promise(resolve => {
    document.querySelector('#login-form').onsubmit = async (event) => {
      event.preventDefault();
      const form = event.target;
      const button = form.querySelector('button');
      button.disabled = true;
      try {
        const response = await fetch('/api/auth/login', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ username: form.username.value, password: form.password.value }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || 'Login failed');
        form.password.value = '';
        showUser(payload.user);
        resolve();
      } catch (error) {
        document.querySelector('#login-error').textContent = error.message;
      } finally { button.disabled = false; }
    };
  });
};
