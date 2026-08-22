/* Arena Hero tactical map — retained PixiJS scene with an on-demand Canvas fallback. */
(() => {
  'use strict';

  const FLAGS = Object.freeze({
    DATA: 1,
    CAMERA: 2,
    SELECTION: 4,
    LAYERS: 8,
    ANIMATION: 16,
    RESIZE: 32,
    ALL: 63,
  });
  const LIMITS = Object.freeze({
    minScale: 8,
    maxScale: 56,
    axisLabels: 16,
    radarMarkers: 10,
    animationMs: 180,
    maxFps: 30,
  });
  const COLORS = Object.freeze({
    bg: '#071016',
    observed: '#10232C',
    grid: '#31515E',
    friendly: '#58C9BE',
    resource: '#D9AA55',
    enemy: '#E56B73',
    text: '#D6E2E4',
    muted: '#829BA6',
    obstacle: '#263B45',
  });
  const COLOR_NUMBERS = Object.freeze({
    bg: 0x071016,
    observed: 0x10232c,
    grid: 0x31515e,
    friendly: 0x58c9be,
    resource: 0xd9aa55,
    enemy: 0xe56b73,
    text: 0xd6e2e4,
    obstacle: 0x263b45,
  });
  const KIND_LABELS = Object.freeze({ CORE: '核心', WORKER: '工人', VANGUARD: '先锋', RANGER: '游侠' });
  const KIND_HP = Object.freeze({ CORE: 5, WORKER: 2, VANGUARD: 4, RANGER: 2 });
  const VIEW_STORAGE_KEY = 'arena-hero:tactical-map:view:v2';

  const clamp = (value, minimum, maximum) => Math.max(minimum, Math.min(maximum, value));
  const isCell = value => Array.isArray(value) && value.length === 2 && value.every(Number.isFinite);
  const entityAliasKey = value => String(value || '').replace(/^entity_/, '');
  const idOf = value => String(value?.alias || value?.id || `${value?.enemy ? 'enemy' : 'object'}:${value?.kind}:${value?.position}`);
  const manhattan = (a, b) => Math.abs(a[0] - b[0]) + Math.abs(a[1] - b[1]);
  const sameCell = (a, b) => isCell(a) && isCell(b) && a[0] === b[0] && a[1] === b[1];
  const lerp = (a, b, amount) => a + (b - a) * amount;

  function niceStep(minimum) {
    if (!Number.isFinite(minimum) || minimum <= 1) return 1;
    const power = 10 ** Math.floor(Math.log10(minimum));
    for (const factor of [1, 2, 5, 10]) {
      const candidate = factor * power;
      if (candidate >= minimum) return candidate;
    }
    return power * 10;
  }

  function axisTicks(minimum, maximum, scale, { maxLabels = LIMITS.axisLabels, minPixels = 64 } = {}) {
    const lower = Math.min(minimum, maximum);
    const upper = Math.max(minimum, maximum);
    const span = Math.max(0, upper - lower);
    const required = Math.max(minPixels / Math.max(0.001, scale), span / Math.max(1, maxLabels - 1));
    const step = niceStep(required);
    const values = [];
    const first = Math.ceil(lower / step) * step;
    for (let value = first; value <= upper + step * 1e-9 && values.length < maxLabels; value += step) {
      values.push(Object.is(value, -0) ? 0 : Math.round(value));
    }
    return { step, values };
  }

  function compressObservedRows(cells) {
    const rows = new Map();
    for (const cell of cells || []) {
      if (!isCell(cell) || !Number.isInteger(cell[0]) || !Number.isInteger(cell[1])) continue;
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
  }

  function normaliseCamera(value, fallback = {}) {
    const source = value && typeof value === 'object' ? value : {};
    const fallbackAnchor = isCell(fallback.anchor) ? fallback.anchor : [0, 0];
    const fallbackPan = fallback.pan && Number.isFinite(fallback.pan.x) && Number.isFinite(fallback.pan.y)
      ? fallback.pan : { x: 0, y: 0 };
    return {
      anchor: isCell(source.anchor) ? source.anchor.map(Number) : [...fallbackAnchor],
      scale: clamp(Number.isFinite(source.scale) ? source.scale : (fallback.scale || 24), LIMITS.minScale, LIMITS.maxScale),
      pan: source.pan && Number.isFinite(source.pan.x) && Number.isFinite(source.pan.y)
        ? { x: source.pan.x, y: source.pan.y } : { ...fallbackPan },
    };
  }

  function visibleBounds(camera, size, marginPixels = 0) {
    const halfWidth = size.w / 2;
    const halfHeight = size.h / 2;
    return {
      minX: camera.anchor[0] + (-marginPixels - halfWidth - camera.pan.x) / camera.scale,
      maxX: camera.anchor[0] + (size.w + marginPixels - halfWidth - camera.pan.x) / camera.scale,
      minY: camera.anchor[1] + (-marginPixels - halfHeight - camera.pan.y) / camera.scale,
      maxY: camera.anchor[1] + (size.h + marginPixels - halfHeight - camera.pan.y) / camera.scale,
    };
  }

  function reconcileKeys(existingKeys, incomingKeys) {
    const existing = new Set(existingKeys || []);
    const incoming = new Set(incomingKeys || []);
    return {
      added: [...incoming].filter(key => !existing.has(key)),
      kept: [...incoming].filter(key => existing.has(key)),
      removed: [...existing].filter(key => !incoming.has(key)),
    };
  }

  function createDirtyScheduler({ requestFrame, cancelFrame, now, draw, isHidden = () => false, maxFps = LIMITS.maxFps }) {
    const minimumFrameMs = 1000 / maxFps;
    let frameId = 0;
    let flags = 0;
    let animationUntil = 0;
    let lastDrawAt = -Infinity;
    const reasons = new Set();

    function schedule() {
      if (!frameId && !isHidden()) frameId = requestFrame(run);
    }

    function run(timestamp) {
      frameId = 0;
      if (isHidden()) return;
      const animating = timestamp < animationUntil;
      if (timestamp - lastDrawAt + 0.25 < minimumFrameMs) {
        if (flags || animating) schedule();
        return;
      }
      if (!flags && !animating) return;
      const frameFlags = flags | (animating ? FLAGS.ANIMATION : 0);
      const frameReasons = [...reasons];
      flags = 0;
      reasons.clear();
      lastDrawAt = timestamp;
      draw(frameFlags, frameReasons, timestamp);
      if (flags || timestamp < animationUntil) schedule();
    }

    return {
      invalidate(nextFlags, reason = 'update') {
        flags |= nextFlags;
        reasons.add(reason);
        schedule();
      },
      animate(duration = LIMITS.animationMs, reason = 'movement') {
        animationUntil = Math.max(animationUntil, now() + duration);
        flags |= FLAGS.ANIMATION;
        reasons.add(reason);
        schedule();
      },
      cancel() {
        if (frameId) cancelFrame(frameId);
        frameId = 0;
      },
      resume() { if (flags || now() < animationUntil) schedule(); },
      state() { return { pending: Boolean(frameId), flags, animationUntil, lastDrawAt }; },
    };
  }

  const testApi = Object.freeze({
    FLAGS,
    LIMITS,
    entityAliasKey,
    niceStep,
    axisTicks,
    compressObservedRows,
    normaliseCamera,
    visibleBounds,
    reconcileKeys,
    createDirtyScheduler,
  });
  if (typeof window !== 'undefined') window.__TACTICAL_MAP_TEST__ = testApi;
  if (typeof document === 'undefined') return;

  const viewport = document.getElementById('map-viewport');
  if (!viewport) return;
  const byId = id => document.getElementById(id);
  const stageHost = byId('map-stage') || viewport;
  const axisX = byId('map-axis-x');
  const axisY = byId('map-axis-y');
  const centerReadout = byId('mapCenterCoordinate');
  const selectionReadout = byId('mapSelectionCoordinate');
  const targetReadout = byId('mapTargetCoordinate');
  const debugHud = byId('mapDebugHud');
  const debugEnabled = new URLSearchParams(window.location.search).get('debugMap') === '1';
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true;

  function makeAxisPool(container, kind) {
    if (!container) return [];
    const pool = [];
    for (let index = 0; index < LIMITS.axisLabels; index++) {
      const label = document.createElement('span');
      label.className = `map-axis-label map-axis-label-${kind}`;
      label.hidden = true;
      container.appendChild(label);
      pool.push(label);
    }
    return pool;
  }

  const xLabels = makeAxisPool(axisX, 'x');
  const yLabels = makeAxisPool(axisY, 'y');
  if (debugHud) debugHud.hidden = !debugEnabled;

  let savedView = {};
  try { savedView = JSON.parse(localStorage.getItem(VIEW_STORAGE_KEY) || '{}'); }
  catch (_) { savedView = {}; }
  const camera = normaliseCamera(savedView);
  let viewInitialized = isCell(savedView.anchor) || Number.isFinite(savedView.scale);
  let layers = {
    fog: savedView.layers?.fog !== false,
    vision: typeof savedView.layers?.vision === 'string'
      ? savedView.layers.vision : (savedView.layers?.vision === false ? 'off' : 'selected'),
    coordinates: savedView.layers?.coordinates !== false,
    labels: savedView.layers?.labels !== false,
  };
  if (!['selected', 'all', 'off'].includes(layers.vision)) layers.vision = 'selected';

  let size = { w: 640, h: 420 };
  let currentView = null;
  let selectedKey = null;
  let trackingKey = typeof savedView.focus === 'string' ? savedView.focus : null;
  let hoveredKey = null;
  let drag = null;
  let pickMode = false;
  let previewTarget = null;
  let lockedTarget = null;
  let persistTimer = 0;
  let wheelPersistTimer = 0;
  let contextLost = false;
  let restoreTimer = 0;
  let fallbackAttempted = false;
  let mode = 'none';
  let canvas = null;
  let context2d = null;
  let pixiApp = null;
  let pixiWorld = null;
  let surfaceGraphic = null;
  let observedGraphic = null;
  let gridGraphic = null;
  let terrainGraphic = null;
  let visionGraphic = null;
  let pathGraphic = null;
  let unitLayer = null;
  let radarLayer = null;
  let radarPool = [];
  const symbolTextures = new Map();
  const entityModels = new Map();
  const entityViews = new Map();
  const stats = {
    renderer: '未初始化', fps: 0, frameMs: 0, frames: 0, objects: 0,
    coordinateLabels: 0, fogSegments: 0, reason: 'startup', lastRenderedAt: 0,
  };

  function setText(node, value) {
    if (node && node.textContent !== value) node.textContent = value;
  }

  function syncLayerControls() {
    if (byId('layerFog')) byId('layerFog').checked = layers.fog;
    if (byId('visionMode')) byId('visionMode').value = layers.vision;
    if (byId('layerCoordinates')) byId('layerCoordinates').checked = layers.coordinates;
    if (byId('layerLabels')) byId('layerLabels').checked = layers.labels;
  }
  syncLayerControls();

  function persistNow() {
    if (persistTimer) window.clearTimeout(persistTimer);
    persistTimer = 0;
    try {
      localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify({
        anchor: camera.anchor, scale: camera.scale, pan: camera.pan, focus: trackingKey, layers,
      }));
    } catch (_) { /* storage is optional in restricted browser contexts */ }
  }

  function schedulePersist(immediate = false) {
    if (immediate) { persistNow(); return; }
    if (persistTimer) window.clearTimeout(persistTimer);
    persistTimer = window.setTimeout(persistNow, 150);
  }

  function worldPoint(cell) {
    return [
      size.w / 2 + camera.pan.x + (cell[0] - camera.anchor[0]) * camera.scale,
      size.h / 2 + camera.pan.y + (cell[1] - camera.anchor[1]) * camera.scale,
    ];
  }

  function worldCoordinate(x, y) {
    return [
      (x - size.w / 2 - camera.pan.x) / camera.scale + camera.anchor[0],
      (y - size.h / 2 - camera.pan.y) / camera.scale + camera.anchor[1],
    ];
  }

  function worldCell(x, y) {
    return worldCoordinate(x, y).map(Math.round);
  }

  function currentCenterCell() {
    return worldCell(size.w / 2, size.h / 2);
  }

  function isInBounds(cell, bounds, padding = 0) {
    return cell[0] >= bounds.minX - padding && cell[0] <= bounds.maxX + padding
      && cell[1] >= bounds.minY - padding && cell[1] <= bounds.maxY + padding;
  }

  function updateCoordinateReadouts() {
    const center = currentCenterCell();
    setText(centerReadout, `中心 ${center[0]},${center[1]}`);
    const selected = entityModels.get(selectedKey);
    setText(selectionReadout, selected ? `选中 ${Math.round(selected.target[0])},${Math.round(selected.target[1])}` : '选中 —');
    const target = previewTarget || lockedTarget;
    setText(targetReadout, target ? `目标 ${target[0]},${target[1]}` : '目标 —');
  }

  function updateAxes() {
    if (axisX) axisX.hidden = !layers.coordinates;
    if (axisY) axisY.hidden = !layers.coordinates;
    if (!layers.coordinates) {
      [...xLabels, ...yLabels].forEach(label => { label.hidden = true; });
      stats.coordinateLabels = 0;
      updateCoordinateReadouts();
      return;
    }
    const bounds = visibleBounds(camera, size);
    const xTicks = axisTicks(bounds.minX, bounds.maxX, camera.scale);
    const yTicks = axisTicks(bounds.minY, bounds.maxY, camera.scale);
    xLabels.forEach((label, index) => {
      const value = xTicks.values[index];
      if (value == null) { label.hidden = true; return; }
      const x = worldPoint([value, camera.anchor[1]])[0];
      label.hidden = false;
      label.textContent = `X ${value}`;
      label.style.transform = `translate3d(${Math.round(x)}px,0,0)`;
    });
    yLabels.forEach((label, index) => {
      const value = yTicks.values[index];
      if (value == null) { label.hidden = true; return; }
      const y = worldPoint([camera.anchor[0], value])[1];
      label.hidden = false;
      label.textContent = `Y ${value}`;
      label.style.transform = `translate3d(0,${Math.round(y)}px,0)`;
    });
    stats.coordinateLabels = xTicks.values.length + yTicks.values.length;
    updateCoordinateReadouts();
  }

  function setRendererNotice(message, { blocking = false, error = false } = {}) {
    const headerStatus = byId('rendererStatus');
    if (headerStatus) {
      headerStatus.textContent = message;
      headerStatus.classList.toggle('is-error', error);
    }
    const notice = byId('mapRendererState');
    if (notice) {
      notice.hidden = !blocking;
      notice.textContent = message;
      notice.classList.toggle('is-error', error);
    }
  }

  function updateDebugHud() {
    if (!debugEnabled || !debugHud) return;
    const idle = scheduler.state().pending ? '调度中' : '空闲';
    debugHud.textContent = [
      `${stats.renderer} · ${idle}`,
      `FPS ${stats.fps.toFixed(1)} · ${stats.frameMs.toFixed(2)} ms`,
      `DisplayObject ${stats.objects} · 坐标 ${stats.coordinateLabels}`,
      `迷雾段 ${stats.fogSegments} · 帧 ${stats.frames}`,
      `原因 ${stats.reason}`,
    ].join('\n');
  }

  function countDisplayObjects(node) {
    if (!node) return 0;
    return 1 + (node.children || []).reduce((total, child) => total + countDisplayObjects(child), 0);
  }

  function resizeSurface() {
    const rect = viewport.getBoundingClientRect();
    size = { w: Math.max(320, Math.round(rect.width || 640)), h: Math.max(300, Math.round(rect.height || 420)) };
    if (mode === 'pixi' && pixiApp) pixiApp.renderer.resize(size.w, size.h);
    if (mode === 'canvas' && canvas && context2d) {
      const dpr = Math.min(1.5, window.devicePixelRatio || 1);
      canvas.width = Math.round(size.w * dpr);
      canvas.height = Math.round(size.h * dpr);
      canvas.style.width = `${size.w}px`;
      canvas.style.height = `${size.h}px`;
      context2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    scheduler.invalidate(FLAGS.RESIZE | FLAGS.CAMERA, 'resize');
  }

  function createSymbolTextures() {
    if (!pixiApp || !window.PIXI) return;
    for (const kind of ['CORE', 'WORKER', 'VANGUARD', 'RANGER']) {
      const graphic = new PIXI.Graphics();
      graphic.lineStyle(2, 0xffffff, 1);
      graphic.beginFill(0xffffff, kind === 'CORE' ? 0.28 : 0.88);
      if (kind === 'CORE') graphic.drawCircle(16, 16, 10);
      else if (kind === 'WORKER') graphic.drawRect(7, 7, 18, 18);
      else if (kind === 'VANGUARD') graphic.drawPolygon([16, 5, 27, 25, 5, 25]);
      else graphic.drawPolygon([16, 4, 28, 16, 16, 28, 4, 16]);
      graphic.endFill();
      const texture = pixiApp.renderer.generateTexture(graphic, {
        region: new PIXI.Rectangle(0, 0, 32, 32),
        resolution: Math.min(2, window.devicePixelRatio || 1),
      });
      symbolTextures.set(kind, texture);
      graphic.destroy();
    }
  }

  function createRadarPool() {
    radarPool = [];
    for (let index = 0; index < LIMITS.radarMarkers; index++) {
      const arrow = new PIXI.Graphics();
      arrow.beginFill(0xffffff).drawPolygon([9, 0, -7, -5, -3, 0, -7, 5]).endFill();
      arrow.visible = false;
      const label = new PIXI.Text('', {
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
        fontSize: 9,
        fill: COLOR_NUMBERS.text,
      });
      label.visible = false;
      radarLayer.addChild(arrow, label);
      radarPool.push({ arrow, label });
    }
  }

  function initPixi() {
    if (!window.PIXI || (PIXI.utils?.isWebGLSupported && !PIXI.utils.isWebGLSupported())) return false;
    try {
      pixiApp = new PIXI.Application({
        width: size.w,
        height: size.h,
        backgroundColor: COLOR_NUMBERS.bg,
        antialias: false,
        resolution: Math.min(1.5, window.devicePixelRatio || 1),
        autoDensity: true,
        autoStart: false,
        sharedTicker: false,
      });
      pixiApp.stop();
      canvas = pixiApp.view || pixiApp.canvas;
      canvas.className = 'map-canvas';
      canvas.setAttribute('aria-label', '当前战术地图（Pixi WebGL 按需渲染）');
      stageHost.replaceChildren(canvas);

      surfaceGraphic = new PIXI.Graphics();
      pixiWorld = new PIXI.Container();
      observedGraphic = new PIXI.Graphics();
      gridGraphic = new PIXI.Graphics();
      terrainGraphic = new PIXI.Graphics();
      visionGraphic = new PIXI.Graphics();
      pathGraphic = new PIXI.Graphics();
      unitLayer = new PIXI.Container();
      radarLayer = new PIXI.Container();
      pixiWorld.addChild(observedGraphic, gridGraphic, terrainGraphic, visionGraphic, pathGraphic, unitLayer);
      pixiApp.stage.addChild(surfaceGraphic, pixiWorld, radarLayer);
      createSymbolTextures();
      createRadarPool();

      canvas.addEventListener('webglcontextlost', event => {
        event.preventDefault();
        contextLost = true;
        scheduler.cancel();
        setRendererNotice('WebGL 上下文丢失 · 正在恢复', { blocking: true, error: true });
        window.clearTimeout(restoreTimer);
        restoreTimer = window.setTimeout(() => fallbackToCanvas('WebGL 恢复超时'), 1400);
      });
      canvas.addEventListener('webglcontextrestored', () => {
        window.clearTimeout(restoreTimer);
        contextLost = false;
        try {
          setRendererNotice('Pixi WebGL · 按需渲染');
          scheduler.invalidate(FLAGS.ALL, 'context-restored');
        } catch (_) { fallbackToCanvas('WebGL 恢复失败'); }
      });
      mode = 'pixi';
      stats.renderer = 'Pixi WebGL';
      setRendererNotice('Pixi WebGL · 按需渲染');
      resizeSurface();
      return true;
    } catch (error) {
      console.warn('Pixi WebGL initialization failed; using Canvas 2D.', error);
      pixiApp = null;
      return false;
    }
  }

  function initCanvas2D(reason = '') {
    canvas = document.createElement('canvas');
    canvas.className = 'map-canvas';
    canvas.setAttribute('aria-label', '当前战术地图（Canvas 2D 按需渲染）');
    stageHost.replaceChildren(canvas);
    context2d = canvas.getContext('2d', { alpha: false, desynchronized: true });
    if (!context2d) return false;
    mode = 'canvas';
    contextLost = false;
    stats.renderer = 'Canvas 2D';
    setRendererNotice(reason ? `Canvas 2D · ${reason}` : 'Canvas 2D · 按需渲染');
    resizeSurface();
    return true;
  }

  function destroyEntityView(key) {
    const view = entityViews.get(key);
    if (!view) return;
    entityViews.delete(key);
    view.holder.removeFromParent?.();
    view.holder.destroy({ children: true, texture: false, baseTexture: false });
  }

  function teardownPixi() {
    for (const key of [...entityViews.keys()]) destroyEntityView(key);
    for (const texture of symbolTextures.values()) {
      try { texture.destroy(true); } catch (_) { /* context may already be gone */ }
    }
    symbolTextures.clear();
    radarPool = [];
    if (pixiApp) {
      try { pixiApp.destroy(true, { children: true, texture: false, baseTexture: false }); }
      catch (_) { /* context-loss cleanup is best effort */ }
    }
    pixiApp = null;
    pixiWorld = null;
    surfaceGraphic = observedGraphic = gridGraphic = terrainGraphic = visionGraphic = pathGraphic = null;
    unitLayer = radarLayer = null;
  }

  function fallbackToCanvas(reason) {
    if (fallbackAttempted && mode === 'canvas') return;
    fallbackAttempted = true;
    window.clearTimeout(restoreTimer);
    teardownPixi();
    mode = 'none';
    if (initCanvas2D(reason)) scheduler.invalidate(FLAGS.ALL, 'canvas-fallback');
    else setRendererNotice('地图渲染器不可用 · 人工任务仍可操作', { blocking: true, error: true });
  }

  function ensureRenderer() {
    if (mode !== 'none') return true;
    if (initPixi()) return true;
    return initCanvas2D('WebGL 不可用');
  }

  function normaliseEntity(value, enemy = false) {
    if (!value || !isCell(value.position)) return null;
    let kind = String(value.kind || value.unit_type || 'WORKER').toUpperCase();
    if (kind === 'UNIT') kind = String(value.unit_type || 'WORKER').toUpperCase();
    if (!KIND_LABELS[kind]) kind = 'WORKER';
    const data = { ...value, kind, enemy: enemy || Boolean(value.enemy) };
    return { key: idOf(data), data, kind, enemy: data.enemy, position: data.position.map(Number) };
  }

  function updateData(view) {
    const map = view?.current?.map || {};
    const commandCenter = view?.command_center || {};
    const aliases = new Map((commandCenter.entities || []).map(entity => [entityAliasKey(entity.alias), entity]));
    const friendly = (map.friendly || [])
      .map(entity => ({ ...entity, ...(aliases.get(entityAliasKey(entity.alias)) || {}) }))
      .map(entity => normaliseEntity(entity, false)).filter(Boolean);
    const enemies = (map.enemies || []).map(entity => normaliseEntity(entity, true)).filter(Boolean);
    const incoming = new Map([...friendly, ...enemies].map(entity => [entity.key, entity]));
    const core = friendly.find(entity => entity.kind === 'CORE');
    const suggestedAnchor = core?.position || friendly[0]?.position || enemies[0]?.position || [0, 0];
    if (!viewInitialized) {
      camera.anchor = [...suggestedAnchor];
      camera.scale = 24;
      camera.pan = { x: 0, y: 0 };
      viewInitialized = true;
      schedulePersist();
    }

    const now = performance.now();
    let moved = false;
    const changes = reconcileKeys(entityModels.keys(), incoming.keys());
    for (const key of changes.removed) {
      entityModels.delete(key);
      destroyEntityView(key);
      if (selectedKey === key) selectedKey = null;
      if (trackingKey === key) trackingKey = null;
      if (hoveredKey === key) hoveredKey = null;
    }
    for (const [key, entity] of incoming) {
      let model = entityModels.get(key);
      if (!model) {
        model = {
          key, kind: entity.kind, enemy: entity.enemy, data: entity.data,
          position: [...entity.position], start: [...entity.position], target: [...entity.position],
          animationStart: now, moving: false,
        };
        entityModels.set(key, model);
      } else {
        model.kind = entity.kind;
        model.enemy = entity.enemy;
        model.data = entity.data;
        if (!sameCell(model.target, entity.position)) {
          model.start = [...model.position];
          model.target = [...entity.position];
          model.animationStart = now;
          model.moving = !reducedMotion;
          if (reducedMotion) model.position = [...entity.position];
          moved = moved || model.moving;
        }
      }
    }

    const tracked = entityModels.get(trackingKey);
    if (tracked) camera.anchor = [...tracked.target];
    const resources = (map.resources || []).filter(isCell).map(cell => cell.map(Number));
    const obstacles = (map.obstacles || []).filter(isCell).map(cell => cell.map(Number));
    const observed = (map.observed || []).filter(isCell).slice(0, 2000).map(cell => cell.map(Number));
    currentView = {
      tick: view?.current?.tick,
      mode: view?.current?.mode_label || '当前态势',
      friendly: friendly.map(entity => entity.data),
      enemies: enemies.map(entity => entity.data),
      resources,
      obstacles,
      observed,
      observedSegments: compressObservedRows(observed),
      beacon: isCell(map.beacon?.position) ? { ...map.beacon, position: map.beacon.position.map(Number) } : null,
    };
    stats.fogSegments = currentView.observedSegments.length;
    updateCoordinateReadouts();
    scheduler.invalidate(FLAGS.DATA | FLAGS.SELECTION, `tick:${currentView.tick ?? 'unknown'}`);
    if (moved) scheduler.animate(LIMITS.animationMs, 'unit-movement');
  }

  function updateAnimations(timestamp) {
    let moving = false;
    for (const model of entityModels.values()) {
      if (!model.moving) continue;
      const raw = clamp((timestamp - model.animationStart) / LIMITS.animationMs, 0, 1);
      const amount = 1 - (1 - raw) ** 3;
      model.position[0] = lerp(model.start[0], model.target[0], amount);
      model.position[1] = lerp(model.start[1], model.target[1], amount);
      model.moving = raw < 1;
      moving = moving || model.moving;
      if (!model.moving) model.position = [...model.target];
    }
    return moving;
  }

  function updateWorldTransform() {
    if (!pixiWorld) return;
    pixiWorld.position.set(size.w / 2 + camera.pan.x, size.h / 2 + camera.pan.y);
    pixiWorld.scale.set(camera.scale, camera.scale);
    pixiWorld.pivot.set(camera.anchor[0], camera.anchor[1]);
  }

  function drawSurfacePixi() {
    surfaceGraphic.clear();
    surfaceGraphic.beginFill(layers.fog ? COLOR_NUMBERS.bg : COLOR_NUMBERS.observed).drawRect(0, 0, size.w, size.h).endFill();
    observedGraphic.clear();
    if (!layers.fog || !currentView) return;
    observedGraphic.beginFill(COLOR_NUMBERS.observed, 0.94);
    for (const [start, end, y] of currentView.observedSegments) {
      observedGraphic.drawRect(start - 0.5, y - 0.5, end - start + 1, 1);
    }
    observedGraphic.endFill();
  }

  function drawGridPixi() {
    gridGraphic.clear();
    const bounds = visibleBounds(camera, size, 20);
    const step = camera.scale >= 18 ? 1 : (camera.scale >= 10 ? 2 : 5);
    const minX = Math.floor(bounds.minX / step) * step;
    const maxX = Math.ceil(bounds.maxX / step) * step;
    const minY = Math.floor(bounds.minY / step) * step;
    const maxY = Math.ceil(bounds.maxY / step) * step;
    for (let x = minX; x <= maxX; x += step) {
      const major = x % 5 === 0;
      gridGraphic.lineStyle((major ? 1 : 0.55) / camera.scale, COLOR_NUMBERS.grid, major ? 0.58 : 0.24);
      gridGraphic.moveTo(x, bounds.minY).lineTo(x, bounds.maxY);
    }
    for (let y = minY; y <= maxY; y += step) {
      const major = y % 5 === 0;
      gridGraphic.lineStyle((major ? 1 : 0.55) / camera.scale, COLOR_NUMBERS.grid, major ? 0.58 : 0.24);
      gridGraphic.moveTo(bounds.minX, y).lineTo(bounds.maxX, y);
    }
  }

  function drawTerrainPixi() {
    terrainGraphic.clear();
    if (!currentView) return;
    const bounds = visibleBounds(camera, size, 60);
    const line = 1 / camera.scale;
    for (const cell of currentView.obstacles) {
      if (!isInBounds(cell, bounds, 1)) continue;
      terrainGraphic.lineStyle(line, COLOR_NUMBERS.grid, 0.85).beginFill(COLOR_NUMBERS.obstacle, 0.95);
      terrainGraphic.drawRect(cell[0] - 0.34, cell[1] - 0.34, 0.68, 0.68).endFill();
    }
    for (const cell of currentView.resources) {
      if (!isInBounds(cell, bounds, 1)) continue;
      terrainGraphic.lineStyle(1.2 / camera.scale, COLOR_NUMBERS.resource, 1).beginFill(COLOR_NUMBERS.resource, 0.86);
      terrainGraphic.drawPolygon([cell[0], cell[1] - 0.34, cell[0] + 0.34, cell[1], cell[0], cell[1] + 0.34, cell[0] - 0.34, cell[1]]).endFill();
    }
    if (currentView.beacon && isInBounds(currentView.beacon.position, bounds, 1)) {
      const [x, y] = currentView.beacon.position;
      terrainGraphic.lineStyle(1.6 / camera.scale, COLOR_NUMBERS.friendly, 0.95).drawCircle(x, y, 0.5);
      terrainGraphic.beginFill(COLOR_NUMBERS.friendly, 0.82).drawCircle(x, y, 0.16).endFill();
    }
  }

  function visionModels() {
    if (layers.vision === 'off') return [];
    const friendly = [...entityModels.values()].filter(model => !model.enemy);
    if (layers.vision === 'all') return friendly;
    const core = friendly.find(model => model.kind === 'CORE');
    const selected = entityModels.get(selectedKey);
    return [...new Map([core, selected].filter(model => model && !model.enemy).map(model => [model.key, model])).values()];
  }

  function drawVisionPixi() {
    visionGraphic.clear();
    for (const model of visionModels()) {
      const radius = Number(model.data.vision_radius);
      if (!Number.isFinite(radius) || radius <= 0) continue;
      const [x, y] = model.target;
      const color = model.kind === 'CORE' ? COLOR_NUMBERS.resource : COLOR_NUMBERS.friendly;
      visionGraphic.lineStyle((model.kind === 'CORE' ? 1.35 : 1) / camera.scale, color, model.kind === 'CORE' ? 0.55 : 0.34);
      visionGraphic.drawPolygon([x, y - radius, x + radius, y, x, y + radius, x - radius, y]);
    }
  }

  function drawPathPixi() {
    pathGraphic.clear();
    const selected = entityModels.get(selectedKey);
    const target = previewTarget || lockedTarget;
    if (selected && target) {
      pathGraphic.lineStyle(1.5 / camera.scale, COLOR_NUMBERS.resource, 0.82);
      pathGraphic.moveTo(selected.position[0], selected.position[1]).lineTo(target[0], target[1]);
      pathGraphic.drawCircle(target[0], target[1], 0.24);
    }
    if (selected) {
      const actionTarget = isCell(selected.data.destination) ? selected.data.destination
        : (isCell(selected.data.target_cell) ? selected.data.target_cell : null);
      if (actionTarget) {
        pathGraphic.lineStyle(1 / camera.scale, selected.enemy ? COLOR_NUMBERS.enemy : COLOR_NUMBERS.friendly, 0.58);
        pathGraphic.moveTo(selected.position[0], selected.position[1]).lineTo(actionTarget[0], actionTarget[1]);
      }
    }
  }

  function createEntityView(model) {
    const holder = new PIXI.Container();
    const selection = new PIXI.Graphics();
    const sprite = new PIXI.Sprite(symbolTextures.get(model.kind) || symbolTextures.get('WORKER'));
    sprite.anchor.set(0.5);
    sprite.width = 26;
    sprite.height = 26;
    const hp = new PIXI.Graphics();
    holder.addChild(selection, sprite, hp);
    unitLayer.addChild(holder);
    const view = { holder, selection, sprite, hp, label: null, kind: model.kind };
    entityViews.set(model.key, view);
    return view;
  }

  function updateEntityView(view, model, fullUpdate) {
    view.holder.position.set(model.position[0], model.position[1]);
    view.holder.scale.set(1 / camera.scale);
    if (view.kind !== model.kind) {
      view.kind = model.kind;
      view.sprite.texture = symbolTextures.get(model.kind) || symbolTextures.get('WORKER');
    }
    const color = model.enemy ? COLOR_NUMBERS.enemy : (model.kind === 'CORE' ? COLOR_NUMBERS.resource : COLOR_NUMBERS.friendly);
    view.sprite.tint = color;
    view.holder.alpha = model.data.status === 'BLOCKED' ? 0.66 : 1;
    if (fullUpdate) {
      view.selection.clear();
      if (model.key === selectedKey) {
        view.selection.lineStyle(1.5, COLOR_NUMBERS.text, 0.9).drawCircle(0, 0, 16);
        view.selection.moveTo(-21, 0).lineTo(-15, 0).moveTo(15, 0).lineTo(21, 0);
      }
      view.hp.clear();
      const hp = Number(model.data.hp);
      if (Number.isFinite(hp)) {
        const maximum = Math.max(1, Number(model.data.max_hp || KIND_HP[model.kind] || hp));
        const ratio = clamp(hp / maximum, 0, 1);
        view.hp.beginFill(COLOR_NUMBERS.enemy, 0.38).drawRect(-12, 15, 24, 2).endFill();
        view.hp.beginFill(COLOR_NUMBERS.friendly, 0.92).drawRect(-12, 15, 24 * ratio, 2).endFill();
      }
    }
    const shouldLabel = layers.labels && (model.key === selectedKey || model.key === hoveredKey || camera.scale >= 30);
    if (shouldLabel && !view.label) {
      view.label = new PIXI.Text('', {
        fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif',
        fontSize: 10,
        fill: 0xffffff,
      });
      view.label.position.set(16, -15);
      view.holder.addChild(view.label);
    }
    if (view.label) {
      view.label.visible = shouldLabel;
      if (shouldLabel) {
        const suffix = model.enemy ? '敌' : String(model.data.alias || model.key).slice(-4);
        const text = `${model.enemy ? '敌方' : KIND_LABELS[model.kind]} ${suffix}`;
        if (view.label.text !== text) view.label.text = text;
        view.label.tint = color;
      }
    }
  }

  function syncEntityViews(flags) {
    const bounds = visibleBounds(camera, size, 80);
    const visibleKeys = new Set();
    for (const model of entityModels.values()) {
      if (!isInBounds(model.position, bounds, 1)) continue;
      visibleKeys.add(model.key);
      const view = entityViews.get(model.key) || createEntityView(model);
      updateEntityView(view, model, Boolean(flags & (FLAGS.DATA | FLAGS.SELECTION | FLAGS.LAYERS | FLAGS.CAMERA | FLAGS.RESIZE)));
    }
    for (const key of [...entityViews.keys()]) if (!visibleKeys.has(key)) destroyEntityView(key);
  }

  function farMarkers() {
    if (!currentView) return [];
    const bounds = visibleBounds(camera, size, 12);
    const origin = currentCenterCell();
    const candidates = [];
    for (const enemy of currentView.enemies) {
      if (!isCell(enemy.position) || isInBounds(enemy.position, bounds)) continue;
      candidates.push({ position: enemy.position, type: 'enemy', label: '敌方' });
    }
    for (const resource of currentView.resources) {
      if (isInBounds(resource, bounds)) continue;
      candidates.push({ position: resource, type: 'resource', label: '资源' });
    }
    if (currentView.beacon && !isInBounds(currentView.beacon.position, bounds)) {
      candidates.push({ position: currentView.beacon.position, type: 'beacon', label: '信标' });
    }
    return candidates.sort((a, b) => manhattan(origin, a.position) - manhattan(origin, b.position)).slice(0, LIMITS.radarMarkers);
  }

  function radarPosition(cell) {
    const origin = currentCenterCell();
    const dx = cell[0] - origin[0];
    const dy = cell[1] - origin[1];
    const halfWidth = Math.max(20, size.w / 2 - 28);
    const halfHeight = Math.max(20, size.h / 2 - 28);
    const multiplier = Math.min(dx ? halfWidth / Math.abs(dx) : Infinity, dy ? halfHeight / Math.abs(dy) : Infinity);
    const factor = Number.isFinite(multiplier) ? multiplier : 0;
    return {
      x: size.w / 2 + dx * factor,
      y: size.h / 2 + dy * factor,
      angle: Math.atan2(dy, dx),
      distance: Math.round(Math.hypot(dx, dy)),
    };
  }

  function drawRadarPixi() {
    const markers = farMarkers();
    radarPool.forEach((entry, index) => {
      const marker = markers[index];
      if (!marker) { entry.arrow.visible = false; entry.label.visible = false; return; }
      const point = radarPosition(marker.position);
      const color = marker.type === 'enemy' ? COLOR_NUMBERS.enemy
        : (marker.type === 'resource' ? COLOR_NUMBERS.resource : COLOR_NUMBERS.friendly);
      entry.arrow.visible = true;
      entry.arrow.tint = color;
      entry.arrow.position.set(point.x, point.y);
      entry.arrow.rotation = point.angle;
      entry.label.visible = true;
      entry.label.text = `${marker.label} ${point.distance}格`;
      entry.label.position.set(clamp(point.x + 8, 6, size.w - 74), clamp(point.y + 7, 8, size.h - 18));
    });
  }

  function renderPixi(flags) {
    if (!pixiApp || !currentView) return;
    if (flags & (FLAGS.CAMERA | FLAGS.RESIZE)) updateWorldTransform();
    if (flags & (FLAGS.DATA | FLAGS.LAYERS | FLAGS.RESIZE)) drawSurfacePixi();
    if (flags & (FLAGS.CAMERA | FLAGS.RESIZE | FLAGS.LAYERS)) drawGridPixi();
    if (flags & (FLAGS.DATA | FLAGS.CAMERA | FLAGS.RESIZE | FLAGS.LAYERS)) drawTerrainPixi();
    if (flags & (FLAGS.DATA | FLAGS.SELECTION | FLAGS.LAYERS | FLAGS.CAMERA | FLAGS.RESIZE)) drawVisionPixi();
    if (flags & (FLAGS.DATA | FLAGS.SELECTION | FLAGS.LAYERS | FLAGS.CAMERA | FLAGS.RESIZE | FLAGS.ANIMATION)) drawPathPixi();
    if (flags & (FLAGS.DATA | FLAGS.SELECTION | FLAGS.LAYERS | FLAGS.CAMERA | FLAGS.RESIZE | FLAGS.ANIMATION)) syncEntityViews(flags);
    if (flags & (FLAGS.DATA | FLAGS.CAMERA | FLAGS.RESIZE)) drawRadarPixi();
    pixiApp.renderer.render(pixiApp.stage);
    stats.objects = countDisplayObjects(pixiApp.stage);
  }

  function drawUnitShapeCanvas(context, kind, radius) {
    context.beginPath();
    if (kind === 'CORE') context.arc(0, 0, radius, 0, Math.PI * 2);
    else if (kind === 'VANGUARD') {
      context.moveTo(0, -radius);
      context.lineTo(radius * 0.9, radius * 0.78);
      context.lineTo(-radius * 0.9, radius * 0.78);
      context.closePath();
    } else if (kind === 'RANGER') {
      context.moveTo(0, -radius);
      context.lineTo(radius, 0);
      context.lineTo(0, radius);
      context.lineTo(-radius, 0);
      context.closePath();
    } else context.rect(-radius * 0.72, -radius * 0.72, radius * 1.44, radius * 1.44);
  }

  function drawCanvasGrid(context, bounds) {
    const step = camera.scale >= 18 ? 1 : (camera.scale >= 10 ? 2 : 5);
    const minX = Math.floor(bounds.minX / step) * step;
    const maxX = Math.ceil(bounds.maxX / step) * step;
    const minY = Math.floor(bounds.minY / step) * step;
    const maxY = Math.ceil(bounds.maxY / step) * step;
    for (let x = minX; x <= maxX; x += step) {
      const position = worldPoint([x, 0])[0];
      context.strokeStyle = COLORS.grid;
      context.globalAlpha = x % 5 === 0 ? 0.58 : 0.24;
      context.lineWidth = x % 5 === 0 ? 1 : 0.55;
      context.beginPath();
      context.moveTo(position, 0);
      context.lineTo(position, size.h);
      context.stroke();
    }
    for (let y = minY; y <= maxY; y += step) {
      const position = worldPoint([0, y])[1];
      context.strokeStyle = COLORS.grid;
      context.globalAlpha = y % 5 === 0 ? 0.58 : 0.24;
      context.lineWidth = y % 5 === 0 ? 1 : 0.55;
      context.beginPath();
      context.moveTo(0, position);
      context.lineTo(size.w, position);
      context.stroke();
    }
    context.globalAlpha = 1;
  }

  function drawCanvasVision(context) {
    for (const model of visionModels()) {
      const radius = Number(model.data.vision_radius);
      if (!Number.isFinite(radius) || radius <= 0) continue;
      const [x, y] = worldPoint(model.target);
      const span = radius * camera.scale;
      context.strokeStyle = model.kind === 'CORE' ? COLORS.resource : COLORS.friendly;
      context.globalAlpha = model.kind === 'CORE' ? 0.55 : 0.34;
      context.lineWidth = model.kind === 'CORE' ? 1.35 : 1;
      context.beginPath();
      context.moveTo(x, y - span);
      context.lineTo(x + span, y);
      context.lineTo(x, y + span);
      context.lineTo(x - span, y);
      context.closePath();
      context.stroke();
    }
    context.globalAlpha = 1;
  }

  function drawCanvasRadar(context) {
    for (const marker of farMarkers()) {
      const point = radarPosition(marker.position);
      const color = marker.type === 'enemy' ? COLORS.enemy
        : (marker.type === 'resource' ? COLORS.resource : COLORS.friendly);
      context.save();
      context.translate(point.x, point.y);
      context.rotate(point.angle);
      context.fillStyle = color;
      context.beginPath();
      context.moveTo(9, 0);
      context.lineTo(-7, -5);
      context.lineTo(-3, 0);
      context.lineTo(-7, 5);
      context.closePath();
      context.fill();
      context.restore();
      context.font = '9px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      context.fillStyle = COLORS.text;
      context.fillText(
        `${marker.label} ${point.distance}格`,
        clamp(point.x + 8, 6, size.w - 74),
        clamp(point.y + 8, 10, size.h - 12),
      );
    }
  }

  function renderCanvas() {
    if (!context2d || !currentView) return;
    const context = context2d;
    const bounds = visibleBounds(camera, size, 60);
    context.fillStyle = layers.fog ? COLORS.bg : COLORS.observed;
    context.fillRect(0, 0, size.w, size.h);
    if (layers.fog) {
      context.fillStyle = COLORS.observed;
      context.globalAlpha = 0.94;
      for (const [start, end, y] of currentView.observedSegments) {
        const point = worldPoint([start - 0.5, y - 0.5]);
        context.fillRect(point[0], point[1], (end - start + 1) * camera.scale, camera.scale);
      }
      context.globalAlpha = 1;
    }
    drawCanvasGrid(context, bounds);

    for (const cell of currentView.obstacles) {
      if (!isInBounds(cell, bounds, 1)) continue;
      const point = worldPoint(cell), radius = camera.scale * 0.34;
      context.fillStyle = COLORS.obstacle;
      context.strokeStyle = COLORS.grid;
      context.lineWidth = 1;
      context.fillRect(point[0] - radius, point[1] - radius, radius * 2, radius * 2);
      context.strokeRect(point[0] - radius, point[1] - radius, radius * 2, radius * 2);
    }
    for (const cell of currentView.resources) {
      if (!isInBounds(cell, bounds, 1)) continue;
      const point = worldPoint(cell), radius = camera.scale * 0.34;
      context.fillStyle = COLORS.resource;
      context.globalAlpha = 0.86;
      context.beginPath();
      context.moveTo(point[0], point[1] - radius);
      context.lineTo(point[0] + radius, point[1]);
      context.lineTo(point[0], point[1] + radius);
      context.lineTo(point[0] - radius, point[1]);
      context.closePath();
      context.fill();
      context.globalAlpha = 1;
    }
    if (currentView.beacon && isInBounds(currentView.beacon.position, bounds, 1)) {
      const point = worldPoint(currentView.beacon.position);
      context.strokeStyle = COLORS.friendly;
      context.lineWidth = 1.6;
      context.beginPath();
      context.arc(point[0], point[1], camera.scale * 0.5, 0, Math.PI * 2);
      context.stroke();
      context.fillStyle = COLORS.friendly;
      context.beginPath();
      context.arc(point[0], point[1], camera.scale * 0.16, 0, Math.PI * 2);
      context.fill();
    }
    drawCanvasVision(context);

    const selected = entityModels.get(selectedKey);
    const taskTarget = previewTarget || lockedTarget;
    if (selected && taskTarget) {
      const from = worldPoint(selected.position), to = worldPoint(taskTarget);
      context.strokeStyle = COLORS.resource;
      context.globalAlpha = 0.82;
      context.lineWidth = 1.5;
      context.beginPath();
      context.moveTo(from[0], from[1]);
      context.lineTo(to[0], to[1]);
      context.stroke();
      context.beginPath();
      context.arc(to[0], to[1], Math.max(5, camera.scale * 0.24), 0, Math.PI * 2);
      context.stroke();
      context.globalAlpha = 1;
    }

    let visibleCount = 0;
    for (const model of entityModels.values()) {
      if (!isInBounds(model.position, bounds, 1)) continue;
      visibleCount += 1;
      const point = worldPoint(model.position), radius = clamp(camera.scale * 0.33, 8, 13);
      const color = model.enemy ? COLORS.enemy : (model.kind === 'CORE' ? COLORS.resource : COLORS.friendly);
      context.save();
      context.translate(point[0], point[1]);
      context.strokeStyle = color;
      context.fillStyle = color;
      context.lineWidth = 1.5;
      context.globalAlpha = model.data.status === 'BLOCKED' ? 0.66 : 0.88;
      drawUnitShapeCanvas(context, model.kind, radius);
      context.fill();
      context.globalAlpha = 1;
      context.stroke();
      if (model.key === selectedKey) {
        context.strokeStyle = COLORS.text;
        context.lineWidth = 1.5;
        context.beginPath();
        context.arc(0, 0, radius + 5, 0, Math.PI * 2);
        context.stroke();
      }
      const hp = Number(model.data.hp);
      if (Number.isFinite(hp)) {
        const maximum = Math.max(1, Number(model.data.max_hp || KIND_HP[model.kind] || hp));
        const ratio = clamp(hp / maximum, 0, 1);
        context.fillStyle = 'rgba(229,107,115,.38)';
        context.fillRect(-12, radius + 4, 24, 2);
        context.fillStyle = COLORS.friendly;
        context.fillRect(-12, radius + 4, 24 * ratio, 2);
      }
      const shouldLabel = layers.labels && (model.key === selectedKey || model.key === hoveredKey || camera.scale >= 30);
      if (shouldLabel) {
        context.font = '10px system-ui, -apple-system, BlinkMacSystemFont, "Microsoft YaHei", sans-serif';
        context.fillStyle = color;
        context.fillText(
          `${model.enemy ? '敌方' : KIND_LABELS[model.kind]} ${model.enemy ? '敌' : String(model.data.alias || model.key).slice(-4)}`,
          16,
          -10,
        );
      }
      context.restore();
    }
    drawCanvasRadar(context);
    stats.objects = visibleCount + currentView.resources.length + currentView.obstacles.length + 6;
  }

  function renderFrame(flags, reasons, timestamp) {
    const startedAt = performance.now();
    updateAnimations(timestamp);
    try {
      if (mode === 'pixi') renderPixi(flags);
      else if (mode === 'canvas') renderCanvas();
    } catch (error) {
      console.warn('Tactical map render failed.', error);
      if (mode === 'pixi') { fallbackToCanvas('WebGL 绘制失败'); return; }
      setRendererNotice('地图渲染失败 · 人工任务仍可操作', { blocking: true, error: true });
      return;
    }
    if (flags & (FLAGS.CAMERA | FLAGS.RESIZE | FLAGS.LAYERS)) updateAxes();
    const completedAt = performance.now();
    stats.frameMs = completedAt - startedAt;
    stats.frames += 1;
    stats.fps = stats.lastRenderedAt ? 1000 / Math.max(1, timestamp - stats.lastRenderedAt) : 0;
    stats.lastRenderedAt = timestamp;
    stats.reason = reasons.join(', ') || (flags & FLAGS.ANIMATION ? 'animation' : 'render');
    updateDebugHud();
  }

  const scheduler = createDirtyScheduler({
    requestFrame: callback => window.requestAnimationFrame(callback),
    cancelFrame: frame => window.cancelAnimationFrame(frame),
    now: () => performance.now(),
    draw: renderFrame,
    isHidden: () => document.hidden || contextLost,
    maxFps: LIMITS.maxFps,
  });

  function nearestEntity(x, y) {
    let nearest = null;
    let best = 24;
    for (const model of entityModels.values()) {
      const point = worldPoint(model.position);
      const candidate = Math.hypot(point[0] - x, point[1] - y);
      if (candidate < best) { best = candidate; nearest = model; }
    }
    return nearest;
  }

  function selectEntity(key, { focus = false, notify = false } = {}) {
    const model = entityModels.get(key);
    if (!model) return false;
    selectedKey = key;
    if (focus) {
      trackingKey = key;
      camera.anchor = [...model.target];
      camera.pan = { x: 0, y: 0 };
      camera.scale = clamp(Math.max(camera.scale, 24), LIMITS.minScale, LIMITS.maxScale);
      schedulePersist(true);
      scheduler.invalidate(FLAGS.CAMERA | FLAGS.SELECTION, 'focus-unit');
    } else scheduler.invalidate(FLAGS.SELECTION, 'select-unit');
    updateCoordinateReadouts();
    if (notify && !model.enemy) window.selectDashboardUnit?.(model.data.alias);
    return true;
  }

  function focusCell(cell) {
    if (!isCell(cell)) return;
    trackingKey = null;
    camera.anchor = cell.map(Number);
    camera.pan = { x: 0, y: 0 };
    schedulePersist(true);
    scheduler.invalidate(FLAGS.CAMERA, 'focus-cell');
  }

  function zoomAt(factor, x, y) {
    const before = worldCoordinate(x, y);
    const nextScale = clamp(camera.scale * factor, LIMITS.minScale, LIMITS.maxScale);
    if (Math.abs(nextScale - camera.scale) < 0.001) return;
    camera.scale = nextScale;
    const after = worldPoint(before);
    camera.pan.x += x - after[0];
    camera.pan.y += y - after[1];
    schedulePersist();
    scheduler.invalidate(FLAGS.CAMERA, 'zoom');
  }

  function handleMapClick(x, y) {
    const entity = nearestEntity(x, y);
    if (entity) {
      selectEntity(entity.key, { focus: true, notify: true });
      return;
    }
    const cell = worldCell(x, y);
    window.updateDashboardMapCursor?.(cell);
    if (pickMode) {
      pickMode = false;
      lockedTarget = cell;
      previewTarget = null;
      window.setDashboardMapTarget?.(cell);
      window.updateDashboardTargetMode?.(false);
      scheduler.invalidate(FLAGS.SELECTION, 'target-locked');
    }
  }

  function eventPosition(event) {
    const rect = viewport.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  function isControlTarget(target) {
    return target instanceof Element
      && Boolean(target.closest('button,input,select,label,.map-coordinate-strip,.map-debug-hud'));
  }

  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0 || contextLost || isControlTarget(event.target)) return;
    const position = eventPosition(event);
    viewport.setPointerCapture?.(event.pointerId);
    drag = { ...position, startPan: { ...camera.pan }, moved: false };
  });
  viewport.addEventListener('pointermove', event => {
    const position = eventPosition(event);
    const cell = worldCell(position.x, position.y);
    window.updateDashboardMapCursor?.(cell);
    if (pickMode && !sameCell(previewTarget, cell)) {
      previewTarget = cell;
      scheduler.invalidate(FLAGS.SELECTION, 'target-preview');
      updateCoordinateReadouts();
    }
    if (drag) {
      drag.moved ||= Math.hypot(position.x - drag.x, position.y - drag.y) > 4;
      if (drag.moved) {
        trackingKey = null;
        camera.pan.x = drag.startPan.x + position.x - drag.x;
        camera.pan.y = drag.startPan.y + position.y - drag.y;
        schedulePersist();
        scheduler.invalidate(FLAGS.CAMERA, 'pan');
      }
      return;
    }
    const nextHover = nearestEntity(position.x, position.y)?.key || null;
    if (nextHover !== hoveredKey) {
      hoveredKey = nextHover;
      scheduler.invalidate(FLAGS.SELECTION, 'hover');
    }
  });
  const stopDrag = event => {
    if (!drag) return;
    const prior = drag;
    drag = null;
    if (prior.moved) schedulePersist(true);
    else if (!isControlTarget(event.target)) {
      const position = eventPosition(event);
      handleMapClick(position.x, position.y);
    }
  };
  viewport.addEventListener('pointerup', stopDrag);
  viewport.addEventListener('pointercancel', stopDrag);
  viewport.addEventListener('wheel', event => {
    if (contextLost || isControlTarget(event.target)) return;
    event.preventDefault();
    const position = eventPosition(event);
    const factor = clamp(Math.exp(-event.deltaY * 0.0015), 0.82, 1.22);
    zoomAt(factor, position.x, position.y);
    window.clearTimeout(wheelPersistTimer);
    wheelPersistTimer = window.setTimeout(() => schedulePersist(true), 130);
  }, { passive: false });
  viewport.addEventListener('dblclick', event => {
    if (isControlTarget(event.target)) return;
    const position = eventPosition(event);
    const entity = nearestEntity(position.x, position.y);
    if (entity) selectEntity(entity.key, { focus: true, notify: true });
  });
  viewport.addEventListener('keydown', event => {
    const panStep = 42;
    if (event.key === 'Escape' && pickMode) {
      pickMode = false;
      previewTarget = null;
      window.updateDashboardTargetMode?.(false);
      scheduler.invalidate(FLAGS.SELECTION, 'target-cancel');
    } else if (event.key === '+' || event.key === '=') zoomAt(1.18, size.w / 2, size.h / 2);
    else if (event.key === '-' || event.key === '_') zoomAt(0.84, size.w / 2, size.h / 2);
    else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
      trackingKey = null;
      camera.pan.x += event.key === 'ArrowLeft' ? panStep : (event.key === 'ArrowRight' ? -panStep : 0);
      camera.pan.y += event.key === 'ArrowUp' ? panStep : (event.key === 'ArrowDown' ? -panStep : 0);
      schedulePersist();
      scheduler.invalidate(FLAGS.CAMERA, 'keyboard-pan');
    } else return;
    event.preventDefault();
  });

  window.focusTacticalUnit = alias => {
    const model = [...entityModels.values()].find(candidate => String(candidate.data.alias) === String(alias));
    if (model) selectEntity(model.key, { focus: true });
  };
  window.selectTacticalUnit = alias => {
    const model = [...entityModels.values()].find(candidate => String(candidate.data.alias) === String(alias));
    if (model) selectEntity(model.key);
  };
  window.focusTacticalCell = cell => focusCell(cell);
  window.zoomTacticalMap = factor => zoomAt(Number(factor) || 1, size.w / 2, size.h / 2);
  window.resetTacticalMap = () => {
    const core = [...entityModels.values()].find(model => !model.enemy && model.kind === 'CORE');
    trackingKey = null;
    camera.anchor = core ? [...core.target] : [0, 0];
    camera.pan = { x: 0, y: 0 };
    camera.scale = 24;
    schedulePersist(true);
    scheduler.invalidate(FLAGS.CAMERA | FLAGS.SELECTION, 'reset');
  };
  window.setTacticalMapLayer = (name, enabled) => {
    if (name === 'vision') layers.vision = typeof enabled === 'string' ? enabled : (enabled ? 'selected' : 'off');
    else if (name in layers) layers[name] = Boolean(enabled);
    else return;
    if (!['selected', 'all', 'off'].includes(layers.vision)) layers.vision = 'selected';
    syncLayerControls();
    schedulePersist();
    scheduler.invalidate(FLAGS.LAYERS | FLAGS.SELECTION, `layer:${name}`);
  };
  window.setTacticalVisionMode = value => window.setTacticalMapLayer('vision', value);
  window.setTacticalMapTargetMode = enabled => {
    pickMode = Boolean(enabled);
    previewTarget = pickMode ? currentCenterCell() : null;
    window.updateDashboardTargetMode?.(pickMode);
    updateCoordinateReadouts();
    scheduler.invalidate(FLAGS.SELECTION, pickMode ? 'target-mode' : 'target-cancel');
  };
  window.getTacticalMapStats = debugEnabled
    ? () => ({ ...stats, scheduler: scheduler.state(), entityModels: entityModels.size, entityViews: entityViews.size })
    : () => null;

  function render(view) {
    if (!ensureRenderer()) {
      setRendererNotice('地图渲染器初始化失败 · 人工任务仍可操作', { blocking: true, error: true });
      return;
    }
    updateData(view);
  }
  window.renderTacticalMap = render;

  const resizeObserver = new ResizeObserver(resizeSurface);
  resizeObserver.observe(viewport);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) scheduler.cancel();
    else {
      resizeSurface();
      scheduler.invalidate(FLAGS.ALL, 'visibility-resume');
      scheduler.resume();
    }
  });
  window.addEventListener('beforeunload', persistNow, { once: true });
  if (window.DashboardReplay?.selected) render(window.DashboardReplay.selected);
})();
