/* Off-screen entity edge radar for the WebGL renderer. */
(() => {
  'use strict';
  const TacticalMap = window.TacticalMap = window.TacticalMap || {};
  const { COLORS, COLOR_NUMBERS, LIMITS, clamp, isCell, manhattan, visibleBounds, worldCell } = TacticalMap;
  TacticalMap.farMarkers = state => {
    if (!state.currentView) return [];
    const bounds = visibleBounds(state.camera, state.size, 12), origin = worldCell(state, state.size.w / 2, state.size.h / 2), candidates = [];
    for (const enemy of state.currentView.enemies) if (isCell(enemy.position) && !TacticalMap.isInBounds(enemy.position, bounds)) candidates.push({ position: enemy.position, type: 'enemy', label: '敌方' });
    for (const resource of state.currentView.resources) if (!TacticalMap.isInBounds(resource, bounds)) candidates.push({ position: resource, type: 'resource', label: '资源' });
    if (state.currentView.beacon && !TacticalMap.isInBounds(state.currentView.beacon.position, bounds)) candidates.push({ position: state.currentView.beacon.position, type: 'beacon', label: '信标' });
    return candidates.sort((a, b) => manhattan(origin, a.position) - manhattan(origin, b.position)).slice(0, LIMITS.radarMarkers);
  };
  TacticalMap.radarPosition = (state, cell) => {
    const origin = worldCell(state, state.size.w / 2, state.size.h / 2), dx = cell[0] - origin[0], dy = cell[1] - origin[1];
    const halfWidth = Math.max(20, state.size.w / 2 - 28), halfHeight = Math.max(20, state.size.h / 2 - 28);
    const multiplier = Math.min(dx ? halfWidth / Math.abs(dx) : Infinity, dy ? halfHeight / Math.abs(dy) : Infinity), factor = Number.isFinite(multiplier) ? multiplier : 0;
    return { x: state.size.w / 2 + dx * factor, y: state.size.h / 2 + dy * factor, angle: Math.atan2(dy, dx), distance: Math.round(Math.hypot(dx, dy)) };
  };
  TacticalMap.createRadarPool = state => {
    state.radarPool = [];
    for (let index = 0; index < LIMITS.radarMarkers; index += 1) {
      const arrow = new PIXI.Graphics(); arrow.beginFill(0xffffff).drawPolygon([9, 0, -7, -5, -3, 0, -7, 5]).endFill(); arrow.visible = false;
      const label = new PIXI.Text('', { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 9, fill: COLOR_NUMBERS.text }); label.visible = false;
      state.radarLayer.addChild(arrow, label); state.radarPool.push({ arrow, label });
    }
  };
  TacticalMap.drawRadarPixi = state => {
    const markers = TacticalMap.farMarkers(state);
    state.radarPool.forEach((entry, index) => {
      const marker = markers[index]; if (!marker) { entry.arrow.visible = false; entry.label.visible = false; return; }
      const point = TacticalMap.radarPosition(state, marker.position), color = marker.type === 'enemy' ? COLOR_NUMBERS.enemy : (marker.type === 'resource' ? COLOR_NUMBERS.resource : COLOR_NUMBERS.friendly);
      entry.arrow.visible = true; entry.arrow.tint = color; entry.arrow.position.set(point.x, point.y); entry.arrow.rotation = point.angle;
      entry.label.visible = true; entry.label.text = `${marker.label} ${point.distance}格`; entry.label.position.set(clamp(point.x + 8, 6, state.size.w - 74), clamp(point.y + 7, 8, state.size.h - 18));
    });
  };
})();
