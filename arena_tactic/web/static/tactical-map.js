/* Pixi.js WebGL tactical renderer with automatic 2D Canvas fallback.
 * Guarantees 100% tactical map rendering across all environments (WebGL / Software Canvas).
 */
(() => {
  const viewport = document.getElementById('map-viewport');
  if (!viewport) return;

  const COLORS = {
    bg: '#081018',
    grid: '#294054',
    gridMajor: '#3b6379',
    text: '#8fa0b3',
    cyan: '#54dfcb',
    blue: '#58a6ff',
    violet: '#b98cff',
    amber: '#f4bd61',
    red: '#ff6b7a',
    obstacle: '#263746',
  };

  const KINDS = {
    CORE: ['核心', COLORS.amber],
    WORKER: ['工人', COLORS.blue],
    VANGUARD: ['先锋', COLORS.violet],
    RANGER: ['游侠', COLORS.cyan],
    ENEMY: ['敌方', COLORS.red],
  };

  const point = p => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite);
  const idOf = o => String(o?.alias || o?.id || `${o?.enemy ? 'enemy' : 'object'}:${o?.kind}:${o?.position}`);
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const lerp = (a, b, t) => a + (b - a) * t;

  let mode = null; // 'pixi' | 'canvas'
  let canvas2d = null, ctx2d = null;
  let pixiApp = null, scene, gridLayer, terrainLayer, unitLayer, fxLayer, radarLayer, world;

  const units = new Map(), lastPositions = new Map();
  let currentView = null, anchor = [0, 0], scale = 24, baseScale = 24;
  let pan = { x: 0, y: 0 }, drag = null, focus = null, size = { w: 640, h: 360 };
  let lastTime = performance.now();

  function worldPoint(p) {
    return [
      size.w / 2 + pan.x + (p[0] - anchor[0]) * scale,
      size.h / 2 + pan.y - (p[1] - anchor[1]) * scale,
    ];
  }

  function resize() {
    const rect = viewport.getBoundingClientRect();
    size = { w: Math.max(320, rect.width || 640), h: Math.max(250, rect.height || 360) };
    if (mode === 'pixi' && pixiApp) {
      pixiApp.renderer.resize(size.w, size.h);
    } else if (canvas2d) {
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas2d.width = size.w * dpr;
      canvas2d.height = size.h * dpr;
      canvas2d.style.width = size.w + 'px';
      canvas2d.style.height = size.h + 'px';
      if (ctx2d) ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    drawAll();
  }

  function setupInteraction(targetEl) {
    targetEl.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      targetEl.setPointerCapture?.(e.pointerId);
      drag = { x: e.clientX, y: e.clientY, px: pan.x, py: pan.y };
    });
    targetEl.addEventListener('pointermove', e => {
      if (drag) {
        pan.x = drag.px + e.clientX - drag.x;
        pan.y = drag.py + e.clientY - drag.y;
        drawAll();
      }
    });
    const stopDrag = () => { drag = null; };
    targetEl.addEventListener('pointerup', stopDrag);
    targetEl.addEventListener('pointercancel', stopDrag);
    viewport.addEventListener('wheel', e => {
      e.preventDefault();
      zoomAt(e.deltaY < 0 ? 1.12 : 0.89, e.offsetX, e.offsetY);
    }, { passive: false });
    viewport.addEventListener('dblclick', e => {
      const target = nearestUnit(e.offsetX, e.offsetY, ['CORE', 'RANGER']);
      if (target) focusUnit(target);
    });
    new ResizeObserver(resize).observe(viewport);
  }

  function initCanvas2D() {
    viewport.innerHTML = '';
    canvas2d = document.createElement('canvas');
    canvas2d.className = 'map-canvas';
    canvas2d.setAttribute('aria-label', '当前可见战术地图 (2D模式)');
    viewport.appendChild(canvas2d);
    ctx2d = canvas2d.getContext('2d');
    setupInteraction(canvas2d);
    mode = 'canvas';
    resize();
    requestAnimationFrame(renderLoop);
    return true;
  }

  function initPixi() {
    if (!window.PIXI || !PIXI.utils?.isWebGLSupported?.()) return false;
    try {
      pixiApp = new PIXI.Application({
        background: 0x081018,
        antialias: true,
        resolution: Math.min(2, window.devicePixelRatio || 1),
        autoDensity: true,
        resizeTo: viewport,
      });
      viewport.innerHTML = '';
      viewport.appendChild(pixiApp.canvas || pixiApp.view);
      (pixiApp.canvas || pixiApp.view).setAttribute('aria-label', '当前可见战术地图 (WebGL模式)');
      scene = new PIXI.Container();
      world = new PIXI.Container();
      gridLayer = new PIXI.Container();
      terrainLayer = new PIXI.Container();
      unitLayer = new PIXI.Container();
      fxLayer = new PIXI.Container();
      radarLayer = new PIXI.Container();
      world.addChild(gridLayer, terrainLayer, fxLayer, unitLayer, radarLayer);
      scene.addChild(world);
      pixiApp.stage.addChild(scene);
      setupInteraction(pixiApp.canvas || pixiApp.view);
      pixiApp.ticker.add(() => {
        const now = performance.now(), dt = Math.min(80, now - lastTime);
        lastTime = now;
        animate(dt);
      });
      mode = 'pixi';
      resize();
      return true;
    } catch (err) {
      console.warn('Pixi WebGL init failed, falling back to 2D Canvas:', err);
      pixiApp = null;
      return false;
    }
  }

  function ensureRenderer() {
    if (mode) return true;
    if (initPixi()) return true;
    return initCanvas2D();
  }

  function zoomAt(factor, x, y) {
    const before = [(x - size.w / 2 - pan.x) / scale + anchor[0], anchor[1] - (y - size.h / 2 - pan.y) / scale];
    scale = Math.max(baseScale * 0.55, Math.min(baseScale * 4, scale * factor));
    const after = worldPoint(before);
    pan.x += x - after[0];
    pan.y += y - after[1];
    drawAll();
  }

  function nearestUnit(x, y, kinds) {
    let found = null, best = 24;
    for (const item of units.values()) {
      if (!kinds.includes(item.kind)) continue;
      const p = worldPoint(item.position);
      const d = Math.hypot(p[0] - x, p[1] - y);
      if (d < best) { best = d; found = item; }
    }
    return found;
  }

  function focusUnit(item) {
    anchor = [...item.position];
    pan = { x: 0, y: 0 };
    scale = baseScale * 1.35;
    focus = item.key;
    drawAll();
  }

  function updateData(view) {
    const map = view?.current?.map || {}, cc = view?.command_center || {};
    const aliases = new Map((cc.entities || []).map(e => [String(e.alias), e]));
    const friendly = (map.friendly || []).filter(o => point(o.position)).map(o => ({ ...o, ...(aliases.get(String(o.alias)) || {}) }));
    const enemies = (map.enemies || []).filter(o => point(o.position)).map(o => ({ ...o, kind: 'ENEMY', enemy: true }));
    const all = [...friendly, ...enemies];
    const core = friendly.find(o => o.kind === 'CORE');
    anchor = core?.position ? [...core.position] : (all[0]?.position ? [...all[0].position] : [0, 0]);

    const local = [...friendly];
    const far = [];
    for (const o of enemies) (dist(anchor, o.position) <= 28 ? local : far).push(o);
    for (const p of (map.resources || []).filter(point)) (dist(anchor, p) <= 28 ? local : far).push({ position: p, resource: true });
    for (const p of (map.obstacles || []).filter(point)) (dist(anchor, p) <= 28 ? local : far).push({ position: p, obstacle: true });
    if (point(map.beacon?.position)) (dist(anchor, map.beacon.position) <= 28 ? local : far).push({ position: map.beacon.position, beacon: true, label: '信标' });

    const span = Math.max(12, ...local.map(o => Math.max(Math.abs(o.position[0] - anchor[0]), Math.abs(o.position[1] - anchor[1])) * 2 + 6));
    baseScale = Math.max(9, Math.min(28, Math.min((size.w - 34) / span, (size.h - 34) / span)));
    if (!focus) scale = baseScale;

    for (const o of all) {
      const key = idOf(o);
      const old = lastPositions.get(key) || o.position;
      lastPositions.set(key, [...o.position]);
      let item = units.get(key);
      if (!item) {
        item = { key, position: [...old], target: [...o.position], kind: o.kind, data: o, progress: 1 };
        units.set(key, item);
      }
      item.target = [...o.position];
      item.data = o;
      item.kind = o.kind;
      item.progress = 0;
    }
    for (const [key, item] of units) {
      if (!all.some(o => idOf(o) === key)) units.delete(key);
    }
    currentView = { map, friendly, enemies, local, far, mode: view?.current?.mode_label || '当前态势' };
  }

  /* ================= 2D Canvas Fallback Renderer ================= */
  function drawCanvas() {
    if (!ctx2d || !currentView) return;
    const ctx = ctx2d;
    ctx.fillStyle = COLORS.bg;
    ctx.fillRect(0, 0, size.w, size.h);

    // 1. Grid
    const radius = Math.ceil(Math.max(size.w, size.h) / scale / 2) + 2;
    for (let i = -radius; i <= radius; i++) {
      const isMajor = i % 5 === 0;
      ctx.strokeStyle = isMajor ? COLORS.gridMajor : COLORS.grid;
      ctx.lineWidth = isMajor ? 1 : 0.5;
      ctx.globalAlpha = isMajor ? 0.72 : 0.35;

      const x = size.w / 2 + pan.x + i * scale;
      const y = size.h / 2 + pan.y + i * scale;

      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, size.h);
      ctx.stroke();

      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(size.w, y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1.0;

    // 2. Terrain (Obstacles, Resources, Beacon)
    for (const o of currentView.local) {
      const p = worldPoint(o.position);
      if (o.obstacle) {
        ctx.fillStyle = COLORS.obstacle;
        ctx.strokeStyle = COLORS.gridMajor;
        ctx.lineWidth = 1;
        ctx.fillRect(p[0] - scale * 0.34, p[1] - scale * 0.34, scale * 0.68, scale * 0.68);
        ctx.strokeRect(p[0] - scale * 0.34, p[1] - scale * 0.34, scale * 0.68, scale * 0.68);
      }
      if (o.resource) {
        ctx.fillStyle = COLORS.amber;
        ctx.strokeStyle = COLORS.amber;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(p[0], p[1] - scale * 0.35);
        ctx.lineTo(p[0] + scale * 0.35, p[1]);
        ctx.lineTo(p[0], p[1] + scale * 0.35);
        ctx.lineTo(p[0] - scale * 0.35, p[1]);
        ctx.closePath();
        ctx.globalAlpha = 0.85;
        ctx.fill();
        ctx.globalAlpha = 1.0;
        ctx.stroke();
      }
      if (o.beacon) {
        ctx.strokeStyle = COLORS.cyan;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(p[0], p[1], scale * 0.52, 0, Math.PI * 2);
        ctx.stroke();
        ctx.fillStyle = COLORS.cyan;
        ctx.beginPath();
        ctx.arc(p[0], p[1], scale * 0.22, 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // 3. FX (Beams)
    const time = performance.now();
    for (const item of units.values()) {
      const o = item.data, target = point(o.target_cell) ? o.target_cell : null;
      if (o.kind === 'RANGER' && o.action === 'SHOOT' && target) {
        const a = worldPoint(item.position), b = worldPoint(target);
        ctx.strokeStyle = COLORS.cyan;
        ctx.lineWidth = 2 + Math.sin(time / 70) * 0.6;
        ctx.globalAlpha = 0.55;
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0], b[1]);
        ctx.stroke();
        ctx.globalAlpha = 1.0;
      }
      if (o.kind === 'WORKER' && o.action === 'HARVEST' && target) {
        const a = worldPoint(item.position), b = worldPoint(target);
        ctx.strokeStyle = COLORS.amber;
        ctx.lineWidth = 2;
        ctx.globalAlpha = 0.45 + Math.sin(time / 100) * 0.2;
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0], b[1]);
        ctx.stroke();
        ctx.globalAlpha = 1.0;
      }
    }

    // 4. Units
    for (const item of units.values()) {
      const p = worldPoint(item.position);
      const color = KINDS[item.kind]?.[1] || COLORS.blue;
      const o = item.data;
      const r = Math.max(7, Math.min(13, scale * 0.32));

      ctx.save();
      ctx.translate(p[0], p[1]);
      if (item.key === focus) ctx.scale(1.15, 1.15);
      if (o.status === 'BLOCKED') ctx.globalAlpha = 0.7;

      ctx.strokeStyle = color;
      ctx.fillStyle = color;

      if (item.kind === 'CORE') {
        ctx.lineWidth = 2;
        ctx.globalAlpha = 0.25;
        ctx.beginPath();
        ctx.arc(0, 0, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1.0;
        ctx.stroke();
      } else if (item.kind === 'ENEMY') {
        ctx.strokeStyle = COLORS.red;
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.85;
        ctx.beginPath();
        ctx.moveTo(0, -r);
        ctx.lineTo(r, 0);
        ctx.lineTo(0, r);
        ctx.lineTo(-r, 0);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.globalAlpha = 1.0;
      } else {
        ctx.lineWidth = 1.5;
        ctx.globalAlpha = 0.82;
        ctx.beginPath();
        ctx.arc(0, 0, r, 0, Math.PI * 2);
        ctx.fill();
        ctx.globalAlpha = 1.0;
        ctx.stroke();
      }

      // HP Bar
      const hp = Number(o.hp);
      if (Number.isFinite(hp)) {
        const maxHp = Math.max(1, Number(o.max_hp || hp));
        const ratio = Math.max(0, Math.min(1, hp / maxHp));
        ctx.fillStyle = 'rgba(255,107,122,0.35)';
        ctx.fillRect(-r, r + 4, r * 2, 2);
        ctx.fillStyle = COLORS.cyan;
        ctx.fillRect(-r, r + 4, r * 2 * ratio, 2);
      }

      // Action vector
      const target = point(o.destination) ? o.destination : (point(o.target_cell) ? o.target_cell : null);
      if (target) {
        const q = worldPoint(target);
        const dx = q[0] - p[0], dy = q[1] - p[1], len = Math.max(1, Math.hypot(dx, dy));
        ctx.strokeStyle = color;
        ctx.lineWidth = o.action === 'SHOOT' ? 2 : 1;
        ctx.globalAlpha = 0.65;
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.lineTo(dx / len * r * 2, dy / len * r * 2);
        ctx.stroke();
        ctx.globalAlpha = 1.0;
      }

      // Label
      ctx.font = '10px system-ui, Microsoft YaHei, sans-serif';
      ctx.fillStyle = color;
      const labelText = `${KINDS[item.kind]?.[0] || item.kind} ${item.kind === 'ENEMY' ? '' : String(o.alias || '').slice(-4)}`;
      ctx.fillText(labelText, r + 4, -r + 5);

      ctx.restore();
    }

    // 5. Radar
    for (const [index, o] of currentView.far.slice(0, 10).entries()) {
      const dx = o.position[0] - anchor[0], dy = o.position[1] - anchor[1];
      const len = Math.max(1, Math.hypot(dx, dy));
      const p = [
        size.w / 2 + pan.x + (dx / len) * (size.w / 2 - 18),
        size.h / 2 + pan.y - (dy / len) * (size.h / 2 - 18),
      ];
      const color = o.enemy ? COLORS.red : (o.beacon ? COLORS.cyan : COLORS.amber);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(p[0], p[1]);
      ctx.lineTo(p[0] - (dx / len) * 10 - (dy / len) * 5, p[1] + (dy / len) * 10 - (dx / len) * 5);
      ctx.lineTo(p[0] - (dx / len) * 10 + (dy / len) * 5, p[1] + (dy / len) * 10 + (dx / len) * 5);
      ctx.closePath();
      ctx.fill();

      ctx.font = '9px system-ui, Microsoft YaHei, sans-serif';
      const label = `${o.label || (o.enemy ? '敌方' : '远端')} · ${Math.round(len)}格`;
      ctx.fillText(label, Math.max(5, Math.min(size.w - 105, p[0] - 38)), Math.max(18, Math.min(size.h - 16, p[1] + (index % 2 ? 10 : -16))));
    }

    // HUD
    ctx.font = '10px system-ui, Microsoft YaHei, sans-serif';
    ctx.fillStyle = COLORS.cyan;
    ctx.fillText(`TACTICAL LINK  /  ${currentView.mode}  /  ${Math.round(scale)} px·格 (2D Canvas)`, 12, 20);
  }

  /* ================= Pixi WebGL Renderer ================= */
  function drawPixi() {
    if (!pixiApp || !currentView) return;
    // Pixi draw logic
    // Clear layers
    gridLayer.removeChildren();
    terrainLayer.removeChildren();
    fxLayer.removeChildren();
    unitLayer.removeChildren();
    radarLayer.removeChildren();

    const gGrid = new PIXI.Graphics();
    gGrid.beginFill(0x081018).drawRect(0, 0, size.w, size.h).endFill();
    const radius = Math.ceil(Math.max(size.w, size.h) / scale / 2) + 2;
    for (let i = -radius; i <= radius; i++) {
      const isMajor = i % 5 === 0;
      const x = size.w / 2 + pan.x + i * scale;
      const y = size.h / 2 + pan.y + i * scale;
      gGrid.lineStyle(isMajor ? 1 : 0.5, isMajor ? 0x3b6379 : 0x294054, isMajor ? 0.72 : 0.35);
      gGrid.moveTo(x, 0).lineTo(x, size.h);
      gGrid.moveTo(0, y).lineTo(size.w, y);
    }
    gridLayer.addChild(gGrid);

    const gTerrain = new PIXI.Graphics();
    for (const o of currentView.local) {
      const p = worldPoint(o.position);
      if (o.obstacle) gTerrain.lineStyle(1, 0x3b6379, 1).beginFill(0x263746).drawRect(p[0] - scale * 0.34, p[1] - scale * 0.34, scale * 0.68, scale * 0.68).endFill();
      if (o.resource) gTerrain.lineStyle(1.5, 0xf4bd61, 1).beginFill(0xf4bd61, 0.85).drawPolygon([p[0], p[1] - scale * 0.35, p[0] + scale * 0.35, p[1], p[0], p[1] + scale * 0.35, p[0] - scale * 0.35, p[1]]).endFill();
      if (o.beacon) {
        gTerrain.lineStyle(2, 0x54dfcb, 0.9).drawCircle(p[0], p[1], scale * 0.52);
        gTerrain.beginFill(0x54dfcb, 0.8).drawCircle(p[0], p[1], scale * 0.22).endFill();
      }
    }
    terrainLayer.addChild(gTerrain);

    // Units in Pixi
    for (const item of units.values()) {
      const p = worldPoint(item.position), o = item.data;
      const color = item.kind === 'CORE' ? 0xf4bd61 : (item.kind === 'RANGER' ? 0x54dfcb : (item.kind === 'VANGUARD' ? 0xb98cff : (item.kind === 'ENEMY' ? 0xff6b7a : 0x58a6ff)));
      const r = Math.max(7, Math.min(13, scale * 0.32));
      const holder = new PIXI.Container();
      const g = new PIXI.Graphics();
      if (item.kind === 'CORE') g.lineStyle(2, color, 1).beginFill(color, 0.25).drawCircle(0, 0, r).endFill();
      else if (item.kind === 'ENEMY') g.lineStyle(1, 0xff6b7a, 1).beginFill(color, 0.85).drawPolygon([0, -r, r, 0, 0, r, -r, 0]).endFill();
      else g.lineStyle(1.5, color, 1).beginFill(color, 0.82).drawCircle(0, 0, r).endFill();

      const hp = Number(o.hp);
      if (Number.isFinite(hp)) {
        const ratio = Math.max(0, Math.min(1, hp / Math.max(1, Number(o.max_hp || hp))));
        g.beginFill(0xff6b7a, 0.35).drawRect(-r, r + 4, r * 2, 2).endFill();
        g.beginFill(0x54dfcb).drawRect(-r, r + 4, r * 2 * ratio, 2).endFill();
      }
      holder.addChild(g);

      const label = new PIXI.Text(`${KINDS[item.kind]?.[0] || item.kind} ${item.kind === 'ENEMY' ? '' : String(o.alias || '').slice(-4)}`, {
        fontFamily: 'system-ui, Microsoft YaHei, sans-serif',
        fontSize: 10,
        fill: color,
      });
      label.position.set(r + 4, -r - 3);
      holder.addChild(label);

      holder.position.set(p[0], p[1]);
      if (o.status === 'BLOCKED') holder.alpha = 0.7;
      if (item.key === focus) holder.scale.set(1.15);
      unitLayer.addChild(holder);
    }

    const hud = new PIXI.Text(`TACTICAL LINK  /  ${currentView.mode}  /  ${Math.round(scale)} px·格 (Pixi WebGL)`, {
      fontFamily: 'system-ui, Microsoft YaHei, sans-serif',
      fontSize: 10,
      fill: 0x54dfcb,
    });
    hud.position.set(12, 10);
    radarLayer.addChild(hud);
  }

  function drawAll() {
    if (mode === 'pixi') drawPixi();
    else if (mode === 'canvas') drawCanvas();
  }

  function animate(dt) {
    if (!currentView) return;
    for (const item of units.values()) {
      const t = Math.min(1, dt / 180);
      item.position[0] = lerp(item.position[0], item.target[0], t);
      item.position[1] = lerp(item.position[1], item.target[1], t);
    }
    drawAll();
  }

  function renderLoop() {
    if (mode === 'canvas') {
      const now = performance.now(), dt = Math.min(80, now - lastTime);
      lastTime = now;
      animate(dt);
      requestAnimationFrame(renderLoop);
    }
  }

  function render(view) {
    if (!ensureRenderer()) {
      viewport.textContent = '态势渲染器初始化中…';
      return;
    }
    updateData(view);
    drawAll();
  }

  window.renderTacticalMap = render;
  if (window.DashboardReplay?.selected) render(window.DashboardReplay.selected);
})();
