const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let csrf='',version=0,entitiesState=[],selectedAlias='',activeKind='ALL',replayFrames=[],replayIndex=0,replayLive=true,replayTimer=0,lastPayload=null;

const labels={CORE:'核心',WORKER:'工人',VANGUARD:'先锋',RANGER:'游侠'};

const actionLabels={WAIT:'等待',MOVE:'移动',HARVEST:'采集',DEPOSIT:'存入',SWEEP:'横扫',SHOOT:'射击',HEAL:'治疗',SPAWN:'生产',REPAIR_SHIELD:'修复护盾',START_MOVE:'开始迁移',PICKUP_BEACON:'拾取信标'};

const statusLabels={RUNNING:'执行中',SUCCESS:'已完成',IDLE:'空闲',BLOCKED:'已阻塞',NO_INTENT:'无动作',SCHEDULED:'已排程',LEGACY:'传统策略',SHADOW:'观察中',STAGED:'已暂存',QUEUED:'排队中',APPLIED:'已生效',CANCELLED:'已取消',FAILED:'失败'};

const goalLabels={LEGACY_LEGACY_ACTION:'传统动作',LEGACY_RETURN:'返回核心',LEGACY_RECON:'侦察资源',LEGACY_EXPLORE:'探索前沿',LEGACY_BEACON:'信标任务',HARVEST_RESOURCE:'采集资源',ECONOMY:'经济运营',DEFEND:'防守',ATTACK:'进攻',BEACON:'信标',LEGACY_PLAN:'传统计划',CONTROL_BEACON:'控制信标'};

const taskLabels={HARVEST:'采集资源',HARVEST_RESOURCE:'采集资源',MOVE_TO_CELL:'移动到目标',RETREAT_TO_CORE:'撤回核心',HOLD_POSITION:'原地待命',BEACON_ESCORT:'护送信标',LEGACY_PLAN:'传统计划'};

const reasonLabels={resources_reserved_or_no_legal_core_action:'资源已保留或核心暂无合法动作',return_cargo_to_core:'将货物运回核心',reobserve_remembered_resource:'重新观察已记忆资源',explore_sector_frontier:'探索分区前沿',holding_defense_ring:'维持防守环',preferred_vanguard_to_beacon:'优先派先锋前往信标',path_to_resource:'前往资源路径',preserve_worker_cargo:'保留工人货物',current_resource:'当前资源',stale:'决策已过期',ok:'正常',manual_task_move:'人工移动任务'};

const wakeLabels={CORE_RESOURCES_OR_LEGAL_ACTION:'核心资源或出现合法动作',NEXT_AUTHORITATIVE_TURN:'等待下一份权威状态',arrive_at_resource:'抵达资源点'};

const directionLabels={UP:'上',DOWN:'下',LEFT:'左',RIGHT:'右'};

const commandLabels={ASSIGN_TASK:'分配任务',CANCEL:'取消任务',EMERGENCY_STOP:'紧急停机',RESUME_AUTO:'恢复自动',START_CORE_MIGRATION:'开始核心迁移',CANCEL_CORE_MIGRATION:'取消核心迁移',UPDATE_POLICY:'更新策略'};

const postureLabels={BALANCED:'均衡',DEFENSIVE:'防御',ECONOMY:'经济',AGGRESSIVE:'进攻'};

function humanize(value,map,fallback='其他'){if(value==null||value==='')return '';
const key=String(value);
return map[key]||fallback}
const action=v=>humanize(v,actionLabels),status=v=>humanize(v,statusLabels),goal=v=>humanize(v,goalLabels),task=v=>humanize(v,taskLabels),reason=v=>humanize(v,reasonLabels),wake=v=>humanize(v,wakeLabels),direction=v=>humanize(v,directionLabels);

function rows(items,render,empty='暂无数据'){return items?.length?items.map(render).join(''):`<div class="muted">${empty}</div>`}
function unitRow(e){const selected=e.alias===selectedAlias,blocked=Boolean(e.blocker),idle=e.status==='IDLE'||e.action==='WAIT';
return `<button class="unit-row ${selected?'is-selected':''}" data-alias="${esc(e.alias)}"><span class="unit-dot ${blocked?'is-blocked':idle?'is-idle':''}"></span><span class="unit-row-main"><span class="unit-row-title">${esc(labels[e.kind]||'单位')} · ${esc(e.alias)}</span><span class="unit-row-sub">${esc(task(e.task)||'空闲')} · ${esc(action(e.action)||'无动作')} · ${e.position?esc(e.position.join(',')):'状态待同步'}</span></span><span class="state-pill">${esc(status(e.status||'UNKNOWN'))}</span></button>`}
function renderUnitList(){const q=String($('unitSearch')?.value||'').trim().toLowerCase(),filtered=entitiesState.filter(e=>(activeKind==='ALL'||e.kind===activeKind)&&(!q||String(e.alias).toLowerCase().includes(q)));
$('unitList').innerHTML=rows(filtered,unitRow,'没有符合筛选条件的单位');
$('unitFilterCount').textContent=`${filtered.length}/${entitiesState.length}`}
function renderUnitDetail(){const e=entitiesState.find(item=>item.alias===selectedAlias)||entitiesState[0];
if(!e){$('unitDetail').innerHTML='<div class="empty-detail">等待单位状态</div>';
return}selectedAlias=e.alias;
$('unitDetail').innerHTML=card(e);
$('taskAlias').value=e.alias}
function chooseUnit(alias){if(!entitiesState.some(e=>e.alias===alias))return;
selectedAlias=alias;
renderUnitList();
renderUnitDetail()}
function cell(v){const m=String(v).trim().match(/^(-?\d+)\s*,\s*(-?\d+)$/);
return m?[Number(m[1]),Number(m[2])]:null}
function syncEntityChoices(entities){const select=$('taskAlias'),chosen=select.value,items=(entities||[]).filter(e=>/^entity_[0-9a-f]{12}$/.test(String(e.alias||'')));
select.innerHTML=`<option value="">选择当前实体…</option>${items.map(e=>`<option value="${esc(e.alias)}">${esc(e.alias)} · ${esc(labels[e.kind]||e.kind||'未知')}</option>`).join('')}`;
if(items.some(e=>e.alias===chosen))select.value=chosen}
function stateLine(e){if(!e.state_synced)return '<span class="sync-wait">等待下一份权威状态</span>';
const pos=e.position?`位置 ${esc(e.position.join(','))}`:'位置 —',hp=e.hp==null?'HP —':`HP ${esc(e.hp)}`,cargo=e.cargo==null?'':`货物 ${esc(e.cargo)}`,shield=e.shield==null?'':`护盾 ${esc(e.shield)}`;
return `${pos} · ${hp}${shield?' · '+shield:''}${cargo?' · '+cargo:''}`}
function card(e){const target=e.target_cell?` → 目标 ${esc(e.target_cell.join(','))}`:'',eta=e.eta_ticks==null?'':` · 预计 ${esc(e.eta_ticks)} Tick`,assignment=e.assignment||{};
const candidates=rows(e.candidate_intents,c=>`<li>${esc(action(c.action)||'—')} ${esc(direction(c.direction)||'')} ${c.target_cell?`→ ${esc(c.target_cell.join(','))}`:''} ${esc(reason(c.reason)||'')}</li>`,'无备选动作');
const nodes=rows(e.node_path,n=>`<li><b>${esc(n.node_id)}</b> · ${esc(status(n.status))} · ${esc(reason(n.reason))}</li>`,'无行为树节点');
return `<article class="unit-card ${e.status==='RUNNING'?'is-running':''} ${e.blocker?'is-blocked':''}
  <header class="unit-head"><div><b>${esc(labels[e.kind]||e.kind||'单位')}</b> <span class="alias">${esc(e.alias)}</span></div><span class="state-pill">${esc(status(e.status||'UNKNOWN'))}</span></header>
  <div class="unit-state"><span>${stateLine(e)}</span><span class="tick">决策 #${esc(e.trace_tick??'—')}</span></div>
  <div class="decision-grid"><div><small>当前任务</small><strong>${esc(task(e.task)||'空闲')}</strong><span>${esc(goal(e.goal)||'无目标')} ${assignment.role?`· ${esc(labels[assignment.role]||humanize(assignment.role,{}))}`:''}</span></div><div><small>当前动作</small><strong>${esc(action(e.action)||'—')}${target}</strong><span>${esc(reason(e.reason)||'无原因')}</span></div><div class="next-step"><small>下一步</small><strong>${esc(action(e.next_step)||task(e.next_step)||'等待新决策')}</strong><span>${esc(wake(e.wake_condition)||reason(e.blocker)||'无触发条件')}${eta}</span></div></div>
  <div class="unit-meta">${assignment.lock?`目标锁 ${esc(assignment.lock)} · `:''}${assignment.lease_until_tick!=null?`租约至 #${esc(assignment.lease_until_tick)} · `:''}${e.waited_ticks?`已等待 ${esc(e.waited_ticks)} Tick`:''}</div>
  <details><summary>查看决策链</summary><div class="trace-columns"><div><b>备选动作</b><ul>${candidates}</ul></div><div><b>行为树路径</b><ul>${nodes}</ul></div></div></details>
  <button class="neutral select-entity" data-alias="${esc(e.alias)}">用于任务</button></article>`}
function render(d){const s=d.service||{},c=d.current||{},cc=d.command_center||{};
version=Number(cc.command_version??version);
$('status').className='status '+(s.connected?'ok':'');
$('status').textContent=s.connected?'已连接 · 对战中':'服务在线 · 等待连接';
$('tick').textContent=c.tick??s.last_tick??'—';
$('resources').textContent=c.resources==null?'—':`${c.resources}/${c.resource_capacity??'—'}`;
$('mode').textContent=c.mode_label||'等待数据';
entitiesState=(cc.entities||[]).slice().sort((a,b)=>({CORE:0,WORKER:1,VANGUARD:2,RANGER:3}[a.kind]??9)-({CORE:0,WORKER:1,VANGUARD:2,RANGER:3}[b.kind]??9)||String(a.alias).localeCompare(String(b.alias)));
$('unitCount').textContent=entitiesState.length;
const taskCounts=entitiesState.reduce((out,e)=>(out[e.task||'IDLE']=(out[e.task||'IDLE']||0)+1,out),{});
const taskSummary=Object.entries(taskCounts).map(([k,n])=>`${esc(task(k)||'空闲')} ${n} 个`).join(' · ');
$('goals').innerHTML=rows(cc.goals,g=>`<div class="row"><b>${esc(goal(g.goal))}</b> <span class="tag">${esc(status(g.status))}</span> ${esc(task(g.stage)||g.stage||'')}</div>`)+(cc.tasks?.length?'':`<div class="row"><b>当前主线</b> <span class="tag">${esc(c.mode_label||'待命')}</span> ${taskSummary||'暂无单位决策'}</div>`);
renderUnitList();
renderUnitDetail();
syncEntityChoices(entitiesState);
$('tasks').innerHTML=rows(cc.tasks,t=>taskLine(t),'当前没有人工或租约任务；请从单位详情分配任务');
const mapSummary=$('mapSummary');
if(mapSummary)mapSummary.textContent=`己方 ${c.map?.friendly?.length||0} · 敌方 ${c.map?.enemies?.length||0}`;
const timeline=$('timeline');
if(timeline)timeline.innerHTML=rows(cc.timeline,t=>taskLine(t,true),'当前没有任务切换记录');
$('commands').innerHTML=rows(cc.commands,x=>`<div class="row">${esc(commandLabels[x.type]||humanize(x.type,{}))} · ${esc(status(x.status))}</div>`)}
function taskLine(t,showTick=false){const target=Array.isArray(t.target)?` · 目标 ${esc(t.target.join(','))}`:'',lease=t.lease_until_tick==null?'':` · 租约至 #${esc(t.lease_until_tick)}`;
return `<div class="row">${showTick?`<span class="tick">#${esc(t.tick)}</span> `:''}<b>${esc(t.task_id)}</b> <span class="tag">${esc(status(t.status))}</span> ${esc(goal(t.goal)||task(t.kind)||'')} ${esc(t.actor_alias||'')}${target}${lease} ${esc(reason(t.reason)||'')}</div>`}
async function api(path,method='GET',data){const h={};
if(csrf){h['X-CSRF-Token']=csrf;
h['If-Match']=`"command-version-${version}"`;
h['Idempotency-Key']='ui-'+crypto.randomUUID()}const r=await fetch(path,{method,headers:{...h,'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});
const d=await r.json();
if(d.command_version!=null)version=d.command_version;
if(!r.ok)throw Error(d.error||'请求失败');
return d}
async function login(){try{const d=await api('/api/v1/session','POST',{password:$('password').value});
csrf=d.csrf_token;
version=Number(d.command_version??0);
$('loginState').textContent='已认证；写操作将在下一 Tick 生效。';
await refreshTasks()}catch(e){$('loginState').textContent='认证失败或写功能未配置。'}}
async function emergency(type){if(!csrf){$('loginState').textContent='请先认证。';
return}if(type==='EMERGENCY_STOP'&&!confirm('确认下一 Tick 让所有当前对象安全等待？'))return;
try{await api(type==='EMERGENCY_STOP'?'/api/v1/control/emergency-stop':'/api/v1/control/resume-auto','POST',{});
$('loginState').textContent='命令已排队，等待下一次成功提交。'}catch(e){$('loginState').textContent='命令未接受：'+e.message}}
async function assign(){if(!csrf){$('taskState').textContent='请先认证。';
return}const alias=$('taskAlias').value.trim(),task_kind=$('taskKind').value,target=cell($('taskTarget').value),priority=Number($('taskPriority').value);
if(!/^entity_[0-9a-f]{12}$/.test(alias))return $('taskState').textContent='请选择当前实体。';
if(task_kind==='MOVE_TO_CELL'&&!target)return $('taskState').textContent='移动任务需要 x,y 目标。';
try{await api(`/api/v1/entities/${alias}/tasks`,'POST',{task_kind,priority,...(target?{target}:{})});
$('taskState').textContent='任务已排队，下一次成功提交后生效。';
await refreshTasks()}catch(e){$('taskState').textContent='任务未接受：'+e.message}}
function renderTasks(tasks){$('taskCommands').innerHTML=rows(tasks,t=>`<div class="row"><b>${esc(commandLabels[t.type]||humanize(t.type,{}))}</b> <span class="tag">${esc(status(t.status))}</span>${t.status==='QUEUED'?` <button class="neutral cancel-command" data-command="${esc(t.command_id)}">撤回</button>`:''}${t.status==='APPLIED'&&t.type==='ASSIGN_TASK'?` <button class="neutral cancel-entity" data-alias="${esc(t.entity_alias)}">取消任务</button>`:''}</div>`,'暂无人工任务。')}
async function refreshTasks(){if(!csrf)return;
try{renderTasks((await api('/api/v1/tasks')).tasks||[])}catch(e){$('taskCommands').textContent='任务状态读取失败：'+e.message}}
async function cancelCommand(id){try{await api(`/api/v1/commands/${encodeURIComponent(id)}`,'DELETE');
$('taskState').textContent='排队命令已撤回。';
await refreshTasks()}catch(e){$('taskState').textContent='撤回失败：'+e.message}}
async function cancelEntity(alias){try{await api(`/api/v1/entities/${encodeURIComponent(alias)}/cancel`,'POST',{});
$('taskState').textContent='取消任务已排队，下一次成功提交后生效。';
await refreshTasks()}catch(e){$('taskState').textContent='取消未接受：'+e.message}}
async function migrate(){if(!csrf)return $('taskState').textContent='请先认证。';
const target=cell($('migrationTarget').value);
if(!target)return $('taskState').textContent='迁移目标必须是 x,y。';
if(!confirm('确认排队 Core 迁移？执行时仍会重新校验安全性。'))return;
try{await api('/api/v1/core/migrations','POST',{target});
$('taskState').textContent='迁移已排队，下一次成功提交后生效。'}catch(e){$('taskState').textContent='迁移未接受：'+e.message}}
async function cancelMigration(){if(!csrf)return $('taskState').textContent='请先认证。';
try{await api('/api/v1/core/migrations','DELETE');
$('taskState').textContent='取消已排队，下一次成功提交后生效。'}catch(e){$('taskState').textContent='取消未接受：'+e.message}}
function renderPolicy(p){$('policyCurrent').textContent=postureLabels[p.posture]||'其他';
$('policyPosture').value=p.posture||'BALANCED'}async function refreshPolicy(){if(csrf)try{renderPolicy(await api('/api/v1/policy'))}catch(e){$('policyState').textContent='策略读取失败：'+e.message}}async function setPolicy(){if(!csrf)return $('policyState').textContent='请先认证。';
try{await api('/api/v1/policy','PATCH',{posture:$('policyPosture').value});
$('policyState').textContent='策略已排队，下一次成功提交后生效。'}catch(e){$('policyState').textContent='策略未接受：'+e.message}}
function selectedFrame(){return replayFrames[replayIndex]||null}
function renderReplay(){const frame=selectedFrame(),slider=$('replaySlider');if(!frame){$('replayState').textContent='等待回放快照';$('replayTick').textContent='—';return}const view={...lastPayload,current:frame.snapshot,command_center:frame.command_center||{timeline:lastPayload?.command_center?.timeline||[]}};slider.value=String(replayIndex);$('replayTick').textContent=`#${frame.tick??'—'}`;$('replayState').textContent=replayLive?'实时跟随最新 Tick':'回放已暂停';$('replayPlay').textContent=replayTimer?'⏸':'▶';window.DashboardReplay={selected:view};render(view);window.renderTacticalMap?.(view);const max=Math.max(1,replayFrames.length-1);$('replayMarkers').innerHTML=replayFrames.flatMap((item,index)=>(item.markers||[]).map(marker=>`<button class="replay-marker ${esc(marker.kind.toLowerCase())}" style="left:${index/max*100}%" data-index="${index}" title="#${esc(item.tick)} · ${esc(marker.label)}"></button>`)).join('')}
function selectReplay(index,{live=false}={}){if(!replayFrames.length)return;replayIndex=Math.max(0,Math.min(replayFrames.length-1,index));replayLive=live;renderReplay()}
function setFrames(payload){lastPayload=payload;replayFrames=(payload.replay?.frames||[]).slice().sort((a,b)=>Number(a.tick)-Number(b.tick));const latest=Math.max(0,replayFrames.length-1);if(replayLive||replayIndex>=replayFrames.length)replayIndex=latest;const slider=$('replaySlider');slider.max=String(latest);slider.value=String(replayIndex);renderReplay()}
function playReplay(){if(replayTimer){clearInterval(replayTimer);replayTimer=0;renderReplay();return}replayLive=false;replayTimer=setInterval(()=>{if(replayIndex>=replayFrames.length-1){clearInterval(replayTimer);replayTimer=0;renderReplay();return}selectReplay(replayIndex+1)},700);renderReplay()}
async function refresh(){try{const r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw Error('dashboard');setFrames(await r.json())}catch{$('status').textContent='状态获取失败'}await refreshPolicy()}$('login').onclick=login;
$('assign').onclick=assign;
$('migrate').onclick=migrate;
$('cancelMigration').onclick=cancelMigration;
$('setPolicy').onclick=setPolicy;
$('unitSearch').oninput=renderUnitList;
$('unitFilters').onclick=e=>{const b=e.target.closest('.filter-btn');
if(!b)return;
activeKind=b.dataset.kind;
document.querySelectorAll('.filter-btn').forEach(x=>x.classList.toggle('is-active',x===b));
renderUnitList()};
$('unitList').onclick=e=>{const b=e.target.closest('.unit-row');
if(b)chooseUnit(b.dataset.alias)};
$('unitDetail').onclick=e=>{const b=e.target.closest('.select-entity');
if(b)chooseUnit(b.dataset.alias)};
$('taskCommands').onclick=e=>{const b=e.target.closest('button');
if(b?.dataset.command)cancelCommand(b.dataset.command);
if(b?.dataset.alias)cancelEntity(b.dataset.alias)};
$('replaySlider').oninput=e=>selectReplay(Number(e.target.value));
$('replayStart').onclick=()=>selectReplay(0);
$('replayPrev').onclick=()=>selectReplay(replayIndex-1);
$('replayPlay').onclick=playReplay;
$('replayNext').onclick=()=>selectReplay(replayIndex+1);
$('replayLive').onclick=()=>{if(replayTimer){clearInterval(replayTimer);replayTimer=0}selectReplay(replayFrames.length-1,{live:true})};
$('replayMarkers').onclick=e=>{const marker=e.target.closest('.replay-marker');if(marker)selectReplay(Number(marker.dataset.index))};
refresh();
setInterval(refresh,3000);
