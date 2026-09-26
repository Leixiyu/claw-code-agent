"use strict";

window.refreshHarnessUsage = async function () {
  if (window.harnessCapabilities && !window.harnessCapabilities.http.includes('GET /api/usage')) return;
  const button = document.querySelector('#usage-refresh');
  if (button.disabled) return;
  button.disabled = true;
  const total = document.querySelector('#usage-total');
  const details = document.querySelector('#usage-details');
  const note = document.querySelector('#usage-note');
  try {
    const usage = await window.harnessReadResponse(await window.harnessFetch('/api/usage', { cache: 'no-store' }));
    total.textContent = usage.total_tokens.toLocaleString();
    details.textContent = `输入 ${usage.input_tokens.toLocaleString()} · 输出 ${usage.output_tokens.toLocaleString()}` +
      ` · 缓存读取 ${usage.cache_read_input_tokens.toLocaleString()} · 缓存写入 ${usage.cache_creation_input_tokens.toLocaleString()}`;
    const active = usage.active_requests || 0;
    const unresolved = usage.unresolved_requests ?? usage.pending_requests ?? 0;
    note.textContent = (usage.imported_tokens ? ` 历史补录 ${usage.imported_tokens.toLocaleString()} Token。` : '') +
      ` 进行中 ${active} 个；待核实 ${unresolved} 个。` +
      (active || unresolved ? ' 当前总量可能不完整，可查看明细。' : '') +
      (usage.history_import_skipped ? ` ${usage.history_import_skipped} 个旧会话暂未补录。` : '');
  } catch (error) {
    total.textContent = '—';
    details.textContent = '';
    note.textContent = error.message;
  } finally { button.disabled = false; }
};

window.initHarnessUsage = function () {
  const $ = selector => document.querySelector(selector);
  $('#usage-refresh').onclick = window.refreshHarnessUsage;
  const dialog = $('#usage-dialog');
  let offset = 0;
  let loading = false;
  const pageSize = 20;
  async function loadRequests() {
    if (loading) return;
    loading = true;
    $('#usage-prev').disabled = true;
    $('#usage-next').disabled = true;
    $('#usage-request-info').textContent = '正在查询…';
    $('#usage-request-rows').replaceChildren();
    const params = new URLSearchParams({limit: String(pageSize), offset: String(offset)});
    const status = $('#usage-status').value;
    const session = $('#usage-session-filter').value.trim();
    if (status) params.set('status', status);
    if (session) params.set('session_id', session);
    try {
      const page = await window.harnessReadResponse(await window.harnessFetch(`/api/usage/requests?${params}`, {cache: 'no-store'}));
      const labels = {in_progress: '进行中', confirmed: '用量已确认', unresolved: '待核实'};
      const purposes = {chat: '对话', compact: '压缩', legacy_import: '历史补录'};
      for (const item of page.items) {
        const row = document.createElement('tr');
        const values = [new Date(item.created_at * 1000).toLocaleString(),
          `${item.request_id}\nSession: ${item.session_id}`,
          `${item.model}\n${purposes[item.purpose] || item.purpose}`,
          `${item.total_tokens.toLocaleString()}\n输入 ${item.input_tokens} · 输出 ${item.output_tokens}\n缓存读 ${item.cache_read_input_tokens} · 写 ${item.cache_creation_input_tokens}`,
          `${labels[item.status] || item.status}\n${item.reason_message || ''}`];
        for (const value of values) {
          const cell = document.createElement('td');
          cell.textContent = value;
          row.appendChild(cell);
        }
        $('#usage-request-rows').appendChild(row);
      }
      $('#usage-request-info').textContent = page.total ? `共 ${page.total} 条，显示 ${page.offset + 1}–${page.offset + page.items.length} 条` : '没有符合条件的请求记录。';
      $('#usage-prev').disabled = offset === 0;
      $('#usage-next').disabled = !page.has_more;
    } catch (error) { $('#usage-request-info').textContent = error.message; }
    finally { loading = false; }
  }
  $('#usage-show-requests').onclick = () => { offset = 0; dialog.showModal(); loadRequests(); };
  $('#usage-close').onclick = () => dialog.close();
  $('#usage-filter-form').onsubmit = event => { event.preventDefault(); if (!loading) { offset = 0; loadRequests(); } };
  $('#usage-prev').onclick = () => { if (!loading) { offset = Math.max(0, offset - pageSize); loadRequests(); } };
  $('#usage-next').onclick = () => { if (!loading) { offset += pageSize; loadRequests(); } };
  // Modest polling keeps long calls distinguishable from inactive requests.
  const poll = window.setInterval(() => {
    if (dialog.open && !document.hidden) { loadRequests(); window.refreshHarnessUsage(); }
  }, 15000);
  window.addEventListener('pagehide', () => window.clearInterval(poll), {once: true});
  window.refreshHarnessUsage();
};
