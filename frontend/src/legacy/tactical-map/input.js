/* Pointer, wheel, keyboard, selection, and tactical target-picking controls. */
(() => {
  'use strict';
  const T = window.TacticalMap = window.TacticalMap || {};
  const { FLAGS, LIMITS, clamp, sameCell, worldCell, worldCoordinate, worldPoint } = T;
  T.nearestEntity = (state, x, y) => { let nearest = null, best = 24; for (const model of state.entityModels.values()) { const point = worldPoint(state, model.position), distance = Math.hypot(point[0] - x, point[1] - y); if (distance < best) { best = distance; nearest = model; } } return nearest; };
  T.selectEntity = (state, key, { focus = false, notify = false } = {}) => { const model = state.entityModels.get(key); if (!model) return false; state.selectedKey = key; if (focus) { state.trackingKey = key; state.camera.anchor = [...model.target]; state.camera.pan = { x: 0, y: 0 }; state.camera.scale = clamp(Math.max(state.camera.scale, 24), LIMITS.minScale, LIMITS.maxScale); T.schedulePersist(state, true); state.scheduler.invalidate(FLAGS.CAMERA | FLAGS.SELECTION, 'focus-unit'); } else state.scheduler.invalidate(FLAGS.SELECTION, 'select-unit'); T.updateCoordinateReadouts(state); if (notify && !model.enemy) window.selectDashboardUnit?.(model.data.alias); return true; };
  T.focusCell = (state, cell) => { if (!T.isCell(cell)) return; state.trackingKey = null; state.camera.anchor = cell.map(Number); state.camera.pan = { x: 0, y: 0 }; T.schedulePersist(state, true); state.scheduler.invalidate(FLAGS.CAMERA, 'focus-cell'); };
  T.zoomAt = (state, factor, x, y) => { const before = worldCoordinate(state, x, y), next = clamp(state.camera.scale * factor, LIMITS.minScale, LIMITS.maxScale); if (Math.abs(next - state.camera.scale) < .001) return; state.camera.scale = next; const after = worldPoint(state, before); state.camera.pan.x += x - after[0]; state.camera.pan.y += y - after[1]; T.schedulePersist(state); state.scheduler.invalidate(FLAGS.CAMERA, 'zoom'); };
  T.bindInput = state => {
    const position = event => { const rect = state.viewport.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; };
    const control = target => target instanceof Element && Boolean(target.closest('button,input,select,label,.map-coordinate-strip,.map-debug-hud'));
    const click = (x, y) => { const entity = T.nearestEntity(state, x, y); if (entity) return T.selectEntity(state, entity.key, { focus: true, notify: true }); const cell = worldCell(state, x, y); window.updateDashboardMapCursor?.(cell); if (state.pickMode) { state.pickMode = false; state.lockedTarget = cell; state.previewTarget = null; window.setDashboardMapTarget?.(cell); window.updateDashboardTargetMode?.(false); state.scheduler.invalidate(FLAGS.SELECTION, 'target-locked'); } };
    state.viewport.addEventListener('pointerdown', event => { if (event.button !== 0 || state.contextLost || control(event.target)) return; const p = position(event); state.viewport.setPointerCapture?.(event.pointerId); state.drag = { ...p, startPan: { ...state.camera.pan }, moved: false }; });
    state.viewport.addEventListener('pointermove', event => { const p = position(event), cell = worldCell(state, p.x, p.y); window.updateDashboardMapCursor?.(cell); if (state.pickMode && !sameCell(state.previewTarget, cell)) { state.previewTarget = cell; state.scheduler.invalidate(FLAGS.SELECTION, 'target-preview'); T.updateCoordinateReadouts(state); } if (state.drag) { state.drag.moved ||= Math.hypot(p.x - state.drag.x, p.y - state.drag.y) > 4; if (state.drag.moved) { state.trackingKey = null; state.camera.pan.x = state.drag.startPan.x + p.x - state.drag.x; state.camera.pan.y = state.drag.startPan.y + p.y - state.drag.y; T.schedulePersist(state); state.scheduler.invalidate(FLAGS.CAMERA, 'pan'); } return; } const hover = T.nearestEntity(state, p.x, p.y)?.key || null; if (hover !== state.hoveredKey) { state.hoveredKey = hover; state.scheduler.invalidate(FLAGS.SELECTION, 'hover'); } });
    const stop = event => { if (!state.drag) return; const prior = state.drag; state.drag = null; if (prior.moved) T.schedulePersist(state, true); else if (!control(event.target)) { const p = position(event); click(p.x, p.y); } };
    state.viewport.addEventListener('pointerup', stop); state.viewport.addEventListener('pointercancel', stop);
    state.viewport.addEventListener('wheel', event => { if (state.contextLost || control(event.target)) return; event.preventDefault(); const p = position(event); T.zoomAt(state, clamp(Math.exp(-event.deltaY * .0015), .82, 1.22), p.x, p.y); window.clearTimeout(state.wheelPersistTimer); state.wheelPersistTimer = window.setTimeout(() => T.schedulePersist(state, true), 130); }, { passive: false });
    state.viewport.addEventListener('dblclick', event => { if (control(event.target)) return; const p = position(event), entity = T.nearestEntity(state, p.x, p.y); if (entity) T.selectEntity(state, entity.key, { focus: true, notify: true }); });
    state.viewport.addEventListener('keydown', event => { const step = 42; if (event.key === 'Escape' && state.pickMode) { state.pickMode = false; state.previewTarget = null; window.updateDashboardTargetMode?.(false); state.scheduler.invalidate(FLAGS.SELECTION, 'target-cancel'); } else if (event.key === '+' || event.key === '=') T.zoomAt(state, 1.18, state.size.w / 2, state.size.h / 2); else if (event.key === '-' || event.key === '_') T.zoomAt(state, .84, state.size.w / 2, state.size.h / 2); else if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) { state.trackingKey = null; state.camera.pan.x += event.key === 'ArrowLeft' ? step : (event.key === 'ArrowRight' ? -step : 0); state.camera.pan.y += event.key === 'ArrowUp' ? step : (event.key === 'ArrowDown' ? -step : 0); T.schedulePersist(state); state.scheduler.invalidate(FLAGS.CAMERA, 'keyboard-pan'); } else return; event.preventDefault(); });
    /* Mobile touch gestures: pinch-zoom (two-finger) + single-finger drag pan with tap-vs-drag threshold. */
    var DRAG_THRESHOLD = 8;
    var touch = { id: -1, startX: 0, startY: 0, moved: false, pinching: false, startDist: 0, startScale: 0, centerX: 0, centerY: 0 };
    var touchPos = touch => { var rect = state.viewport.getBoundingClientRect(); return { x: touch.clientX - rect.left, y: touch.clientY - rect.top }; };
    var pinchDist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
    state.viewport.addEventListener('touchstart', function (e) {
      if (state.contextLost || control(e.target)) return;
      var ct = e.changedTouches;
      if (e.touches.length === 2 && ct.length > 0) {
        e.preventDefault();
        if (touch.id >= 0) { touch.id = -1; touch.moved = false; }
        touch.pinching = true;
        touch.startDist = pinchDist(e.touches[0], e.touches[1]);
        touch.startScale = state.camera.scale;
        touch.centerX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        touch.centerY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
      } else if (e.touches.length === 1 && !touch.pinching) {
        var t = ct[0], p = touchPos(t);
        touch.id = t.identifier;
        touch.startX = p.x;
        touch.startY = p.y;
        touch.moved = false;
        state.drag = { x: p.x, y: p.y, startPan: { x: state.camera.pan.x, y: state.camera.pan.y }, moved: false };
      }
    }, { passive: false });
    state.viewport.addEventListener('touchmove', function (e) {
      if (state.contextLost || control(e.target)) return;
      if (touch.pinching && e.touches.length === 2) {
        e.preventDefault();
        var dist = pinchDist(e.touches[0], e.touches[1]);
        var factor = dist / touch.startDist;
        var rect = state.viewport.getBoundingClientRect();
        var cx = (e.touches[0].clientX + e.touches[1].clientX) / 2 - rect.left;
        var cy = (e.touches[0].clientY + e.touches[1].clientY) / 2 - rect.top;
        var targetScale = clamp(touch.startScale * factor, LIMITS.minScale, LIMITS.maxScale);
        var scaleFactor = targetScale / state.camera.scale;
        if (Math.abs(scaleFactor - 1) > .001) T.zoomAt(state, scaleFactor, cx, cy);
      } else if (touch.id >= 0) {
        var changed = e.changedTouches;
        for (var i = 0; i < changed.length; i++) {
          if (changed[i].identifier !== touch.id) continue;
          var p = touchPos(changed[i]);
          var dx = p.x - touch.startX, dy = p.y - touch.startY;
          if (!touch.moved && Math.hypot(dx, dy) <= DRAG_THRESHOLD) break;
          if (!touch.moved) { touch.moved = true; if (state.drag) state.drag.moved = true; }
          e.preventDefault();
          if (state.drag) {
            state.trackingKey = null;
            state.camera.pan.x = state.drag.startPan.x + dx;
            state.camera.pan.y = state.drag.startPan.y + dy;
            T.schedulePersist(state);
            state.scheduler.invalidate(FLAGS.CAMERA, 'pan');
          }
          break;
        }
      }
    }, { passive: false });
    var touchEnd = function (e) {
      if (touch.pinching) {
        if (e.touches.length < 2) { touch.pinching = false; T.schedulePersist(state, true); }
        return;
      }
      if (touch.id < 0) return;
      var changed = e.changedTouches;
      for (var i = 0; i < changed.length; i++) {
        if (changed[i].identifier !== touch.id) continue;
        touch.id = -1;
        if (!touch.moved) {
          state.drag = null;
          click(touch.startX, touch.startY);
        } else {
          if (state.drag) state.drag.moved = true;
          T.schedulePersist(state, true);
        }
        state.drag = null;
        break;
      }
    };
    state.viewport.addEventListener('touchend', touchEnd, { passive: false });
    state.viewport.addEventListener('touchcancel', touchEnd, { passive: false });
  };
})();
