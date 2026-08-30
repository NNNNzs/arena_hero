import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import vm from 'node:vm';

const browser = { location: { search: '' } };
const context = vm.createContext({ window: browser, console, URLSearchParams });
for (const name of ['layers.js', 'camera.js', 'radar.js', 'renderers.js', 'input.js', 'main.js']) {
  const source = fs.readFileSync(path.resolve('frontend/src/map/engine', name), 'utf8');
  vm.runInContext(source, context, { filename: `tactical-map/${name}` });
}
const utils = browser.__TACTICAL_MAP_TEST__;
const plain = value => JSON.parse(JSON.stringify(value));

test('adaptive rulers stay sparse at every supported zoom', () => {
  for (const scale of [8, 12, 24, 40, 56]) {
    const halfSpan = 1440 / scale / 2;
    const ticks = utils.axisTicks(-halfSpan, halfSpan, scale);
    assert.ok(ticks.values.length <= 16);
    for (let index = 1; index < ticks.values.length; index += 1) {
      assert.ok((ticks.values[index] - ticks.values[index - 1]) * scale >= 64);
    }
  }
  assert.equal(utils.LIMITS.axisLabels * 2, 32);
});

test('observed cells are compressed into contiguous row segments', () => {
  const segments = utils.compressObservedRows([
    [0, 0], [1, 0], [2, 0], [2, 0], [4, 0], [1, 1], [3, 1], [4, 1],
  ]);
  assert.deepEqual(plain(segments), [[0, 2, 0], [4, 4, 0], [1, 1, 1], [3, 4, 1]]);
});

test('map aliases reconcile with command-center entity aliases', () => {
  assert.equal(utils.entityAliasKey('268fa23356e6'), '268fa23356e6');
  assert.equal(utils.entityAliasKey('entity_268fa23356e6'), '268fa23356e6');
});

test('active move decisions become bounded tactical route overlays', () => {
  const routes = utils.collectMovementRoutes([
    {
      key: 'vanguard-1',
      position: [2, 3],
      data: {
        alias: 'a1',
        action: 'MOVE',
        target_cell: [8, 5],
        task: 'LEGACY_PATROL',
        reason: 'patrol_outer_ring',
      },
    },
    {
      key: 'worker-waiting',
      position: [0, 0],
      data: { alias: 'b2', action: 'WAIT', target_cell: [4, 4] },
    },
    {
      key: 'ranger-no-target',
      position: [1, 1],
      data: { alias: 'c3', action: 'MOVE', target_cell: null },
    },
  ]);

  assert.deepEqual(plain(routes), [{
    key: 'vanguard-1',
    from: [2, 3],
    target: [8, 5],
    action: 'MOVE',
    task: 'LEGACY_PATROL',
    reason: 'patrol_outer_ring',
  }]);
});

test('movement routes ignore a target that is already the current cell', () => {
  assert.deepEqual(plain(utils.collectMovementRoutes([
    { key: 'worker-1', position: [4, 4], data: { action: 'MOVE', target_cell: [4, 4] } },
  ])), []);
});

test('a 2000-cell observed stress field does not create per-cell geometry', () => {
  const cells = [];
  for (let y = 0; y < 50; y += 1) {
    for (let x = 0; x < 40; x += 1) cells.push([x, y]);
  }
  assert.equal(cells.length, 2000);
  assert.equal(utils.compressObservedRows(cells).length, 50);
});

test('camera state clamps unsafe zoom while preserving pan and anchor', () => {
  assert.deepEqual(plain(utils.normaliseCamera({ anchor: [8, -3], scale: 1, pan: { x: 17, y: -9 } })), {
    anchor: [8, -3], scale: 4, pan: { x: 17, y: -9 },
  });
  assert.equal(utils.normaliseCamera({ scale: 999 }).scale, 56);
});

test('visible bounds follow camera pan without expanding the world', () => {
  const bounds = utils.visibleBounds(
    { anchor: [10, 20], scale: 10, pan: { x: 100, y: -50 } },
    { w: 400, h: 200 },
  );
  assert.deepEqual(plain(bounds), { minX: -20, maxX: 20, minY: 15, maxY: 35 });
});

test('entity reconciliation remains bounded across 500 replay switches', () => {
  let existing = [];
  for (let frame = 0; frame < 500; frame += 1) {
    const incoming = Array.from({ length: 40 }, (_, index) => `unit-${frame % 7}-${index}`);
    const diff = utils.reconcileKeys(existing, incoming);
    assert.equal(diff.added.length + diff.kept.length, incoming.length);
    assert.equal(diff.removed.length + diff.kept.length, existing.length);
    existing = incoming;
  }
  assert.equal(existing.length, 40);
});

test('dirty scheduler coalesces invalidations and becomes idle after short animation', () => {
  let clock = 0;
  let nextId = 1;
  const pending = new Map();
  const draws = [];
  const scheduler = utils.createDirtyScheduler({
    requestFrame(callback) { const id = nextId++; pending.set(id, callback); return id; },
    cancelFrame(id) { pending.delete(id); },
    now: () => clock,
    draw: (flags, reasons, timestamp) => draws.push({ flags, reasons, timestamp }),
    maxFps: 30,
  });
  const runFrame = timestamp => {
    clock = timestamp;
    const entry = pending.entries().next().value;
    if (!entry) return;
    pending.delete(entry[0]);
    entry[1](timestamp);
  };

  for (let index = 0; index < 100; index += 1) scheduler.invalidate(utils.FLAGS.DATA, 'same-tick');
  assert.equal(pending.size, 1);
  runFrame(0);
  assert.equal(draws.length, 1);
  assert.equal(pending.size, 0);

  scheduler.animate(180);
  for (let timestamp = 16; timestamp <= 240; timestamp += 16) runFrame(timestamp);
  assert.ok(draws.length <= 7, `expected no more than six animation renders plus initial render, got ${draws.length}`);
  assert.equal(pending.size, 0);
  assert.equal(scheduler.state().pending, false);
});
