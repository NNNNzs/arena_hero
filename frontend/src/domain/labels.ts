export const KIND_LABELS: Record<string, string> = { CORE: '核心', WORKER: '工人', VANGUARD: '先锋', RANGER: '游侠' }
export const ACTION_LABELS: Record<string, string> = {
  WAIT: '等待', MOVE: '移动', HARVEST: '采集', DEPOSIT: '存入', SWEEP: '横扫', SHOOT: '射击',
  HEAL: '治疗', SPAWN: '生产', REPAIR_SHIELD: '修复护盾', START_MOVE: '开始迁移', PICKUP_BEACON: '拾取信标',
}
export const STATUS_LABELS: Record<string, string> = {
  RUNNING: '执行中', SUCCESS: '已完成', IDLE: '空闲', BLOCKED: '已阻塞', NO_INTENT: '无动作',
  SCHEDULED: '已排程', LEGACY: '传统策略', SHADOW: '观察中', STAGED: '已暂存', QUEUED: '排队中',
  APPLIED: '已生效', CANCELLED: '已取消', FAILED: '失败', UNKNOWN: '待同步',
}
export const GOAL_LABELS: Record<string, string> = {
  LEGACY_LEGACY_ACTION: '传统动作', LEGACY_RETURN: '返回核心', LEGACY_RECON: '侦察资源', LEGACY_EXPLORE: '探索前沿',
  LEGACY_BEACON: '信标任务', HARVEST_RESOURCE: '采集资源', ECONOMY: '经济运营', DEFEND: '防守', ATTACK: '进攻',
  BEACON: '信标', LEGACY_PLAN: '传统计划', CONTROL_BEACON: '控制信标',
}
export const TASK_LABELS: Record<string, string> = {
  HARVEST: '采集资源', HARVEST_RESOURCE: '采集资源', HARVEST_VISIBLE: '采集可见资源', MOVE_TO_CELL: '移动到目标',
  RETREAT_TO_CORE: '撤回核心', HOLD_POSITION: '原地待命', BEACON_ESCORT: '护送信标', LEGACY_PLAN: '传统计划',
}
export const REASON_LABELS: Record<string, string> = {
  resources_reserved_or_no_legal_core_action: '资源已保留或核心暂无合法动作', return_cargo_to_core: '将货物运回核心',
  continue_locked_resource_route: '锁定延续前往资源', reobserve_remembered_resource: '重新观察已记忆资源',
  explore_sector_frontier: '探索分区前沿', holding_defense_ring: '维持防守环', preferred_vanguard_to_beacon: '优先派先锋前往信标',
  path_to_resource: '前往资源路径', preserve_worker_cargo: '保留工人货物', current_resource: '当前资源', stale: '决策已过期',
  ok: '正常', manual_task_move: '人工移动任务', unit_retreat_to_core_heal: '撤退治疗',
  unit_retreat_to_core_heal_unsafe_fallback: '撤退治疗（风险路径）', unit_retreat_to_core_heal_shelter: '撤退治疗（掩体庇护）',
}
export const WAKE_LABELS: Record<string, string> = {
  CORE_RESOURCES_OR_LEGAL_ACTION: '核心资源或出现合法动作', NEXT_AUTHORITATIVE_TURN: '等待下一份权威状态', arrive_at_resource: '抵达资源点',
}
export const DIRECTION_LABELS: Record<string, string> = { UP: '上', DOWN: '下', LEFT: '左', RIGHT: '右' }
export const COMMAND_LABELS: Record<string, string> = {
  ASSIGN_TASK: '分配任务', ASSIGN_SQUAD: '调整编组', CANCEL: '取消任务', EMERGENCY_STOP: '紧急停机', RESUME_AUTO: '恢复自动',
  START_CORE_MIGRATION: '开始核心迁移', CANCEL_CORE_MIGRATION: '取消核心迁移', UPDATE_POLICY: '更新策略', TRIGGER_ANALYSIS: '触发分析',
}
export const POSTURE_LABELS: Record<string, string> = { BALANCED: '均衡', DEFENSIVE: '防御', ECONOMY: '经济', AGGRESSIVE: '进攻' }
export const SQUAD_TYPE_ICONS: Record<string, string> = { BASE_DEFENSE: '🛡️', EXPEDITION_BEACON: '🌟', MINING_ESCORT: '⛏️', SCOUT_RECON: '🔭' }
export const SQUAD_TYPE_LABELS: Record<string, string> = {
  BASE_DEFENSE: '基地防线', EXPEDITION_BEACON: '信标远征', MINING_ESCORT: '矿区采矿/护航', SCOUT_RECON: '迷雾侦察/巡逻',
}

export function humanize(value: unknown, mapping: Record<string, string>, fallback = '其他'): string {
  if (value == null || value === '') return ''
  return mapping[String(value)] || fallback
}

export const actionLabel = (value: unknown) => humanize(value, ACTION_LABELS)
export const statusLabel = (value: unknown) => humanize(value, STATUS_LABELS)
export const goalLabel = (value: unknown) => humanize(value, GOAL_LABELS)
export const taskLabel = (value: unknown) => humanize(value, TASK_LABELS)
export const reasonLabel = (value: unknown) => humanize(value, REASON_LABELS)
export const wakeLabel = (value: unknown) => humanize(value, WAKE_LABELS)
export const directionLabel = (value: unknown) => humanize(value, DIRECTION_LABELS)

export function parseCell(value: unknown): [number, number] | null {
  const match = String(value ?? '').trim().match(/^(-?\d+)\s*,\s*(-?\d+)$/)
  return match ? [Number(match[1]), Number(match[2])] : null
}

export function formatCell(value: unknown): string {
  return Array.isArray(value) && value.length === 2 ? `${value[0]},${value[1]}` : '—'
}

export function modeCausalityText(causality: any): string {
  const mode = causality?.mode
  if (!mode) return '尚无本 Tick 的策略判定记录。'
  const source = Array.isArray(mode.source_cell) ? ` 触发点 ${mode.source_cell.join(',')}。` : ''
  const transition = mode.changed ? `从 ${mode.previous_mode || '未知'} 切换。` : `已持续 ${mode.duration_ticks ?? '—'} Tick。`
  return `${mode.summary || mode.rule_id || '策略判定'} ${transition}${source} 退出条件：${mode.exit_condition || '下一份权威状态'}。`
}
