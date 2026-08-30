import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = path.resolve(frontendRoot, '..')
const read = file => fs.readFileSync(file, 'utf8')
const componentRoot = path.join(frontendRoot, 'src', 'components')
const componentSource = fs.readdirSync(componentRoot).filter(file => file.endsWith('.vue')).map(file => read(path.join(componentRoot, file))).join('\n')
const apiSource = read(path.join(frontendRoot, 'src', 'api', 'client.ts'))
const stateSource = read(path.join(frontendRoot, 'src', 'state', 'dashboard.ts'))
const mapSource = fs.readdirSync(path.join(frontendRoot, 'src', 'map', 'engine')).map(file => read(path.join(frontendRoot, 'src', 'map', 'engine', file))).join('\n')
const ids = source => new Set([...source.matchAll(/id="([^"]+)"/g)].map(match => match[1]))
const actualIds = ids(componentSource)
const requiredIds = [
  'tick', 'resources', 'mode', 'modeCausality', 'unitCount', 'status', 'unitFilterCount', 'rosterTabs', 'tabUnitsBtn', 'tabSquadsBtn',
  'tabUnitsView', 'unitSearch', 'unitFilters', 'unitList', 'tabSquadsView', 'squadTotalStats', 'squadList', 'unitDetail', 'resourceInfo',
  'goals', 'tasks', 'password', 'login', 'loginState', 'taskAlias', 'taskKind', 'taskPriority', 'taskTarget', 'assign', 'taskState', 'taskCommands',
  'migrationTarget', 'migrate', 'cancelMigration', 'policyCurrent', 'policyPosture', 'setPolicy', 'policyState', 'policyConfig', 'migrationAnalysis',
  'triggerAnalysis', 'migrationState', 'chunkSaturation', 'commands', 'timeline', 'mapModeBadge', 'mapSummary', 'rendererStatus', 'mapZoomIn',
  'mapZoomOut', 'mapReset', 'layerFog', 'visionMode', 'layerCoordinates', 'layerLabels', 'mapPickTarget', 'map-viewport', 'map-stage', 'map-axis-x',
  'map-axis-y', 'mapCenterCoordinate', 'mapCursor', 'mapSelectionCoordinate', 'mapTargetCoordinate', 'mapTargetMode', 'mapRendererState', 'mapDebugHud',
  'eventDrawer', 'eventDrawerClose', 'eventFilters', 'eventCountAllDrawer', 'eventCountCombat', 'eventCountHarvest', 'eventCountOps', 'eventCountAnomaly',
  'eventLogList', 'replayState', 'replayTick', 'replayTrack', 'replaySlider', 'replayMarkers', 'replayHover', 'replayStart', 'replayPrev', 'replayPlay',
  'replayNext', 'replayLoadEarlier', 'replayLive', 'eventDrawerToggle', 'eventCountAll',
]
const missingIds = requiredIds.filter(id => !actualIds.has(id) && !componentSource.includes(id))
assert.deepEqual(missingIds, [], `Vue components lost dashboard IDs: ${missingIds.join(', ')}`)
for (const component of ['CommandHeader.vue', 'RosterPanel.vue', 'TacticalMapPanel.vue', 'SituationPanel.vue', 'EventDrawer.vue', 'ReplayPanel.vue']) {
  assert.equal(fs.existsSync(path.join(componentRoot, component)), true, `missing Vue component: ${component}`)
}
assert.equal(fs.existsSync(path.join(frontendRoot, 'src', 'legacy', 'dashboard-controller.ts')), false, 'legacy dashboard controller still exists')
for (const endpoint of ['/api/dashboard', '/api/events', '/api/replay', '/api/map/memory', '/api/v1/session', '/api/v1/entities/', '/api/v1/policy', '/api/v1/core/migrations']) {
  assert.equal((apiSource + stateSource).includes(endpoint), true, `missing API contract: ${endpoint}`)
}
assert.match(mapSource, /renderTacticalMap/)
assert.doesNotMatch(componentSource + apiSource + stateSource, /ARENA_HERO_API_KEY|Authorization:\s*Bearer|api[_-]?key\s*=/i)

const buildRoot = path.join(repoRoot, 'arena_tactic', 'web', 'static', 'app')
const builtIndex = path.join(buildRoot, 'index.html')
assert.equal(fs.existsSync(builtIndex), true, 'Vue build output is missing; run pnpm build first')
const indexSource = read(builtIndex)
const assetPaths = [...indexSource.matchAll(/(?:src|href)="(\/static\/app\/assets\/[^"?]+)"/g)].map(match => match[1])
assert.ok(assetPaths.length >= 2, 'built index does not reference JS and CSS assets')
for (const assetPath of assetPaths) {
  const relativeAsset = assetPath.replace(/^\/static\/app\//, 'arena_tactic/web/static/app/')
  assert.equal(fs.existsSync(path.join(repoRoot, relativeAsset)), true, `missing built asset: ${assetPath}`)
}
console.log(`Vue dashboard contract OK: ${actualIds.size} DOM IDs, ${assetPaths.length} built assets, ${requiredIds.length} required IDs`)
