const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));

let csrf = '';
let version = 0;
let entitiesState = [];
let selectedAlias = '';
let activeKind = 'ALL';
let replayFrames = [];
let replayIndex = 0;
let replayLive = true;
let replayTimer = 0;
let resumeReplayAfterVisibility = false;
let lastPayload = null;
let currentRenderedView = null;
let lastMapKey = '';
let refreshInFlight = false;
let refreshTimer = 0;
let policyInFlight = false;
let lastPolicyRefresh = 0;
let lastReplayTick = -1;
let replayPollTimer = 0;
let historyLoaded = false;

const renderCache = {
  metrics: '',
  entities: '',
  overview: '',
  resources: '',
  markers: '',
};

const labels = { CORE: '核心', WORKER: '工人', VANGUARD: '先锋', RANGER: '游侠' };
const actionLabels = {
  WAIT: '等待', MOVE: '移动', HARVEST: '采集', DEPOSIT: '存入', SWEEP: '横扫', SHOOT: '射击',
  HEAL: '治疗', SPAWN: '生产', REPAIR_SHIELD: '修复护盾', START_MOVE: '开始迁移', PICKUP_BEACON: '拾取信标',
};
const statusLabels = {
  RUNNING: '执行中', SUCCESS: '已完成', IDLE: '空闲', BLOCKED: '已阻塞', NO_INTENT: '无动作',
  SCHEDULED: '已排程', LEGACY: '传统策略', SHADOW: '观察中', STAGED: '已暂存', QUEUED: '排队中',
  APPLIED: '已生效', CANCELLED: '已取消', FAILED: '失败', UNKNOWN: '待同步',
};
const goalLabels = {
  LEGACY_LEGACY_ACTION: '传统动作', LEGACY_RETURN: '返回核心', LEGACY_RECON: '侦察资源',
  LEGACY_EXPLORE: '探索前沿', LEGACY_BEACON: '信标任务', HARVEST_RESOURCE: '采集资源', ECONOMY: '经济运营',
  DEFEND: '防守', ATTACK: '进攻', BEACON: '信标', LEGACY_PLAN: '传统计划', CONTROL_BEACON: '控制信标',
};
const taskLabels = {
  HARVEST: '采集资源', HARVEST_RESOURCE: '采集资源', HARVEST_VISIBLE: '采集可见资源',
  MOVE_TO_CELL: '移动到目标', RETREAT_TO_CORE: '撤回核心', HOLD_POSITION: '原地待命',
  BEACON_ESCORT: '护送信标', LEGACY_PLAN: '传统计划',
};
const reasonLabels = {
  resources_reserved_or_no_legal_core_action: '资源已保留或核心暂无合法动作',
  return_cargo_to_core: '将货物运回核心', continue_locked_resource_route: '锁定延续前往资源',
  reobserve_remembered_resource: '重新观察已记忆资源', explore_sector_frontier: '探索分区前沿',
  holding_defense_ring: '维持防守环', preferred_vanguard_to_beacon: '优先派先锋前往信标',
  path_to_resource: '前往资源路径', preserve_worker_cargo: '保留工人货物', current_resource: '当前资源',
  stale: '决策已过期', ok: '正常', manual_task_move: '人工移动任务',
};
const wakeLabels = {
  CORE_RESOURCES_OR_LEGAL_ACTION: '核心资源或出现合法动作',
  NEXT_AUTHORITATIVE_TURN: '等待下一份权威状态',
  arrive_at_resource: '抵达资源点',
};
const directionLabels = { UP: '上', DOWN: '下', LEFT: '左', RIGHT: '右' };
const commandLabels = {
  ASSIGN_TASK: '分配任务', CANCEL: '取消任务', EMERGENCY_STOP: '紧急停机', RESUME_AUTO: '恢复自动',
  START_CORE_MIGRATION: '开始核心迁移', CANCEL_CORE_MIGRATION: '取消核心迁移', UPDATE_POLICY: '更新策略',
};
const postureLabels = { BALANCED: '均衡', DEFENSIVE: '防御', ECONOMY: '经济', AGGRESSIVE: '进攻' };

function humanize(value, mapping, fallback = '其他') {
  if (value == null || value === '') return '';
  return mapping[String(value)] || fallback;
}
const action = value => humanize(value, actionLabels);
const status = value => humanize(value, statusLabels);
const goal = value => humanize(value, goalLabels);
const task = value => humanize(value, taskLabels);
const reason = value => humanize(value, reasonLabels);
const wake = value => humanize(value, wakeLabels);
const direction = value => humanize(value, directionLabels);
const signature = value => JSON.stringify(value ?? null);

function setText(id, value) {
  const node = $(id);
  if (node && node.textContent !== String(value)) node.textContent = String(value);
}

function setHtml(id, value) {
  const node = $(id);
  if (node && node.innerHTML !== value) node.innerHTML = value;
}

function rows(items, renderer, empty = '暂无数据') {
  return items?.length ? items.map(renderer).join('') : `<div class="muted">${empty}</div>`;
}

function unitRow(entity) {
  const selected = entity.alias === selectedAlias;
  const blocked = Boolean(entity.blocker);
  const idle = entity.status === 'IDLE' || entity.action === 'WAIT';
  const glyphClasses = [`kind-${String(entity.kind || 'worker').toLowerCase()}`];
  if (blocked) glyphClasses.push('is-blocked');
  else if (idle) glyphClasses.push('is-idle');
  return `<button class="unit-row ${selected ? 'is-selected' : ''}" data-alias="${esc(entity.alias)}">
    <span class="unit-glyph ${glyphClasses.join(' ')}" aria-hidden="true"></span>
    <span class="unit-row-main"><span class="unit-row-title">${esc(labels[entity.kind] || '单位')} · ${esc(entity.alias)}</span>
    <span class="unit-row-sub">${esc(task(entity.task) || '空闲')} · ${esc(action(entity.action) || '无动作')} · ${entity.position ? esc(entity.position.join(',')) : '状态待同步'}</span></span>
    <span class="state-pill">${esc(status(entity.status || 'UNKNOWN'))}</span>
  </button>`;
}

function renderUnitList() {
  const query = String($('unitSearch')?.value || '').trim().toLowerCase();
  const filtered = entitiesState.filter(entity => (
    (activeKind === 'ALL' || entity.kind === activeKind)
    && (!query || String(entity.alias).toLowerCase().includes(query))
  ));
  setHtml('unitList', rows(filtered, unitRow, '没有符合筛选条件的单位'));
  setText('unitFilterCount', `${filtered.length}/${entitiesState.length}`);
}

function stateLine(entity) {
  if (!entity.state_synced) return '<span class="sync-wait">等待下一份权威状态</span>';
  const position = entity.position ? `位置 ${esc(entity.position.join(','))}` : '位置 —';
  const hp = entity.hp == null ? 'HP —' : `HP ${esc(entity.hp)}`;
  const cargo = entity.cargo == null ? '' : ` · 货物 ${esc(entity.cargo)}`;
  const shield = entity.shield == null ? '' : ` · 护盾 ${esc(entity.shield)}`;
  return `${position} · ${hp}${shield}${cargo}`;
}

function card(entity) {
  const target = entity.target_cell ? ` → 目标 ${esc(entity.target_cell.join(','))}` : '';
  const eta = entity.eta_ticks == null ? '' : ` · 预计 ${esc(entity.eta_ticks)} Tick`;
  const assignment = entity.assignment || {};
  const candidates = rows(entity.candidate_intents, candidate => (
    `<li>${esc(action(candidate.action) || '—')} ${esc(direction(candidate.direction) || '')} ${candidate.target_cell ? `→ ${esc(candidate.target_cell.join(','))}` : ''} ${esc(reason(candidate.reason) || '')}</li>`
  ), '无备选动作');
  const nodes = rows(entity.node_path, node => (
    `<li><b>${esc(node.node_id)}</b> · ${esc(status(node.status))} · ${esc(reason(node.reason))}</li>`
  ), '无行为树节点');
  return `<article class="unit-card ${entity.status === 'RUNNING' ? 'is-running' : ''} ${entity.blocker ? 'is-blocked' : ''}">
    <header class="unit-head"><div><b>${esc(labels[entity.kind] || entity.kind || '单位')}</b><span class="alias">${esc(entity.alias)}</span></div><span class="state-pill">${esc(status(entity.status || 'UNKNOWN'))}</span></header>
    <div class="unit-state"><span>${stateLine(entity)}</span><span class="tick">#${esc(entity.trace_tick ?? '—')}</span></div>
    <div class="decision-grid">
      <div><small>当前任务</small><strong>${esc(task(entity.task) || '空闲')}</strong><span>${esc(goal(entity.goal) || '无目标')} ${assignment.role ? `· ${esc(labels[assignment.role] || humanize(assignment.role, {}))}` : ''}</span></div>
      <div><small>当前动作</small><strong>${esc(action(entity.action) || '—')}${target}</strong><span>${esc(reason(entity.reason) || '无原因')}</span></div>
      <div class="next-step"><small>下一步</small><strong>${esc(action(entity.next_step) || task(entity.next_step) || '等待新决策')}</strong><span>${esc(wake(entity.wake_condition) || reason(entity.blocker) || '无触发条件')}${eta}</span></div>
    </div>
    <div class="unit-meta">${assignment.lock ? `目标锁 ${esc(assignment.lock)} · ` : ''}${assignment.lease_until_tick != null ? `租约至 #${esc(assignment.lease_until_tick)} · ` : ''}${entity.waited_ticks ? `已等待 ${esc(entity.waited_ticks)} Tick` : ''}</div>
    <details><summary>查看决策链</summary><div class="trace-columns"><div><b>备选动作</b><ul>${candidates}</ul></div><div><b>行为树路径</b><ul>${nodes}</ul></div></div></details>
    <button class="neutral select-entity" data-alias="${esc(entity.alias)}">用于任务</button>
  </article>`;
}

function renderUnitDetail() {
  const entity = entitiesState.find(item => item.alias === selectedAlias) || entitiesState[0];
  if (!entity) {
    setHtml('unitDetail', '<div class="empty-detail">等待单位状态</div>');
    return;
  }
  selectedAlias = entity.alias;
  setHtml('unitDetail', card(entity));
  if ($('taskAlias')) $('taskAlias').value = entity.alias;
}

function chooseUnit(alias, { focusMap = true } = {}) {
  if (!entitiesState.some(entity => entity.alias === alias)) return;
  selectedAlias = alias;
  renderUnitList();
  renderUnitDetail();
  renderResourceInfo(currentRenderedView, true);
  if (focusMap) window.focusTacticalUnit?.(alias);
  else window.selectTacticalUnit?.(alias);
}
window.selectDashboardUnit = alias => chooseUnit(alias, { focusMap: false });

function cell(value) {
  const match = String(value).trim().match(/^(-?\d+)\s*,\s*(-?\d+)$/);
  return match ? [Number(match[1]), Number(match[2])] : null;
}

function syncEntityChoices(entities) {
  const select = $('taskAlias');
  if (!select) return;
  const chosen = selectedAlias || select.value;
  const items = (entities || []).filter(entity => /^entity_[0-9a-f]{12}$/.test(String(entity.alias || '')));
  select.innerHTML = `<option value="">选择当前实体…</option>${items.map(entity => (
    `<option value="${esc(entity.alias)}">${esc(entity.alias)} · ${esc(labels[entity.kind] || entity.kind || '未知')}</option>`
  )).join('')}`;
  if (items.some(entity => entity.alias === chosen)) select.value = chosen;
}

function taskLine(item, showTick = false) {
  const target = Array.isArray(item.target) ? ` · 目标 ${esc(item.target.join(','))}` : '';
  const lease = item.lease_until_tick == null ? '' : ` · 租约至 #${esc(item.lease_until_tick)}`;
  return `<div class="row">${showTick ? `<span class="tick">#${esc(item.tick)}</span> ` : ''}<b>${esc(item.task_id)}</b> <span class="tag">${esc(status(item.status))}</span> ${esc(goal(item.goal) || task(item.kind) || '')} ${esc(item.actor_alias || '')}${target}${lease} ${esc(reason(item.reason) || '')}</div>`;
}

function renderResourceInfo(view, force = false) {
  const current = view?.current || {};
  const commandCenter = view?.command_center || {};
  const resources = (current.map?.resources || []).filter(item => Array.isArray(item) && item.length === 2);
  const selected = (commandCenter.entities || []).find(entity => entity.alias === selectedAlias);
  const mappedSelected = (current.map?.friendly || []).find(entity => entity.alias === selectedAlias);
  const core = (current.map?.friendly || []).find(entity => entity.kind === 'CORE');
  const focus = selected?.position || mappedSelected?.position || core?.position || [0, 0];
  const nearest = resources.map(position => ({
    position,
    distance: Math.abs(position[0] - focus[0]) + Math.abs(position[1] - focus[1]),
  })).sort((left, right) => left.distance - right.distance || left.position[0] - right.position[0] || left.position[1] - right.position[1]).slice(0, 8);
  const key = signature([current.tick, selectedAlias, nearest]);
  if (!force && renderCache.resources === key) return;
  renderCache.resources = key;
  setHtml('resourceInfo', nearest.length ? nearest.map(item => (
    `<button class="resource-row" data-cell="${item.position[0]},${item.position[1]}"><span>资源点 ${esc(item.position.join(','))} · 距离 ${item.distance}</span><small>余量未知</small></button>`
  )).join('') : '<div class="muted">当前无可见资源点</div>');
}

function render(view) {
  const service = view?.service || {};
  const current = view?.current || {};
  const commandCenter = view?.command_center || {};
  currentRenderedView = view;
  version = Number(commandCenter.command_version ?? version);

  const metricsKey = signature([
    service.running, service.connected, service.last_tick, current.tick, current.resources,
    current.resource_capacity, current.mode_label, commandCenter.entities?.length,
  ]);
  if (renderCache.metrics !== metricsKey) {
    renderCache.metrics = metricsKey;
    const statusNode = $('status');
    if (statusNode) statusNode.className = `status ${service.connected ? 'ok' : ''}`;
    setText('status', !service.running ? '服务已停止' : (service.connected ? '已连接 · 对战中' : '服务在线 · 等待连接'));
    setText('tick', current.tick ?? service.last_tick ?? '—');
    setText('resources', current.resources == null ? '—' : `${current.resources}/${current.resource_capacity ?? '—'}`);
    setText('mode', current.mode_label || '等待数据');
    setText('unitCount', commandCenter.entities?.length || 0);
  }

  const nextEntities = (commandCenter.entities || []).slice().sort((left, right) => (
    ({ CORE: 0, WORKER: 1, VANGUARD: 2, RANGER: 3 }[left.kind] ?? 9)
    - ({ CORE: 0, WORKER: 1, VANGUARD: 2, RANGER: 3 }[right.kind] ?? 9)
    || String(left.alias).localeCompare(String(right.alias))
  ));
  const entitiesKey = signature([current.tick, nextEntities]);
  if (renderCache.entities !== entitiesKey) {
    renderCache.entities = entitiesKey;
    entitiesState = nextEntities;
    if (!entitiesState.some(entity => entity.alias === selectedAlias)) selectedAlias = entitiesState[0]?.alias || '';
    renderUnitList();
    renderUnitDetail();
    syncEntityChoices(entitiesState);
    if (selectedAlias) window.selectTacticalUnit?.(selectedAlias);
  }

  const overviewKey = signature([
    current.tick, current.mode_label, commandCenter.goals, commandCenter.tasks,
    commandCenter.commands, commandCenter.timeline, entitiesState.map(entity => entity.task),
  ]);
  if (renderCache.overview !== overviewKey) {
    renderCache.overview = overviewKey;
    const taskCounts = entitiesState.reduce((output, entity) => {
      output[entity.task || 'IDLE'] = (output[entity.task || 'IDLE'] || 0) + 1;
      return output;
    }, {});
    const summary = Object.entries(taskCounts).map(([name, count]) => `${esc(task(name) || '空闲')} ${count} 个`).join(' · ');
    setHtml('goals', rows(commandCenter.goals, item => (
      `<div class="row"><b>${esc(goal(item.goal))}</b> <span class="tag">${esc(status(item.status))}</span> ${esc(task(item.stage) || item.stage || '')}</div>`
    )) + (commandCenter.tasks?.length ? '' : `<div class="row"><b>当前主线</b> <span class="tag">${esc(current.mode_label || '待命')}</span> ${summary || '暂无单位决策'}</div>`));
    setHtml('tasks', rows(commandCenter.tasks, item => taskLine(item), '当前没有人工或租约任务'));
    setHtml('timeline', rows(commandCenter.timeline, item => taskLine(item, true), '当前没有任务切换记录'));
    setHtml('commands', rows(commandCenter.commands, item => (
      `<div class="row">${esc(commandLabels[item.type] || humanize(item.type, {}))} · ${esc(status(item.status))}</div>`
    ), '尚无命令'));
  }

  setText('mapSummary', `己方 ${current.map?.friendly?.length || 0} · 敌方 ${current.map?.enemies?.length || 0} · 资源 ${current.map?.resources?.length || 0} · 已观测 ${current.map?.observed?.length || 0}格`);
  renderResourceInfo(view);
}

async function api(path, method = 'GET', data) {
  const headers = { 'Content-Type': 'application/json' };
  if (csrf) {
    headers['X-CSRF-Token'] = csrf;
    headers['If-Match'] = `"command-version-${version}"`;
    headers['Idempotency-Key'] = `ui-${crypto.randomUUID()}`;
  }
  const response = await fetch(path, {
    method,
    headers,
    body: data ? JSON.stringify(data) : undefined,
  });
  const payload = await response.json();
  if (payload.command_version != null) version = payload.command_version;
  if (!response.ok) throw Error(payload.error || '请求失败');
  return payload;
}

async function login() {
  try {
    const payload = await api('/api/v1/session', 'POST', { password: $('password').value });
    csrf = payload.csrf_token;
    version = Number(payload.command_version ?? 0);
    setText('loginState', '已认证；写操作将在下一 Tick 生效。');
    await Promise.all([refreshTasks(), refreshPolicy(true)]);
  } catch (_) { setText('loginState', '认证失败或写功能未配置。'); }
}

async function assign() {
  if (!csrf) { setText('taskState', '请先认证。'); return; }
  const alias = $('taskAlias').value.trim();
  const taskKind = $('taskKind').value;
  const target = cell($('taskTarget').value);
  const priority = Number($('taskPriority').value);
  if (!/^entity_[0-9a-f]{12}$/.test(alias)) { setText('taskState', '请选择当前实体。'); return; }
  if (taskKind === 'MOVE_TO_CELL' && !target) { setText('taskState', '移动任务需要 x,y 目标。'); return; }
  try {
    await api(`/api/v1/entities/${alias}/tasks`, 'POST', { task_kind: taskKind, priority, ...(target ? { target } : {}) });
    setText('taskState', '任务已排队，下一次成功提交后生效。');
    await refreshTasks();
  } catch (error) { setText('taskState', `任务未接受：${error.message}`); }
}

function renderTasks(tasks) {
  setHtml('taskCommands', rows(tasks, item => (
    `<div class="row"><b>${esc(commandLabels[item.type] || humanize(item.type, {}))}</b> <span class="tag">${esc(status(item.status))}</span>${item.status === 'QUEUED' ? ` <button class="neutral cancel-command" data-command="${esc(item.command_id)}">撤回</button>` : ''}${item.status === 'APPLIED' && item.type === 'ASSIGN_TASK' ? ` <button class="neutral cancel-entity" data-alias="${esc(item.entity_alias)}">取消任务</button>` : ''}</div>`
  ), '暂无人工任务。'));
}

async function refreshTasks() {
  if (!csrf) return;
  try { renderTasks((await api('/api/v1/tasks')).tasks || []); }
  catch (error) { setText('taskCommands', `任务状态读取失败：${error.message}`); }
}

async function cancelCommand(id) {
  try {
    await api(`/api/v1/commands/${encodeURIComponent(id)}`, 'DELETE');
    setText('taskState', '排队命令已撤回。');
    await refreshTasks();
  } catch (error) { setText('taskState', `撤回失败：${error.message}`); }
}

async function cancelEntity(alias) {
  try {
    await api(`/api/v1/entities/${encodeURIComponent(alias)}/cancel`, 'POST', {});
    setText('taskState', '取消任务已排队，下一次成功提交后生效。');
    await refreshTasks();
  } catch (error) { setText('taskState', `取消未接受：${error.message}`); }
}

async function migrate() {
  if (!csrf) { setText('taskState', '请先认证。'); return; }
  const target = cell($('migrationTarget').value);
  if (!target) { setText('taskState', '迁移目标必须是 x,y。'); return; }
  if (!confirm('确认排队 Core 迁移？执行时仍会重新校验安全性。')) return;
  try {
    await api('/api/v1/core/migrations', 'POST', { target });
    setText('taskState', '迁移已排队，下一次成功提交后生效。');
  } catch (error) { setText('taskState', `迁移未接受：${error.message}`); }
}

async function cancelMigration() {
  if (!csrf) { setText('taskState', '请先认证。'); return; }
  try {
    await api('/api/v1/core/migrations', 'DELETE');
    setText('taskState', '取消已排队，下一次成功提交后生效。');
  } catch (error) { setText('taskState', `取消未接受：${error.message}`); }
}

function renderPolicy(policy) {
  setText('policyCurrent', postureLabels[policy.posture] || '其他');
  if ($('policyPosture')) $('policyPosture').value = policy.posture || 'BALANCED';
}

async function refreshPolicy(force = false) {
  if (!csrf || policyInFlight || (!force && Date.now() - lastPolicyRefresh < 15000)) return;
  policyInFlight = true;
  try {
    renderPolicy(await api('/api/v1/policy'));
    lastPolicyRefresh = Date.now();
  } catch (error) { setText('policyState', `策略读取失败：${error.message}`); }
  finally { policyInFlight = false; }
}

async function setPolicy() {
  if (!csrf) { setText('policyState', '请先认证。'); return; }
  try {
    await api('/api/v1/policy', 'PATCH', { posture: $('policyPosture').value });
    setText('policyState', '策略已排队，下一次成功提交后生效。');
    await refreshPolicy(true);
  } catch (error) { setText('policyState', `策略未接受：${error.message}`); }
}

function selectedFrame() { return replayFrames[replayIndex] || null; }

function renderReplayMarkers() {
  const markerKey = signature(replayFrames.map(frame => [frame.tick, frame.markers]));
  if (renderCache.markers === markerKey) return;
  renderCache.markers = markerKey;
  const maximum = Math.max(1, replayFrames.length - 1);
  setHtml('replayMarkers', replayFrames.flatMap((frame, index) => (
    (frame.markers || []).map(marker => `<button class="replay-marker ${esc(String(marker.kind || '').toLowerCase())}" style="left:${index / maximum * 100}%" data-index="${index}" title="#${esc(frame.tick)} · ${esc(marker.label)}"></button>`)
  )).join(''));
}

function renderReplay({ forceMap = false } = {}) {
  const frame = selectedFrame();
  if (!frame) {
    setText('replayState', '等待回放快照');
    setText('replayTick', '—');
    return;
  }
  const view = {
    ...lastPayload,
    current: frame.snapshot,
    command_center: frame.command_center || { timeline: lastPayload?.command_center?.timeline || [] },
  };
  $('replaySlider').value = String(replayIndex);
  setText('replayTick', `#${frame.tick ?? '—'}`);
  setText('replayState', replayLive ? '实时跟随最新 Tick' : '历史回放 · 自动态势仍在后台更新');
  setText('replayPlay', replayTimer ? '⏸ 暂停' : '▶ 播放');
  const badge = $('mapModeBadge');
  if (badge) {
    badge.textContent = replayLive ? '实时态势' : '历史回放';
    badge.classList.toggle('is-replay', !replayLive);
  }
  window.DashboardReplay = { selected: view };
  render(view);
  const mapKey = `${frame.tick ?? 'unknown'}:${replayLive ? 'live' : 'replay'}`;
  if (forceMap || lastMapKey !== mapKey) {
    lastMapKey = mapKey;
    try {
      window.renderTacticalMap?.(view);
      if (selectedAlias) window.selectTacticalUnit?.(selectedAlias);
    } catch (error) { console.error('Tactical map render error:', error); }
  }
}

function selectReplay(index, { live = false } = {}) {
  if (!replayFrames.length) return;
  replayIndex = Math.max(0, Math.min(replayFrames.length - 1, index));
  replayLive = live;
  if (!live && !historyLoaded) {
    historyLoaded = true;
    fetchReplayHistory();
  }
  renderReplay();
}

function setLive(payload) {
  lastPayload = payload;
  render(payload);
}

function mergeReplayFrames(newFrames) {
  if (!newFrames.length) return;
  const priorTick = selectedFrame()?.tick;
  const tickMap = new Map(replayFrames.map(f => [f.tick, f]));
  for (const frame of newFrames) tickMap.set(frame.tick, frame);
  replayFrames = [...tickMap.values()].sort((a, b) => Number(a.tick) - Number(b.tick)).slice(-200);
  lastReplayTick = Math.max(...replayFrames.map(f => Number(f.tick)));
  const latest = Math.max(0, replayFrames.length - 1);
  if (replayLive || replayIndex >= replayFrames.length) replayIndex = latest;
  else if (priorTick != null) {
    const retained = replayFrames.findIndex(frame => frame.tick === priorTick);
    replayIndex = retained >= 0 ? retained : Math.min(replayIndex, latest);
  }
  $('replaySlider').max = String(latest);
  $('replaySlider').value = String(replayIndex);
  renderReplayMarkers();
  renderReplay();
}

function stopReplay() {
  if (replayTimer) window.clearInterval(replayTimer);
  replayTimer = 0;
}

function startReplay() {
  if (!replayFrames.length || replayTimer) return;
  replayLive = false;
  if (!historyLoaded) {
    historyLoaded = true;
    fetchReplayHistory();
  }
  replayTimer = window.setInterval(() => {
    if (document.hidden) return;
    if (replayIndex >= replayFrames.length - 1) {
      stopReplay();
      renderReplay();
      return;
    }
    selectReplay(replayIndex + 1);
  }, 700);
  renderReplay();
}

function playReplay() {
  if (replayTimer) { stopReplay(); renderReplay(); }
  else startReplay();
}

async function refresh() {
  if (refreshInFlight || document.hidden) return;
  refreshInFlight = true;
  try {
    const response = await fetch('/api/dashboard', { cache: 'no-store' });
    if (!response.ok) throw Error(`HTTP ${response.status}`);
    setLive(await response.json());
    await refreshPolicy();
  } catch (_) {
    const statusNode = $('status');
    if (statusNode) statusNode.className = 'status';
    setText('status', '状态获取失败 · 将自动重试');
  } finally { refreshInFlight = false; }
}

async function refreshReplay() {
  if (document.hidden || !lastPayload) return;
  try {
    const url = replayLive
      ? `/api/replay?limit=32&from_tick=${lastReplayTick}`
      : null;
    if (!url) return;
    const r = await fetch(url, { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    mergeReplayFrames(data.frames || []);
  } catch (_) {}
}

async function fetchReplayHistory() {
  try {
    const r = await fetch('/api/replay?limit=64', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    mergeReplayFrames(data.frames || []);
  } catch (_) {}
}

function startPolling() {
  if (refreshTimer) window.clearInterval(refreshTimer);
  refreshTimer = 0;
  if (replayPollTimer) window.clearInterval(replayPollTimer);
  replayPollTimer = 0;
  if (document.hidden) return;
  refresh();
  refreshReplay();
  refreshTimer = window.setInterval(refresh, 3000);
  replayPollTimer = window.setInterval(refreshReplay, 3000);
}

function updateDashboardMapCursor(position) {
  setText('mapCursor', position ? `光标 ${position[0]},${position[1]}` : '光标 —');
}
function updateDashboardTargetMode(enabled) {
  if ($('mapTargetMode')) $('mapTargetMode').hidden = !enabled;
  if ($('mapPickTarget')) {
    $('mapPickTarget').classList.toggle('is-active', Boolean(enabled));
    $('mapPickTarget').setAttribute('aria-pressed', String(Boolean(enabled)));
  }
}
function setDashboardMapTarget(position) {
  $('taskTarget').value = `${position[0]},${position[1]}`;
  $('taskKind').value = 'MOVE_TO_CELL';
  updateDashboardMapCursor(position);
  setText('taskState', `已从地图锁定目标 ${position[0]},${position[1]}；认证后可排队任务。`);
  const drawer = document.querySelector('.order-drawer');
  if (drawer) drawer.open = true;
}
window.updateDashboardMapCursor = updateDashboardMapCursor;
window.updateDashboardTargetMode = updateDashboardTargetMode;
window.setDashboardMapTarget = setDashboardMapTarget;

$('login').onclick = login;
$('assign').onclick = assign;
$('migrate').onclick = migrate;
$('cancelMigration').onclick = cancelMigration;
$('setPolicy').onclick = setPolicy;
$('unitSearch').oninput = renderUnitList;
$('unitFilters').onclick = event => {
  const button = event.target.closest('.filter-btn');
  if (!button) return;
  activeKind = button.dataset.kind;
  document.querySelectorAll('.filter-btn').forEach(item => item.classList.toggle('is-active', item === button));
  renderUnitList();
};
$('unitList').onclick = event => {
  const button = event.target.closest('.unit-row');
  if (button) chooseUnit(button.dataset.alias);
};
$('unitDetail').onclick = event => {
  const button = event.target.closest('.select-entity');
  if (button) chooseUnit(button.dataset.alias);
};
$('taskAlias').onchange = event => {
  if (event.target.value) chooseUnit(event.target.value);
};
$('resourceInfo').onclick = event => {
  const button = event.target.closest('.resource-row');
  const position = button ? cell(button.dataset.cell) : null;
  if (!position) return;
  window.focusTacticalCell?.(position);
  updateDashboardMapCursor(position);
};
$('mapPickTarget').onclick = () => {
  const next = $('mapTargetMode').hidden;
  window.setTacticalMapTargetMode?.(next);
  updateDashboardTargetMode(next);
};
$('mapZoomIn').onclick = () => window.zoomTacticalMap?.(1.2);
$('mapZoomOut').onclick = () => window.zoomTacticalMap?.(0.84);
$('mapReset').onclick = () => window.resetTacticalMap?.();
$('layerFog').onchange = event => window.setTacticalMapLayer?.('fog', event.target.checked);
$('visionMode').onchange = event => window.setTacticalVisionMode?.(event.target.value);
$('layerCoordinates').onchange = event => window.setTacticalMapLayer?.('coordinates', event.target.checked);
$('layerLabels').onchange = event => window.setTacticalMapLayer?.('labels', event.target.checked);
$('taskCommands').onclick = event => {
  const button = event.target.closest('button');
  if (button?.dataset.command) cancelCommand(button.dataset.command);
  if (button?.dataset.alias) cancelEntity(button.dataset.alias);
};
$('replaySlider').oninput = event => selectReplay(Number(event.target.value));
$('replayStart').onclick = () => selectReplay(0);
$('replayPrev').onclick = () => selectReplay(replayIndex - 1);
$('replayPlay').onclick = playReplay;
$('replayNext').onclick = () => selectReplay(replayIndex + 1);
$('replayLive').onclick = () => {
  stopReplay();
  historyLoaded = false;
  selectReplay(replayFrames.length - 1, { live: true });
};
$('replayMarkers').onclick = event => {
  const marker = event.target.closest('.replay-marker');
  if (marker) selectReplay(Number(marker.dataset.index));
};

document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (refreshTimer) window.clearInterval(refreshTimer);
    refreshTimer = 0;
    if (replayPollTimer) window.clearInterval(replayPollTimer);
    replayPollTimer = 0;
    resumeReplayAfterVisibility = Boolean(replayTimer);
    stopReplay();
  } else {
    startPolling();
    if (resumeReplayAfterVisibility) startReplay();
    resumeReplayAfterVisibility = false;
  }
});

startPolling();
