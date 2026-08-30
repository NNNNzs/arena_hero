import { computed, inject, provide, ref, shallowRef, type InjectionKey, type Ref } from 'vue'
import { dashboardApi, type Cell, type CommandAuth, type DashboardEntity, type DashboardPayload, type EventsPayload, type JsonObject, type MapMemoryPayload, type ReplayFrame } from '../api/client'
import { parseCell } from '../domain/labels'

export interface MapRequest {
  id: number
  kind: 'cell' | 'unit' | 'target-mode' | 'reset' | 'zoom'
  cell?: Cell
  alias?: string
  factor?: number
}

export interface DashboardStore {
  view: Ref<DashboardPayload | null>
  displayView: Readonly<Ref<DashboardPayload | null>>
  memory: Ref<MapMemoryPayload | null>
  entities: Readonly<Ref<DashboardEntity[]>>
  selectedAlias: Ref<string>
  selectedEntity: Readonly<Ref<DashboardEntity | undefined>>
  selectedFrame: Readonly<Ref<ReplayFrame | null>>
  replayFrames: Ref<ReplayFrame[]>
  replayIndex: Ref<number>
  replayLive: Ref<boolean>
  replayTimer: Ref<number>
  lastReplayTick: Ref<number>
  eventCategory: Ref<string>
  eventState: Ref<EventsPayload>
  eventDrawerOpen: Ref<boolean>
  authDialogOpen: Ref<boolean>
  csrf: Ref<string>
  commandVersion: Ref<number>
  loginState: Ref<string>
  taskState: Ref<string>
  policyState: Ref<string>
  migrationState: Ref<string>
  taskCommands: Ref<JsonObject[]>
  mapTarget: Ref<Cell | null>
  mapRequest: Ref<MapRequest | null>
  policyPosture: Ref<string>
  policyOverrides: Ref<Record<string, number | string>>
  running: Ref<boolean>
  refresh: () => Promise<void>
  start: () => void
  stop: () => void
  selectUnit: (alias: string, options?: { focusMap?: boolean }) => void
  focusCell: (cell: Cell) => void
  focusSquad: (squadId: string) => void
  setTargetFromMap: (cell: Cell) => void
  fetchEvents: () => Promise<void>
  setEventCategory: (category: string) => void
  setEventDrawer: (open: boolean) => void
  openAuthDialog: () => void
  closeAuthDialog: () => void
  clearAuth: () => void
  login: (password: string) => Promise<boolean>
  assignTask: (alias: string, taskKind: string, priority: number, target: Cell | null) => Promise<void>
  refreshTasks: () => Promise<void>
  cancelCommand: (id: string) => Promise<void>
  cancelEntity: (alias: string) => Promise<void>
  migrate: (target: Cell) => Promise<void>
  cancelMigration: () => Promise<void>
  setPolicy: (posture: string, overrides: Record<string, number>) => Promise<void>
  triggerAnalysis: () => Promise<void>
  selectReplay: (index: number, live?: boolean) => void
  toggleReplay: () => void
  loadEarlierReplay: () => Promise<void>
  setReplayLive: () => void
}

const storeKey: InjectionKey<DashboardStore> = Symbol('arena-hero-dashboard')

const entityOrder: Record<string, number> = { CORE: 0, WORKER: 1, VANGUARD: 2, RANGER: 3 }
const configFields = [
  'core_guard_vanguards', 'core_guard_rangers', 'intercept_vanguards', 'intercept_rangers',
  'resource_recheck_worker_limit', 'early_workers', 'early_vanguards', 'early_rangers',
  'patrol_radius_min', 'patrol_radius_max', 'patrol_arc_segments', 'patrol_radius_units_per_step',
  'minimum_resource_reserve', 'peacetime_resource_buffer',
]
export const COMMAND_PASSWORD_STORAGE_KEY = 'arena-hero.command-password'

export function provideDashboardStore(store: DashboardStore) {
  provide(storeKey, store)
}

export function useDashboardStore(): DashboardStore {
  const store = inject(storeKey)
  if (!store) throw new Error('DashboardStore is not provided')
  return store
}

function sortEntities(entities: DashboardEntity[] = []) {
  return [...entities].sort((left, right) => (entityOrder[left.kind || ''] ?? 9) - (entityOrder[right.kind || ''] ?? 9) || left.alias.localeCompare(right.alias))
}

export function createDashboardStore(): DashboardStore {
  const view = shallowRef<DashboardPayload | null>(null)
  const memory = shallowRef<MapMemoryPayload | null>(null)
  const selectedAlias = ref('')
  const replayFrames = ref<ReplayFrame[]>([])
  const replayIndex = ref(0)
  const replayLive = ref(true)
  const replayTimer = ref(0)
  const lastReplayTick = ref(-1)
  const eventCategory = ref('ALL')
  const eventState = ref<EventsPayload>({ events: [], category_counts: {}, total: 0 })
  const eventDrawerOpen = ref(false)
  const authDialogOpen = ref(false)
  const csrf = ref('')
  const commandVersion = ref(0)
  const loginState = ref('')
  const taskState = ref('')
  const policyState = ref('认证后可更新。')
  const migrationState = ref('')
  const taskCommands = ref<JsonObject[]>([])
  const mapTarget = ref<Cell | null>(null)
  const mapRequest = ref<MapRequest | null>(null)
  const policyPosture = ref('BALANCED')
  const policyOverrides = ref<Record<string, number | string>>({})
  const running = ref(false)
  let refreshTimer = 0
  let replayPollTimer = 0
  let memoryVersion = 0
  let requestId = 0
  let eventInFlight = false
  let refreshInFlight = false
  let policyInFlight = false
  let historyLoaded = false
  let earlierReplayInFlight = false

  const entities = computed(() => sortEntities(view.value?.command_center?.entities))
  const selectedEntity = computed(() => entities.value.find(entity => entity.alias === selectedAlias.value))
  const selectedFrame = computed(() => replayFrames.value[replayIndex.value] || null)
  const displayView = computed<DashboardPayload | null>(() => {
    if (replayLive.value || !selectedFrame.value || !view.value) return view.value
    return {
      ...view.value,
      current: selectedFrame.value.snapshot,
      command_center: selectedFrame.value.command_center || view.value.command_center,
    }
  })

  function emitMapRequest(request: Omit<MapRequest, 'id'>) {
    mapRequest.value = { ...request, id: ++requestId }
  }

  function auth(): CommandAuth | undefined {
    return csrf.value ? { csrf: csrf.value, version: commandVersion.value } : undefined
  }

  async function command<T extends JsonObject>(path: string, method: string, payload?: JsonObject): Promise<T> {
    const response = await dashboardApi.command<T>(path, method, payload, auth())
    if (response.command_version != null) commandVersion.value = Number(response.command_version)
    return response
  }

  function updateEventSummary(next: EventsPayload | undefined) {
    if (!next) return
    eventState.value = {
      ...eventState.value,
      ...next,
      events: next.events ?? eventState.value.events ?? [],
      category_counts: next.category_counts ?? eventState.value.category_counts ?? {},
      total: next.total ?? next.matched ?? eventState.value.total ?? 0,
    }
  }

  async function fetchEvents() {
    if (eventInFlight) return
    eventInFlight = true
    try { updateEventSummary(await dashboardApi.events({ limit: 50, category: eventCategory.value })) } catch { /* retry on next open */ }
    finally { eventInFlight = false }
  }

  function applyView(next: DashboardPayload) {
    view.value = next
    commandVersion.value = Number(next.command_center?.command_version ?? commandVersion.value)
    updateEventSummary(next.event_log)
    const available = entities.value
    if (!available.some(entity => entity.alias === selectedAlias.value)) selectedAlias.value = available[0]?.alias || ''
    const policy = next.policy_config
    if (policy?.posture) policyPosture.value = String(policy.posture)
    const overrides = policy?.overrides
    if (overrides && typeof overrides === 'object') policyOverrides.value = { ...overrides }
  }

  async function refresh() {
    if (refreshInFlight || document.hidden) return
    refreshInFlight = true
    try {
      const next = await dashboardApi.dashboard()
      applyView(next)
      if (next.map_memory_version && next.map_memory_version !== memoryVersion) void fetchMapMemory()
      if (replayLive.value) void refreshReplay()
    } catch { /* status is rendered as degraded by the last payload */ }
    finally { refreshInFlight = false }
  }

  async function fetchMapMemory() {
    try {
      const next = await dashboardApi.mapMemory()
      memoryVersion = Number(next.version || 0)
      memory.value = next
    } catch { /* map remains usable with current-turn data */ }
  }

  function mergeReplayFrames(nextFrames: ReplayFrame[], keepWindow = false) {
    if (!nextFrames.length) return
    const previousTick = selectedFrame.value?.tick
    const byTick = new Map(replayFrames.value.map(frame => [frame.tick, frame]))
    nextFrames.forEach(frame => byTick.set(frame.tick, frame))
    replayFrames.value = [...byTick.values()].sort((a, b) => Number(a.tick) - Number(b.tick)).slice(keepWindow ? 0 : -200)
    const latest = Math.max(0, replayFrames.value.length - 1)
    if (replayLive.value || replayIndex.value >= replayFrames.value.length) replayIndex.value = latest
    else if (previousTick != null) {
      const retained = replayFrames.value.findIndex(frame => frame.tick === previousTick)
      replayIndex.value = retained >= 0 ? retained : Math.min(replayIndex.value, latest)
    }
    const ticks = replayFrames.value.map(frame => Number(frame.tick)).filter(Number.isFinite)
    lastReplayTick.value = ticks.length ? Math.max(...ticks) : -1
  }

  async function refreshReplay() {
    if (document.hidden || !view.value || !replayLive.value) return
    try { mergeReplayFrames((await dashboardApi.replay({ limit: 8, fromTick: lastReplayTick.value })).frames || []) } catch { /* next poll retries */ }
  }

  async function fetchReplayHistory() {
    try { mergeReplayFrames((await dashboardApi.replay({ limit: 32 })).frames || []) } catch { /* empty replay is valid before first Tick */ }
  }

  async function loadEarlierReplay() {
    if (earlierReplayInFlight || !replayFrames.value.length) return
    earlierReplayInFlight = true
    try {
      const earliest = Number(replayFrames.value[0].tick)
      const frames = (await dashboardApi.replay({ limit: 32, toTick: earliest - 1 })).frames || []
      if (frames.length) mergeReplayFrames(frames, true)
    } finally { earlierReplayInFlight = false }
  }

  function selectReplay(index: number, live = false) {
    if (!replayFrames.value.length) return
    replayIndex.value = Math.max(0, Math.min(replayFrames.value.length - 1, index))
    replayLive.value = live
    if (!live && replayIndex.value === 0) void loadEarlierReplay()
    if (!live && !historyLoaded) { historyLoaded = true; void fetchReplayHistory() }
  }

  function stopReplay() {
    if (replayTimer.value) window.clearInterval(replayTimer.value)
    replayTimer.value = 0
  }

  function toggleReplay() {
    if (replayTimer.value) { stopReplay(); return }
    if (!replayFrames.value.length) return
    replayLive.value = false
    if (!historyLoaded) { historyLoaded = true; void fetchReplayHistory() }
    replayTimer.value = window.setInterval(() => {
      if (document.hidden || replayIndex.value >= replayFrames.value.length - 1) { stopReplay(); return }
      selectReplay(replayIndex.value + 1)
    }, 700)
  }

  function setReplayLive() {
    stopReplay()
    historyLoaded = false
    selectReplay(replayFrames.value.length - 1, true)
  }

  function selectUnit(alias: string, options: { focusMap?: boolean } = {}) {
    if (!entities.value.some(entity => entity.alias === alias)) return
    selectedAlias.value = alias
    if (options.focusMap !== false) emitMapRequest({ kind: 'unit', alias })
  }

  function focusCell(cell: Cell) { emitMapRequest({ kind: 'cell', cell }) }

  function focusSquad(squadId: string) {
    const squad = (view.value?.squads?.squads || []).find(item => item.id === squadId)
    if (!squad) return
    const details = squad.causality || {}
    const target = details.coordination_target || details.centroid || squad.target
    if (Array.isArray(target) && target.length === 2) focusCell(target as Cell)
    const first = (details.member_aliases || squad.members || []).map((item: any) => typeof item === 'string' ? item : item.alias).find(Boolean)
    if (first) selectUnit(String(first), { focusMap: false })
  }

  function setTargetFromMap(cell: Cell) {
    mapTarget.value = cell
    taskState.value = `已从地图锁定目标 ${cell.join(',')}；认证后可排队任务。`
  }

  function setEventCategory(category: string) {
    eventCategory.value = category
    void fetchEvents()
  }

  function setEventDrawer(open: boolean) {
    eventDrawerOpen.value = open
    if (open) void fetchEvents()
  }

  function readCachedPassword(): string {
    try { return window.localStorage.getItem(COMMAND_PASSWORD_STORAGE_KEY) || '' } catch { return '' }
  }

  function cachePassword(password: string) {
    try { window.localStorage.setItem(COMMAND_PASSWORD_STORAGE_KEY, password) } catch { /* private browsing may deny storage */ }
  }

  function openAuthDialog() { authDialogOpen.value = true }
  function closeAuthDialog() { authDialogOpen.value = false }
  function clearAuth() {
    csrf.value = ''
    taskCommands.value = []
    commandVersion.value = 0
    loginState.value = '已清除本机口令缓存。'
  }

  async function login(password: string): Promise<boolean> {
    if (!password.trim()) {
      loginState.value = '请输入管理员口令。'
      return false
    }
    try {
      const response = await command<JsonObject>('/api/v1/session', 'POST', { password })
      csrf.value = String(response.csrf_token || '')
      commandVersion.value = Number(response.command_version || 0)
      loginState.value = '已认证；写操作将在下一 Tick 生效。'
      await Promise.all([refreshTasks(), refreshPolicy(true)])
      return true
    } catch {
      csrf.value = ''
      loginState.value = '认证失败或写功能未配置。'
      return false
    }
  }

  async function assignTask(alias: string, taskKind: string, priority: number, target: Cell | null) {
    if (!csrf.value) { taskState.value = '请先认证。'; return }
    if (!/^entity_[0-9a-f]{12}$/.test(alias)) { taskState.value = '请选择当前实体。'; return }
    if (taskKind === 'MOVE_TO_CELL' && !target) { taskState.value = '移动任务需要 x,y 目标。'; return }
    try {
      await command(`/api/v1/entities/${encodeURIComponent(alias)}/tasks`, 'POST', { task_kind: taskKind, priority, ...(target ? { target } : {}) })
      taskState.value = '任务已排队，下一次成功提交后生效。'
      await refreshTasks()
    } catch (error: any) { taskState.value = `任务未接受：${error.message}` }
  }

  async function refreshTasks() {
    if (!csrf.value) return
    try { taskCommands.value = (await command<JsonObject>('/api/v1/tasks', 'GET')).tasks || [] }
    catch (error: any) { taskState.value = `任务状态读取失败：${error.message}` }
  }

  async function cancelCommand(id: string) {
    try { await command(`/api/v1/commands/${encodeURIComponent(id)}`, 'DELETE'); taskState.value = '排队命令已撤回。'; await refreshTasks() }
    catch (error: any) { taskState.value = `撤回失败：${error.message}` }
  }

  async function cancelEntity(alias: string) {
    try { await command(`/api/v1/entities/${encodeURIComponent(alias)}/cancel`, 'POST', {}); taskState.value = '取消任务已排队，下一次成功提交后生效。'; await refreshTasks() }
    catch (error: any) { taskState.value = `取消未接受：${error.message}` }
  }

  async function migrate(target: Cell) {
    if (!csrf.value) { taskState.value = '请先认证。'; return }
    try { await command('/api/v1/core/migrations', 'POST', { target }); taskState.value = '迁移已排队，下一次成功提交后生效。' }
    catch (error: any) { taskState.value = `迁移未接受：${error.message}` }
  }

  async function cancelMigration() {
    if (!csrf.value) { taskState.value = '请先认证。'; return }
    try { await command('/api/v1/core/migrations', 'DELETE'); taskState.value = '取消已排队，下一次成功提交后生效。' }
    catch (error: any) { taskState.value = `取消未接受：${error.message}` }
  }

  async function refreshPolicy(force = false) {
    if (!csrf.value || policyInFlight || (!force && !view.value)) return
    policyInFlight = true
    try {
      const policy = await command<JsonObject>('/api/v1/policy', 'GET')
      if (policy.posture) policyPosture.value = String(policy.posture)
      policyState.value = ''
    } catch (error: any) { policyState.value = `策略读取失败：${error.message}` }
    finally { policyInFlight = false }
  }

  async function setPolicy(posture: string, overrides: Record<string, number>) {
    if (!csrf.value) { policyState.value = '请先认证。'; return }
    try {
      await command('/api/v1/policy', 'PATCH', { posture, ...overrides })
      policyState.value = '策略已排队，下一次成功提交后生效。'
      await refreshPolicy(true)
    } catch (error: any) { policyState.value = `策略未接受：${error.message}` }
  }

  async function triggerAnalysis() {
    if (!csrf.value) { migrationState.value = '请先认证。'; return }
    try { await command('/api/v1/commands', 'POST', { type: 'TRIGGER_ANALYSIS', payload: { task_name: 'resource_density_scan' } }); migrationState.value = '分析扫描已触发，结果将在下一 Tick 更新。' }
    catch (error: any) { migrationState.value = `触发失败：${error.message}` }
  }

  function start() {
    if (running.value) return
    running.value = true
    void refresh()
    const cachedPassword = readCachedPassword()
    if (cachedPassword) void login(cachedPassword)
    void fetchMapMemory()
    void fetchReplayHistory()
    refreshTimer = window.setInterval(() => void refresh(), 3000)
    replayPollTimer = window.setInterval(() => void refreshReplay(), 3000)
  }

  function stop() {
    running.value = false
    stopReplay()
    if (refreshTimer) window.clearInterval(refreshTimer)
    if (replayPollTimer) window.clearInterval(replayPollTimer)
    refreshTimer = 0
    replayPollTimer = 0
  }

  const store: DashboardStore = {
    view, displayView, memory, entities, selectedAlias, selectedEntity, selectedFrame, replayFrames, replayIndex, replayLive,
    replayTimer, lastReplayTick, eventCategory, eventState, eventDrawerOpen, authDialogOpen, csrf, commandVersion, loginState, taskState,
    policyState, migrationState, taskCommands, mapTarget, mapRequest, policyPosture, policyOverrides, running, refresh, start, stop,
    selectUnit, focusCell, focusSquad, setTargetFromMap, fetchEvents, setEventCategory, setEventDrawer, openAuthDialog, closeAuthDialog, clearAuth,
    login: async (password: string) => {
      cachePassword(password)
      return login(password)
    }, assignTask,
    refreshTasks, cancelCommand, cancelEntity, migrate, cancelMigration, setPolicy, triggerAnalysis, selectReplay,
    toggleReplay, loadEarlierReplay, setReplayLive,
  }
  return store
}

export function currentConfigOverrides(store: DashboardStore) {
  const overrides = store.policyOverrides.value
  return Object.fromEntries(configFields.map(field => [field, overrides[field] ?? '']))
}

export function cellFromText(value: string): Cell | null { return parseCell(value) }
