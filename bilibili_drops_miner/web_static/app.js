const $ = (id) => document.getElementById(id);
let current = null, groupVersion = null, qrTimer = null, qrEpoch = 0;
let roomPreview = null;
let progressVersion;
let accountValid = false, accountPending = false;
const dirty = new Set();
let syncTimer = null, syncQueue = Promise.resolve();
async function refreshAccount() {
  if (accountPending) return;
  accountPending = true;
  try {
    const data = await api('account');
    accountValid = data.status === 'valid';
    $('account').textContent = accountValid ? (data.name || '已登录') : ({empty: '未登录', invalid: 'Cookie 已失效，请重新登录', error: '校验失败，请检查网络', checking: '正在校验账号'})[data.status];
    $('account').dataset.connected = String(accountValid);
    controls();
  } finally { accountPending = false; }
}
function syncInputs() {
  clearTimeout(syncTimer);
  const operation = syncQueue.catch(() => {}).then(async () => {
    for (const kind of ['settings', 'cookie', 'task-ids', 'selection']) {
      if (!dirty.has(kind)) continue;
      let body;
      if (kind === 'settings') {
        if (!$('settings').reportValidity()) throw new Error('请检查直播间参数');
        const rooms = $('rooms').value.trim().split(/[,，\s]+/).filter(Boolean);
        if (rooms.some(room => !/^\d+$/.test(room))) throw new Error('请检查直播间参数');
        body = {room_ids: rooms.map(Number), thread_count: Number($('threads').value), reconnect_delay_seconds: Number($('reconnect').value), task_query_interval_seconds: Number($('interval').value)};
      } else if (kind === 'cookie') {
        if (!$('cookie').value.trim()) { dirty.delete(kind); continue; }
        body = {cookie: $('cookie').value};
      } else if (kind === 'task-ids') body = {task_ids: $('task-ids').value};
      else body = {groups: [...$('groups').querySelectorAll('input:checked')].map(input => Number(input.value)), generation: groupVersion};
      dirty.delete(kind);
      try {
        const data = await api(kind, body);
        if (kind === 'settings') { renderGroups(data); renderRoom(data.settings.room_ids); }
        if (kind === 'cookie') {
          accountValid = false;
          await refreshAccount();
        }
      } catch (error) { dirty.add(kind); throw error; }
    }
  });
  syncQueue = operation;
  return operation;
}
function scheduleSync(kind) {
  dirty.add(kind);
  clearTimeout(syncTimer);
  syncTimer = setTimeout(() => syncInputs().then(() => message('')).catch(error => message(error.message, true)), 650);
}
const pending = new Set();
let messageTimer = null;
function message(text, error = false, untilPhase = '', duration = 5000) {
  clearTimeout(messageTimer);
  $('message').textContent = text;
  $('message').className = error ? 'error' : '';
  $('message').dataset.untilPhase = untilPhase;
  if (text && !error && !untilPhase && duration > 0) {
    messageTimer = setTimeout(() => message(''), duration);
  }
}
async function api(path, body) {
  const response = await fetch('/api/' + path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-Web-Request': '1'}, body: JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求失败，请重试');
  return data;
}
function fillSettings(s) {
  $('rooms').value = s.room_ids.join(', '); $('threads').value = s.thread_count;
  $('reconnect').value = s.reconnect_delay_seconds; $('interval').value = s.task_query_interval_seconds;
}
function renderGroups(data) {
  if (groupVersion === data.generation) return;
  groupVersion = data.generation; $('groups').replaceChildren();
  if (!data.groups.length) {
    $('groups').className = 'empty';
    $('groups').textContent = '暂无任务分组'; return;
  }
  $('groups').className = '';
  data.groups.forEach((group, i) => {
    const label = document.createElement('label'); label.className = 'group';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = i; checkbox.checked = data.selected.includes(i);
    const text = document.createElement('span'); text.textContent = `${group.label || '任务组'} · 房间 ${group.room_id}`;
    const detail = document.createElement('small'); detail.textContent = (group.task_ids || []).join(' / '); text.append(detail);
    label.append(checkbox, text); $('groups').append(label);
  });
}
function controls() {
  if (!current) return;
  const operationPending = [...pending].some(button => button.id !== 'stop');
  const busy = current.phase !== 'stopped' || operationPending;
  const taskBusy = current.phase === 'stopping' || operationPending;
  for (const id of ['qr-button', 'manual-mode', 'logout']) $(id).disabled = busy || pending.has($(id));
  for (const input of $('settings').elements) input.disabled = busy;
  for (const input of $('cookie-form').elements) input.disabled = busy;
  for (const input of $('task-ids-form').elements) input.disabled = taskBusy;
  for (const input of $('groups').querySelectorAll('input')) input.disabled = taskBusy;
  for (const id of ['discover', 'progress', 'claim']) $(id).disabled = taskBusy || pending.has($(id));
  $('start').disabled = busy || !accountValid || pending.has($('start'));
  $('stop').disabled = current.phase === 'stopped' || pending.has($('stop'));
}
function renderRoom(rooms) {
  const target = $('room-count');
  if (rooms.length !== 1) {
    roomPreview = null;
    target.textContent = rooms.length ? `${rooms.length} 个房间` : '未配置';
    target.title = rooms.join(', ');
    return;
  }
  const id = rooms[0];
  if (!roomPreview || roomPreview.id !== id) roomPreview = {id, title: '', pending: false, expires: 0};
  const preview = roomPreview;
  target.textContent = preview.title || `房间 ${id}`;
  target.title = preview.title ? `${preview.title}（${id}）` : `房间 ${id}`;
  if (preview.pending || preview.expires > Date.now()) return;
  preview.pending = true;
  api(`room/${id}`).then(data => {
    preview.title = data.title || '';
    preview.expires = Date.now() + (preview.title ? 300000 : 30000);
    if (roomPreview === preview) {
      target.textContent = preview.title || `房间 ${id}`;
      target.title = preview.title ? `${preview.title}（${id}）` : `房间 ${id}`;
    }
  }).catch(() => { preview.expires = Date.now() + 30000; })
    .finally(() => { preview.pending = false; });
}
async function refresh(initial = false) {
  const data = await api('state'); current = data;
  if (initial) {
    fillSettings(data.settings);
    $('task-ids').value = (data.manual_task_ids || []).join(', ');
  }
  refreshAccount().catch(() => { accountValid = false; $('account').textContent = '账号校验失败，请重试'; controls(); });
  $('phase').textContent = ({stopped: '已停止', starting: '正在启动', running: '运行中', stopping: '正在停止'})[data.phase];
  $('phase').dataset.phase = data.phase;
  if ($('message').dataset.untilPhase === data.phase || ($('message').dataset.untilPhase === 'running' && data.phase === 'stopped')) message('');
  renderRoom(data.settings.room_ids);
  $('session-count').textContent = `${data.active_sessions ?? 0}/${data.planned_sessions ?? 0}`;
  const logs = $('logs');
  const followLogs = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 24;
  const logText = data.logs.join('\n') || '暂无记录';
  if (logs.textContent !== logText) {
    logs.textContent = logText;
    if (followLogs) logs.scrollTop = logs.scrollHeight;
  }
  if (progressVersion !== data.progress_version) {
    progressVersion = data.progress_version;
    renderTasks(data.task_progress || [], false);
  }
  renderGroups(data); controls();
}
async function action(button, operation) {
  pending.add(button); button.disabled = true; button.setAttribute('aria-busy', 'true'); controls();
  try { if (button.id !== 'stop' && button.id !== 'logout') await syncInputs(); await operation(); await refresh(); } catch (error) { message(error.message, true); }
  finally { pending.delete(button); button.disabled = false; button.removeAttribute('aria-busy'); controls(); }
}
for (const [id, kind] of [['settings', 'settings'], ['cookie-form', 'cookie'], ['task-ids-form', 'task-ids']]) {
  $(id).addEventListener('submit', event => { event.preventDefault(); syncInputs().catch(error => message(error.message, true)); });
  $(id).addEventListener('input', () => scheduleSync(kind));
  $(id).addEventListener('change', () => scheduleSync(kind));
}
$('groups').addEventListener('change', () => scheduleSync('selection'));
function cancelQr() {
  qrEpoch++; clearTimeout(qrTimer); $('qr-panel').hidden = true; $('qr-image').removeAttribute('src');
  $('cookie-form').hidden = false;
  $('manual-mode').setAttribute('aria-pressed', 'true'); $('qr-button').setAttribute('aria-pressed', 'false');
}
$('manual-mode').onclick = cancelQr;
$('qr-button').onclick = () => action($('qr-button'), async () => {
  cancelQr(); const epoch = qrEpoch; const qr = await api('qr', {});
  if (epoch !== qrEpoch) return;
  $('qr-image').src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(qr.svg);
  $('qr-panel').hidden = false; $('cookie-form').hidden = true; $('qr-status').textContent = '等待扫码';
  $('manual-mode').setAttribute('aria-pressed', 'false'); $('qr-button').setAttribute('aria-pressed', 'true');
  let failures = 0;
  async function poll() {
    if (epoch !== qrEpoch) return;
    try {
      const result = await api(`qr/${qr.id}/poll`, {});
      if (epoch !== qrEpoch) return;
      failures = 0;
      const labels = {UNSCANNED: '等待扫码', CONFIRMED_PENDING: '已扫码，请在手机上确认', EXPIRED: '二维码已过期，请重新生成', SUCCESS: '登录成功'};
      $('qr-status').textContent = labels[result.status] || '请重新生成二维码';
      if (result.status === 'SUCCESS') { $('cookie').value = ''; cancelQr(); message('扫码登录成功'); await refresh(); return; }
      if (result.status === 'EXPIRED') return;
    } catch (error) { if (epoch !== qrEpoch) return; $('qr-status').textContent = error.message; if (++failures >= 3) return; }
    qrTimer = setTimeout(poll, 2500);
  }
  qrTimer = setTimeout(poll, 2500);
});
$('logout').onclick = () => action($('logout'), async () => { clearTimeout(syncTimer); await syncQueue.catch(() => {}); dirty.delete('cookie'); await api('logout', {}); showClaimResults(false); $('claim-toggle').hidden = true; $('claim-results').replaceChildren(); accountValid = false; $('cookie').value = ''; cancelQr(); message('已清除服务端登录凭据'); });
$('discover').onclick = () => action($('discover'), async () => { message('正在获取静态 HTML…', false, '', 0); const data = await api('discover', {}); groupVersion = null; renderGroups(data); message(data.groups.length ? '解析完成，勾选任务分组即可自动应用' : '静态 HTML 中没有可解析的任务；可稍后重试或仅运行观看挂机'); });
for (const operation of ['start', 'stop']) $(operation).onclick = () => action($(operation), async () => { await api(operation, {}); message(operation === 'start' ? '正在使用当前配置启动挂机' : '已请求停止，正在释放连接', false, operation === 'stop' ? 'stopped' : 'running'); });
function showClaimResults(open) {
  $('claim-bubble').hidden = !open;
  $('claim-toggle').setAttribute('aria-expanded', String(open));
}
$('claim-toggle').onclick = () => showClaimResults($('claim-bubble').hidden);
$('claim-close').onclick = () => {
  showClaimResults(false);
  $('claim-toggle').focus({preventScroll: true});
};
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && !$('claim-bubble').hidden && !$('activity-section').open) {
    showClaimResults(false);
    $('claim-toggle').focus({preventScroll: true});
  }
});
function renderTasks(items, claim) {
  const target = $(claim ? 'claim-results' : 'tasks');
  target.replaceChildren();
  if (claim) {
    const succeeded = items.filter(item => item.success).length;
    const skipped = items.filter(item => !item.success && item.skipped).length;
    $('claim-count').textContent = items.length;
    $('claim-summary').textContent = `成功 ${succeeded} · 跳过 ${skipped} · 未领取 ${items.length - succeeded - skipped}`;
    $('claim-toggle').hidden = false;
    showClaimResults(true);
    target.scrollTop = 0;
  }
  if (!items.length) { target.textContent = claim ? '暂无领取结果' : ''; return; }
  for (const item of items) {
    const row = document.createElement('div'); row.className = 'task';
    const title = document.createElement('strong'); title.textContent = item.task_name || item.task_id; row.append(title);
    if (claim) {
      row.dataset.result = item.success ? 'success' : item.skipped ? 'skipped' : 'failed';
      if (item.award_name || item.reward_name) {
        const reward = document.createElement('small'); reward.textContent = item.award_name || item.reward_name; row.append(reward);
      }
      const p = document.createElement('p'); p.textContent = `${item.success ? '领取成功' : item.skipped ? '已跳过' : '未领取'} · ${item.message || ''}`; row.append(p);
    }
    else {
      const checkpoints = item.check_points && item.check_points.length ? item.check_points : [item];
      for (const point of checkpoints) {
        const p = document.createElement('p'); p.textContent = `${point.alias || '进度'}：${point.cur_value} / ${point.limit_value}${point.award_name ? ' · ' + point.award_name : ''}`; row.append(p);
        if (point.limit_value > 0) { const bar = document.createElement('progress'); bar.max = point.limit_value; bar.value = point.cur_value; bar.setAttribute('aria-label', point.alias || '任务进度'); row.append(bar); }
      }
    }
    target.append(row);
  }
}
for (const operation of ['progress', 'claim']) $(operation).onclick = () => action($(operation), async () => { message('正在处理任务…', false, '', 0); const data = await api(`tasks/${operation}`, {}); renderTasks(data.items, operation === 'claim'); message('任务操作完成'); });
$('logs-toggle').onclick = () => {
  $('activity-section').showModal();
  $('logs-toggle').setAttribute('aria-expanded', 'true');
  $('logs').scrollTop = $('logs').scrollHeight;
};
$('logs-close').onclick = () => $('activity-section').close();
$('activity-section').addEventListener('close', () => {
  $('logs-toggle').setAttribute('aria-expanded', 'false');
  $('logs-toggle').focus({preventScroll: true});
});
$('activity-section').addEventListener('click', event => {
  if (event.target !== $('activity-section')) return;
  const bounds = $('activity-section').getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) $('activity-section').close();
});
async function tick() { try { await refresh(); } catch (error) { message(error.message, true); } setTimeout(tick, 3000); }
refresh(true).then(() => setTimeout(tick, 3000)).catch(error => { message(error.message, true); setTimeout(tick, 3000); });
