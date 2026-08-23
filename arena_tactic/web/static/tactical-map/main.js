/* Tactical map entry point: state assembly, data reconciliation, and public API. */
(() => {
  'use strict';
  const T = window.TacticalMap = window.TacticalMap || {};
  const { FLAGS, LIMITS, VIEW_STORAGE_KEY, KIND_LABELS, clamp, isCell, sameCell, lerp, idOf, entityAliasKey, normaliseCamera, reconcileKeys, compressObservedRows, axisTicks, visibleBounds, worldCell } = T;
  const collectMovementRoutes = entities => {
    const routes = [];
    for (const entity of entities || []) {
      const data = entity?.data || entity || {};
      const action = String(data.action || '').toUpperCase();
      const from = isCell(entity?.position) ? entity.position : (isCell(data.position) ? data.position : data.current_cell);
      const target = data.target_cell;
      if (action !== 'MOVE' || !isCell(from) || !isCell(target) || sameCell(from, target)) continue;
      routes.push({
        key: String(entity?.key || idOf(data)),
        from: from.map(Number),
        target: target.map(Number),
        action,
        task: String(data.task || ''),
        reason: String(data.reason || ''),
      });
      if (routes.length >= 100) break;
    }
    return routes;
  };
  const testApi = Object.freeze({ FLAGS, LIMITS, entityAliasKey, collectMovementRoutes, niceStep: T.niceStep, axisTicks, compressObservedRows, normaliseCamera, visibleBounds, reconcileKeys, createDirtyScheduler: T.createDirtyScheduler });
  window.__TACTICAL_MAP_TEST__ = testApi;
  if (typeof document === 'undefined') return;
  const viewport = document.getElementById('map-viewport'); if (!viewport) return;
  const byId = id => document.getElementById(id), stageHost = byId('map-stage') || viewport;
  let saved = {}; try { saved = JSON.parse(localStorage.getItem(VIEW_STORAGE_KEY) || '{}'); } catch (_) {}
  const state = T.state = { viewport, byId, stageHost, axisX: byId('map-axis-x'), axisY: byId('map-axis-y'), centerReadout: byId('mapCenterCoordinate'), selectionReadout: byId('mapSelectionCoordinate'), targetReadout: byId('mapTargetCoordinate'), debugHud: byId('mapDebugHud'), debugEnabled: new URLSearchParams(window.location.search).get('debugMap') === '1', reducedMotion: window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true, camera: normaliseCamera(saved), viewInitialized: isCell(saved.anchor) || Number.isFinite(saved.scale), layers: { fog: saved.layers?.fog !== false, vision: typeof saved.layers?.vision === 'string' ? saved.layers.vision : (saved.layers?.vision === false ? 'off' : 'selected'), coordinates: saved.layers?.coordinates !== false, labels: saved.layers?.labels !== false }, size: { w: 640, h: 420 }, currentView: null, selectedKey: null, trackingKey: typeof saved.focus === 'string' ? saved.focus : null, hoveredKey: null, drag: null, pickMode: false, previewTarget: null, lockedTarget: null, persistTimer: 0, wheelPersistTimer: 0, contextLost: false, restoreTimer: 0, fallbackAttempted: false, mode: 'none', canvas: null, context2d: null, pixiApp: null, pixiWorld: null, surfaceGraphic: null, observedGraphic: null, gridGraphic: null, terrainGraphic: null, visionGraphic: null, pathGraphic: null, routeGraphic: null, unitLayer: null, radarLayer: null, radarPool: [], symbolTextures: new Map(), entityModels: new Map(), entityViews: new Map(), stats: { renderer: '未初始化', fps: 0, frameMs: 0, frames: 0, objects: 0, coordinateLabels: 0, fogSegments: 0, reason: 'startup', lastRenderedAt: 0 } };
  if (!['selected', 'all', 'off'].includes(state.layers.vision)) state.layers.vision = 'selected';
  const axisPool = (container, kind) => { if (!container) return []; return Array.from({ length: LIMITS.axisLabels }, () => { const label = document.createElement('span'); label.className = `map-axis-label map-axis-label-${kind}`; label.hidden = true; container.appendChild(label); return label; }); };
  state.xLabels = axisPool(state.axisX, 'x'); state.yLabels = axisPool(state.axisY, 'y'); if (state.debugHud) state.debugHud.hidden = !state.debugEnabled;
  const text = (node, value) => { if (node && node.textContent !== value) node.textContent = value; };
  T.currentCenterCell = s => worldCell(s, s.size.w / 2, s.size.h / 2);
  T.schedulePersist = (s, immediate = false) => { const persist = () => { if (s.persistTimer) clearTimeout(s.persistTimer); s.persistTimer = 0; try { localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify({ anchor: s.camera.anchor, scale: s.camera.scale, pan: s.camera.pan, focus: s.trackingKey, layers: s.layers })); } catch (_) {} }; if (immediate) return persist(); if (s.persistTimer) clearTimeout(s.persistTimer); s.persistTimer = setTimeout(persist, 150); };
  const syncControls = () => { if (byId('layerFog')) byId('layerFog').checked = state.layers.fog; if (byId('visionMode')) byId('visionMode').value = state.layers.vision; if (byId('layerCoordinates')) byId('layerCoordinates').checked = state.layers.coordinates; if (byId('layerLabels')) byId('layerLabels').checked = state.layers.labels; };
  T.updateCoordinateReadouts = s => { const center = T.currentCenterCell(s), selected = s.entityModels.get(s.selectedKey), target = s.previewTarget || s.lockedTarget; text(s.centerReadout, `中心 ${center[0]},${center[1]}`); text(s.selectionReadout, selected ? `选中 ${Math.round(selected.target[0])},${Math.round(selected.target[1])}` : '选中 —'); text(s.targetReadout, target ? `目标 ${target[0]},${target[1]}` : '目标 —'); };
  T.updateAxes = s => { if (s.axisX) s.axisX.hidden = !s.layers.coordinates; if (s.axisY) s.axisY.hidden = !s.layers.coordinates; if (!s.layers.coordinates) { [...s.xLabels, ...s.yLabels].forEach(node => { node.hidden = true; }); s.stats.coordinateLabels = 0; return T.updateCoordinateReadouts(s); } const bounds = visibleBounds(s.camera, s.size), xs = axisTicks(bounds.minX, bounds.maxX, s.camera.scale), ys = axisTicks(bounds.minY, bounds.maxY, s.camera.scale); s.xLabels.forEach((node, index) => { const value = xs.values[index]; node.hidden = value == null; if (value != null) { node.textContent = `X ${value}`; node.style.transform = `translate3d(${Math.round(T.worldPoint(s, [value, s.camera.anchor[1]])[0])}px,0,0)`; } }); s.yLabels.forEach((node, index) => { const value = ys.values[index]; node.hidden = value == null; if (value != null) { node.textContent = `Y ${value}`; node.style.transform = `translate3d(0,${Math.round(T.worldPoint(s, [s.camera.anchor[0], value])[1])}px,0)`; } }); s.stats.coordinateLabels = xs.values.length + ys.values.length; T.updateCoordinateReadouts(s); };
  T.updateDebugHud = s => { if (!s.debugEnabled || !s.debugHud) return; const idle = s.scheduler.state().pending ? '调度中' : '空闲'; s.debugHud.textContent = [`${s.stats.renderer} · ${idle}`, `FPS ${s.stats.fps.toFixed(1)} · ${s.stats.frameMs.toFixed(2)} ms`, `DisplayObject ${s.stats.objects} · 坐标 ${s.stats.coordinateLabels}`, `迷雾段 ${s.stats.fogSegments} · 帧 ${s.stats.frames}`, `原因 ${s.stats.reason}`].join('\n'); };
  T.updateAnimations = (s, timestamp) => { for (const model of s.entityModels.values()) { if (!model.moving) continue; const raw = clamp((timestamp - model.animationStart) / LIMITS.animationMs, 0, 1), amount = 1 - (1 - raw) ** 3; model.position = [lerp(model.start[0], model.target[0], amount), lerp(model.start[1], model.target[1], amount)]; model.moving = raw < 1; if (!model.moving) model.position = [...model.target]; } };
  const routeStart = (s, route) => s.entityModels.get(route.key)?.position || route.from;
  const drawPixiRoute = (graphic, from, to, scale, color, alpha, width) => {
    const dx = to[0] - from[0], dy = to[1] - from[1], length = Math.hypot(dx, dy);
    if (!length) return;
    const ux = dx / length, uy = dy / length;
    const period = Math.max(13 / scale, length / 120), dash = period * .62;
    graphic.lineStyle(width / scale, color, alpha);
    for (let offset = 0; offset < length; offset += period) {
      const end = Math.min(offset + dash, length);
      graphic.moveTo(from[0] + ux * offset, from[1] + uy * offset).lineTo(from[0] + ux * end, from[1] + uy * end);
    }
    const arrow = clamp(9 / scale, .2, .55), angle = Math.atan2(dy, dx), spread = Math.PI / 6;
    graphic.moveTo(...to).lineTo(to[0] - Math.cos(angle - spread) * arrow, to[1] - Math.sin(angle - spread) * arrow);
    graphic.moveTo(...to).lineTo(to[0] - Math.cos(angle + spread) * arrow, to[1] - Math.sin(angle + spread) * arrow);
    graphic.drawCircle(to[0], to[1], clamp(3.5 / scale, .1, .2));
  };
  const drawPixiRoutes = s => {
    if (s.mode !== 'pixi' || !s.pixiWorld || !window.PIXI || !s.currentView) return;
    if (!s.routeGraphic || s.routeGraphic.destroyed) {
      s.routeGraphic = new PIXI.Graphics();
      const pathIndex = s.pathGraphic && s.pathGraphic.parent === s.pixiWorld ? s.pixiWorld.getChildIndex(s.pathGraphic) : s.pixiWorld.children.length;
      s.pixiWorld.addChildAt(s.routeGraphic, Math.max(0, pathIndex));
    }
    const graphic = s.routeGraphic;
    graphic.clear();
    for (const route of s.currentView.routes || []) {
      const selected = route.key === s.selectedKey;
      drawPixiRoute(graphic, routeStart(s, route), route.target, s.camera.scale, selected ? T.COLOR_NUMBERS.resource : T.COLOR_NUMBERS.friendly, selected ? .96 : .52, selected ? 2.2 : 1.35);
    }
  };
  const drawCanvasRoutes = s => {
    if (s.mode !== 'canvas' || !s.context2d || !s.currentView) return;
    const c = s.context2d;
    c.save();
    c.lineCap = 'round';
    for (const route of s.currentView.routes || []) {
      const selected = route.key === s.selectedKey, rawStart = T.worldPoint(s, routeStart(s, route)), end = T.worldPoint(s, route.target);
      const dx = end[0] - rawStart[0], dy = end[1] - rawStart[1], length = Math.hypot(dx, dy);
      if (!length) continue;
      const ux = dx / length, uy = dy / length, trim = Math.min(12, length * .25), start = [rawStart[0] + ux * trim, rawStart[1] + uy * trim];
      c.strokeStyle = selected ? T.COLORS.resource : T.COLORS.friendly;
      c.globalAlpha = selected ? .96 : .52;
      c.lineWidth = selected ? 2.2 : 1.35;
      c.setLineDash([8, 5]);
      c.beginPath(); c.moveTo(...start); c.lineTo(...end); c.stroke();
      c.setLineDash([]);
      const angle = Math.atan2(dy, dx), arrow = 9, spread = Math.PI / 6;
      c.beginPath();
      c.moveTo(...end); c.lineTo(end[0] - Math.cos(angle - spread) * arrow, end[1] - Math.sin(angle - spread) * arrow);
      c.moveTo(...end); c.lineTo(end[0] - Math.cos(angle + spread) * arrow, end[1] - Math.sin(angle + spread) * arrow);
      c.stroke();
      c.beginPath(); c.arc(end[0], end[1], 3.5, 0, Math.PI * 2); c.stroke();
    }
    c.restore();
  };
  const baseRenderFrame = T.renderFrame;
  T.renderFrame = (s, flags, reasons, timestamp) => {
    if (s.mode === 'pixi') {
      T.updateAnimations(s, timestamp);
      drawPixiRoutes(s);
    }
    baseRenderFrame(s, flags, reasons, timestamp);
    if (s.mode === 'canvas') drawCanvasRoutes(s);
  };
  const normaliseEntity = (value, enemy = false) => { if (!value || !isCell(value.position)) return null; let kind = String(value.kind || value.unit_type || 'WORKER').toUpperCase(); if (kind === 'UNIT') kind = String(value.unit_type || 'WORKER').toUpperCase(); if (!KIND_LABELS[kind]) kind = 'WORKER'; const data = { ...value, kind, enemy: enemy || Boolean(value.enemy) }; return { key: idOf(data), data, kind, enemy: data.enemy, position: data.position.map(Number) }; };
  const updateData = view => { const map = view?.current?.map || {}, center = view?.command_center || {}, aliases = new Map((center.entities || []).map(entity => [entityAliasKey(entity.alias), entity])); const friendly = (map.friendly || []).map(entity => ({ ...entity, ...(aliases.get(entityAliasKey(entity.alias)) || {}) })).map(entity => normaliseEntity(entity)).filter(Boolean), enemies = (map.enemies || []).map(entity => normaliseEntity(entity, true)).filter(Boolean), incoming = new Map([...friendly, ...enemies].map(entity => [entity.key, entity])); const core = friendly.find(entity => entity.kind === 'CORE'), anchor = core?.position || friendly[0]?.position || enemies[0]?.position || [0, 0]; if (!state.viewInitialized) { state.camera.anchor = [...anchor]; state.camera.scale = 24; state.camera.pan = { x: 0, y: 0 }; state.viewInitialized = true; T.schedulePersist(state); } const now = performance.now(), changes = reconcileKeys(state.entityModels.keys(), incoming.keys()); for (const key of changes.removed) { state.entityModels.delete(key); T.destroyEntityView(state, key); if (state.selectedKey === key) state.selectedKey = null; if (state.trackingKey === key) state.trackingKey = null; if (state.hoveredKey === key) state.hoveredKey = null; } let moved = false; for (const [key, entity] of incoming) { let model = state.entityModels.get(key); if (!model) { model = { key, kind: entity.kind, enemy: entity.enemy, data: entity.data, position: [...entity.position], start: [...entity.position], target: [...entity.position], animationStart: now, moving: false }; state.entityModels.set(key, model); } else { model.kind = entity.kind; model.enemy = entity.enemy; model.data = entity.data; if (!sameCell(model.target, entity.position)) { model.start = [...model.position]; model.target = [...entity.position]; model.animationStart = now; model.moving = !state.reducedMotion; if (state.reducedMotion) model.position = [...entity.position]; moved ||= model.moving; } } } const tracked = state.entityModels.get(state.trackingKey); if (tracked) state.camera.anchor = [...tracked.target]; const observed = (map.observed || []).filter(isCell).slice(0, 2000).map(cell => cell.map(Number)); state.currentView = { tick: view?.current?.tick, mode: view?.current?.mode_label || '当前态势', friendly: friendly.map(e => e.data), enemies: enemies.map(e => e.data), routes: collectMovementRoutes(friendly), resources: (map.resources || []).filter(isCell).map(c => c.map(Number)), obstacles: (map.obstacles || []).filter(isCell).map(c => c.map(Number)), mined: (map.mined || []).filter(isCell).map(c => c.map(Number)), knownResources: (map.known_resources || []).filter(isCell).map(c => c.map(Number)), explored: (map.explored || []).filter(isCell).map(c => c.map(Number)), observed, observedSegments: compressObservedRows(observed), beacon: isCell(map.beacon?.position) ? { ...map.beacon, position: map.beacon.position.map(Number) } : null }; state.stats.fogSegments = state.currentView.observedSegments.length; T.updateCoordinateReadouts(state); state.scheduler.invalidate(FLAGS.DATA | FLAGS.SELECTION, `tick:${state.currentView.tick ?? 'unknown'}`); if (moved) state.scheduler.animate(LIMITS.animationMs, 'unit-movement'); };
  state.scheduler = T.createDirtyScheduler({ requestFrame: callback => requestAnimationFrame(callback), cancelFrame: frame => cancelAnimationFrame(frame), now: () => performance.now(), draw: (flags, reasons, timestamp) => T.renderFrame(state, flags, reasons, timestamp), isHidden: () => document.hidden || state.contextLost, maxFps: LIMITS.maxFps });
  syncControls(); T.bindInput(state);
  window.focusTacticalUnit = alias => { const model = [...state.entityModels.values()].find(item => String(item.data.alias) === String(alias)); if (model) T.selectEntity(state, model.key, { focus: true }); };
  window.selectTacticalUnit = alias => { const model = [...state.entityModels.values()].find(item => String(item.data.alias) === String(alias)); if (model) T.selectEntity(state, model.key); };
  window.focusTacticalCell = cell => T.focusCell(state, cell); window.zoomTacticalMap = factor => T.zoomAt(state, Number(factor) || 1, state.size.w / 2, state.size.h / 2);
  window.resetTacticalMap = () => { const core = [...state.entityModels.values()].find(model => !model.enemy && model.kind === 'CORE'); state.trackingKey = null; state.camera.anchor = core ? [...core.target] : [0, 0]; state.camera.pan = { x: 0, y: 0 }; state.camera.scale = 24; T.schedulePersist(state, true); state.scheduler.invalidate(FLAGS.CAMERA | FLAGS.SELECTION, 'reset'); };
  window.setTacticalMapLayer = (name, enabled) => { if (name === 'vision') state.layers.vision = typeof enabled === 'string' ? enabled : (enabled ? 'selected' : 'off'); else if (name in state.layers) state.layers[name] = Boolean(enabled); else return; if (!['selected', 'all', 'off'].includes(state.layers.vision)) state.layers.vision = 'selected'; syncControls(); T.schedulePersist(state); state.scheduler.invalidate(FLAGS.LAYERS | FLAGS.SELECTION, `layer:${name}`); };
  window.setTacticalVisionMode = value => window.setTacticalMapLayer('vision', value); window.setTacticalMapTargetMode = enabled => { state.pickMode = Boolean(enabled); state.previewTarget = state.pickMode ? T.currentCenterCell(state) : null; window.updateDashboardTargetMode?.(state.pickMode); T.updateCoordinateReadouts(state); state.scheduler.invalidate(FLAGS.SELECTION, state.pickMode ? 'target-mode' : 'target-cancel'); }; window.getTacticalMapStats = state.debugEnabled ? () => ({ ...state.stats, scheduler: state.scheduler.state(), entityModels: state.entityModels.size, entityViews: state.entityViews.size }) : () => null;
  window.renderTacticalMap = view => { if (!T.ensureRenderer(state)) return; updateData(view); };
  const observer = new ResizeObserver(() => T.resizeSurface(state)); observer.observe(viewport); document.addEventListener('visibilitychange', () => { if (document.hidden) state.scheduler.cancel(); else { T.resizeSurface(state); state.scheduler.invalidate(FLAGS.ALL, 'visibility-resume'); state.scheduler.resume(); } }); window.addEventListener('beforeunload', () => T.schedulePersist(state, true), { once: true }); if (window.DashboardReplay?.selected) window.renderTacticalMap(window.DashboardReplay.selected);
})();
