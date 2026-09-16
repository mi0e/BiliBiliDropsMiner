const $ = (id) => document.getElementById(id);
let current = null, groupVersion = null, qrTimer = null, qrEpoch = 0;
let roomPreview = null;
const pending = new Set();
function message(text, error = false) { $('message').textContent = text; $('message').className = error ? 'error' : ''; }
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
  const busy = current.phase !== 'stopped';
  for (const id of ['qr-button', 'manual-mode', 'logout', 'discover', 'select']) $(id).disabled = busy || pending.has($(id));
  for (const input of $('settings').elements) input.disabled = busy || pending.has(input);
  for (const form of ['cookie-form', 'task-ids-form']) {
    for (const input of $(form).elements) input.disabled = busy || pending.has(input);
  }
  $('start').disabled = busy || !current.logged_in || pending.has($('start'));
  $('stop').disabled = !busy || pending.has($('stop'));
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
  $('account').textContent = data.logged_in ? '凭据已保存' : '未登录';
  $('account').dataset.connected = String(data.logged_in);
  $('phase').textContent = ({stopped: '已停止', starting: '正在启动', running: '运行中', stopping: '正在停止'})[data.phase];
  $('phase').dataset.phase = data.phase;
  renderRoom(data.settings.room_ids);
  $('session-count').textContent = `${data.active_sessions ?? 0}/${data.planned_sessions ?? 0}`;
  $('logs').textContent = data.logs.join('\n') || '暂无记录'; renderGroups(data); controls();
}
async function action(button, operation) {
  pending.add(button); button.disabled = true; button.setAttribute('aria-busy', 'true');
  try { await operation(); await refresh(); } catch (error) { message(error.message, true); }
  finally { pending.delete(button); button.disabled = false; button.removeAttribute('aria-busy'); controls(); }
}
$('settings').addEventListener('submit', (event) => {
  event.preventDefault(); action(event.submitter || $('settings').querySelector('button'), async () => {
    const rooms = $('rooms').value.trim().split(/[,，\s]+/).filter(Boolean);
    if (!rooms.length || rooms.some(room => !/^\d+$/.test(room))) throw new Error('请填写数字房间号，用逗号或换行分隔');
    await api('settings', {room_ids: rooms.map(Number), thread_count: Number($('threads').value), reconnect_delay_seconds: Number($('reconnect').value), task_query_interval_seconds: Number($('interval').value)});
    message('配置已保存');
  });
});
function cancelQr() {
  qrEpoch++; clearTimeout(qrTimer); $('qr-panel').hidden = true; $('qr-image').removeAttribute('src');
  $('cookie-form').hidden = false;
  $('manual-mode').setAttribute('aria-pressed', 'true'); $('qr-button').setAttribute('aria-pressed', 'false');
}
$('manual-mode').onclick = cancelQr;
$('cookie-form').addEventListener('submit', event => {
  event.preventDefault();
  action($('save-cookie'), async () => {
    await api('cookie', {cookie: $('cookie').value});
    $('cookie').value = ''; cancelQr(); message('Cookie 已保存');
  });
});
$('task-ids-form').addEventListener('submit', event => {
  event.preventDefault();
  action($('save-task-ids'), async () => {
    const data = await api('task-ids', {task_ids: $('task-ids').value});
    $('task-ids').value = data.task_ids.join(', '); message('任务 ID 已保存');
  });
});
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
      if (result.status === 'SUCCESS') { cancelQr(); message('扫码登录成功'); await refresh(); return; }
      if (result.status === 'EXPIRED') return;
    } catch (error) { if (epoch !== qrEpoch) return; $('qr-status').textContent = error.message; if (++failures >= 3) return; }
    qrTimer = setTimeout(poll, 2500);
  }
  qrTimer = setTimeout(poll, 2500);
});
$('logout').onclick = () => action($('logout'), async () => { await api('logout', {}); $('cookie').value = ''; cancelQr(); message('已清除服务端登录凭据'); });
$('discover').onclick = () => action($('discover'), async () => { message('正在获取静态 HTML…'); const data = await api('discover', {}); groupVersion = null; renderGroups(data); message(data.groups.length ? '解析完成，请选择并保存任务分组' : '静态 HTML 中没有可解析的任务；可稍后重试或仅运行观看挂机'); });
$('select').onclick = () => action($('select'), async () => { await api('selection', {groups: [...$('groups').querySelectorAll('input:checked')].map(input => Number(input.value)), generation: groupVersion}); message('任务分组选择已保存'); });
for (const operation of ['start', 'stop']) $(operation).onclick = () => action($(operation), async () => { await api(operation, {}); message(operation === 'start' ? '正在使用已保存的配置启动挂机' : '已请求停止，正在释放连接'); });
function renderTasks(items, claim) {
  $('tasks').replaceChildren();
  if (!items.length) { $('tasks').textContent = '暂无任务结果'; return; }
  for (const item of items) {
    const row = document.createElement('div'); row.className = 'task';
    const title = document.createElement('strong'); title.textContent = item.task_name || item.task_id; row.append(title);
    if (claim) { const p = document.createElement('p'); p.textContent = `${item.success ? '领取成功' : item.skipped ? '已跳过' : '未领取'} · ${item.message || ''}`; row.append(p); }
    else {
      const checkpoints = item.check_points && item.check_points.length ? item.check_points : [item];
      for (const point of checkpoints) {
        const p = document.createElement('p'); p.textContent = `${point.alias || '进度'}：${point.cur_value} / ${point.limit_value}${point.award_name ? ' · ' + point.award_name : ''}`; row.append(p);
        if (point.limit_value > 0) { const bar = document.createElement('progress'); bar.max = point.limit_value; bar.value = point.cur_value; bar.setAttribute('aria-label', point.alias || '任务进度'); row.append(bar); }
      }
    }
    $('tasks').append(row);
  }
}
for (const operation of ['progress', 'claim']) $(operation).onclick = () => action($(operation), async () => { message('正在处理任务…'); const data = await api(`tasks/${operation}`, {}); renderTasks(data.items, operation === 'claim'); message('任务操作完成'); });
async function tick() { try { await refresh(); } catch (error) { message(error.message, true); } setTimeout(tick, 3000); }
refresh(true).then(() => setTimeout(tick, 3000)).catch(error => { message(error.message, true); setTimeout(tick, 3000); });
