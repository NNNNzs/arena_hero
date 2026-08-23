/* Shared map constants and data-layer helpers. Loaded before the map runtime. */
(() => {
  'use strict';
  const TacticalMap = window.TacticalMap = window.TacticalMap || {};
  TacticalMap.FLAGS = Object.freeze({ DATA: 1, CAMERA: 2, SELECTION: 4, LAYERS: 8, ANIMATION: 16, RESIZE: 32, ALL: 63 });
  TacticalMap.LIMITS = Object.freeze({ minScale: 8, maxScale: 56, axisLabels: 16, radarMarkers: 10, animationMs: 180, maxFps: 30 });
  TacticalMap.COLORS = Object.freeze({ bg: '#071016', observed: '#10232C', grid: '#31515E', friendly: '#58C9BE', resource: '#D9AA55', enemy: '#E56B73', text: '#D6E2E4', muted: '#829BA6', obstacle: '#263B45', mined: '#5A3E28', explored: '#0E1A22' });
  TacticalMap.COLOR_NUMBERS = Object.freeze({ bg: 0x071016, observed: 0x10232c, grid: 0x31515e, friendly: 0x58c9be, resource: 0xd9aa55, enemy: 0xe56b73, text: 0xd6e2e4, obstacle: 0x263b45, mined: 0x5a3e28, explored: 0x0e1a22 });
  TacticalMap.KIND_LABELS = Object.freeze({ CORE: '核心', WORKER: '工人', VANGUARD: '先锋', RANGER: '游侠' });
  TacticalMap.KIND_HP = Object.freeze({ CORE: 5, WORKER: 2, VANGUARD: 4, RANGER: 2 });
  TacticalMap.VIEW_STORAGE_KEY = 'arena-hero:tactical-map:view:v2';
  TacticalMap.clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
  TacticalMap.isCell = value => Array.isArray(value) && value.length === 2 && value.every(Number.isFinite);
  TacticalMap.sameCell = (a, b) => TacticalMap.isCell(a) && TacticalMap.isCell(b) && a[0] === b[0] && a[1] === b[1];
  TacticalMap.lerp = (a, b, amount) => a + (b - a) * amount;
  TacticalMap.manhattan = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]);
  TacticalMap.entityAliasKey = value => String(value || '').replace(/^entity_/, '');
  TacticalMap.idOf = value => String(value?.alias || value?.id || `${value?.enemy ? 'enemy' : 'object'}:${value?.kind}:${value?.position}`);

  TacticalMap.compressObservedRows = cells => {
    const rows = new Map();
    for (const cell of cells || []) {
      if (!TacticalMap.isCell(cell) || !Number.isInteger(cell[0]) || !Number.isInteger(cell[1])) continue;
      if (!rows.has(cell[1])) rows.set(cell[1], new Set());
      rows.get(cell[1]).add(cell[0]);
    }
    const segments = [];
    for (const y of [...rows.keys()].sort((a, b) => a - b)) {
      const values = [...rows.get(y)].sort((a, b) => a - b);
      if (!values.length) continue;
      let start = values[0], end = values[0];
      for (const x of values.slice(1)) {
        if (x === end + 1) end = x;
        else { segments.push([start, end, y]); start = x; end = x; }
      }
      segments.push([start, end, y]);
    }
    return segments;
  };
})();
