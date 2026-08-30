import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = path.resolve(frontendRoot, '..')
const read = file => fs.readFileSync(file, 'utf8')
const componentRoot = path.join(frontendRoot, 'src', 'components')
const componentSource = fs.readdirSync(componentRoot)
  .filter(file => file.endsWith('.vue'))
  .map(file => read(path.join(componentRoot, file)))
  .join('\n')
const backendSource = read(path.join(repoRoot, 'arena_tactic', 'dashboard.py'))
const controllerSource = read(path.join(frontendRoot, 'src', 'legacy', 'dashboard-controller.ts'))
const apiSource = read(path.join(frontendRoot, 'src', 'api', 'client.ts'))
const ids = source => new Set([...source.matchAll(/id="([^"]+)"/g)].map(match => match[1]))
const maintainedTemplate = backendSource.slice(backendSource.lastIndexOf('DASHBOARD_HTML = """'))
const expectedIds = ids(maintainedTemplate)
const actualIds = ids(componentSource)
const missingIds = [...expectedIds].filter(id => !actualIds.has(id))
assert.deepEqual(missingIds, [], `Vue components lost dashboard IDs: ${missingIds.join(', ')}`)

for (const component of ['CommandHeader.vue', 'RosterPanel.vue', 'TacticalMapPanel.vue', 'SituationPanel.vue', 'EventDrawer.vue', 'ReplayPanel.vue']) {
  assert.equal(fs.existsSync(path.join(componentRoot, component)), true, `missing Vue component: ${component}`)
}

for (const endpoint of ['/api/dashboard', '/api/events', '/api/replay', '/api/map/memory', '/api/v1/session', '/api/v1/entities/', '/api/v1/policy', '/api/v1/core/migrations']) {
  assert.equal((apiSource + controllerSource).includes(endpoint), true, `missing API contract: ${endpoint}`)
}

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

assert.doesNotMatch(componentSource + apiSource + controllerSource, /ARENA_HERO_API_KEY|Authorization:\s*Bearer|api[_-]?key\s*=/i)
console.log(`Vue dashboard contract OK: ${actualIds.size} DOM IDs, ${assetPaths.length} built assets, ${8} API paths`)
