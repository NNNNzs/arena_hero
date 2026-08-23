/* Camera math, rulers, and the on-demand dirty-frame scheduler. */
(() => {
  'use strict';
  const TacticalMap = window.TacticalMap = window.TacticalMap || {};
  const { LIMITS, clamp, isCell } = TacticalMap;
  TacticalMap.niceStep = minimum => {
    if (!Number.isFinite(minimum) || minimum <= 1) return 1;
    const power = 10 ** Math.floor(Math.log10(minimum));
    for (const factor of [1, 2, 5, 10]) if (factor * power >= minimum) return factor * power;
    return power * 10;
  };
  TacticalMap.axisTicks = (minimum, maximum, scale, { maxLabels = LIMITS.axisLabels, minPixels = 64 } = {}) => {
    const lower = Math.min(minimum, maximum), upper = Math.max(minimum, maximum);
    const required = Math.max(minPixels / Math.max(0.001, scale), (upper - lower) / Math.max(1, maxLabels - 1));
    const step = TacticalMap.niceStep(required), values = [], first = Math.ceil(lower / step) * step;
    for (let value = first; value <= upper + step * 1e-9 && values.length < maxLabels; value += step) values.push(Object.is(value, -0) ? 0 : Math.round(value));
    return { step, values };
  };
  TacticalMap.normaliseCamera = (value, fallback = {}) => {
    const source = value && typeof value === 'object' ? value : {};
    const fallbackAnchor = isCell(fallback.anchor) ? fallback.anchor : [0, 0];
    const fallbackPan = fallback.pan && Number.isFinite(fallback.pan.x) && Number.isFinite(fallback.pan.y) ? fallback.pan : { x: 0, y: 0 };
    return {
      anchor: isCell(source.anchor) ? source.anchor.map(Number) : [...fallbackAnchor],
      scale: clamp(Number.isFinite(source.scale) ? source.scale : (fallback.scale || 24), LIMITS.minScale, LIMITS.maxScale),
      pan: source.pan && Number.isFinite(source.pan.x) && Number.isFinite(source.pan.y) ? { x: source.pan.x, y: source.pan.y } : { ...fallbackPan },
    };
  };
  TacticalMap.visibleBounds = (camera, size, marginPixels = 0) => ({
    minX: camera.anchor[0] + (-marginPixels - size.w / 2 - camera.pan.x) / camera.scale,
    maxX: camera.anchor[0] + (size.w + marginPixels - size.w / 2 - camera.pan.x) / camera.scale,
    minY: camera.anchor[1] + (-marginPixels - size.h / 2 - camera.pan.y) / camera.scale,
    maxY: camera.anchor[1] + (size.h + marginPixels - size.h / 2 - camera.pan.y) / camera.scale,
  });
  TacticalMap.reconcileKeys = (existingKeys, incomingKeys) => {
    const existing = new Set(existingKeys || []), incoming = new Set(incomingKeys || []);
    return { added: [...incoming].filter(key => !existing.has(key)), kept: [...incoming].filter(key => existing.has(key)), removed: [...existing].filter(key => !incoming.has(key)) };
  };
  TacticalMap.createDirtyScheduler = ({ requestFrame, cancelFrame, now, draw, isHidden = () => false, maxFps = LIMITS.maxFps }) => {
    const minimumFrameMs = 1000 / maxFps; let frameId = 0, flags = 0, animationUntil = 0, lastDrawAt = -Infinity; const reasons = new Set();
    const schedule = () => { if (!frameId && !isHidden()) frameId = requestFrame(run); };
    function run(timestamp) {
      frameId = 0; if (isHidden()) return;
      const animating = timestamp < animationUntil;
      if (timestamp - lastDrawAt + 0.25 < minimumFrameMs) { if (flags || animating) schedule(); return; }
      if (!flags && !animating) return;
      const frameFlags = flags | (animating ? TacticalMap.FLAGS.ANIMATION : 0), frameReasons = [...reasons];
      flags = 0; reasons.clear(); lastDrawAt = timestamp; draw(frameFlags, frameReasons, timestamp);
      if (flags || timestamp < animationUntil) schedule();
    }
    return { invalidate(nextFlags, reason = 'update') { flags |= nextFlags; reasons.add(reason); schedule(); }, animate(duration = LIMITS.animationMs, reason = 'movement') { animationUntil = Math.max(animationUntil, now() + duration); flags |= TacticalMap.FLAGS.ANIMATION; reasons.add(reason); schedule(); }, cancel() { if (frameId) cancelFrame(frameId); frameId = 0; }, resume() { if (flags || now() < animationUntil) schedule(); }, state() { return { pending: Boolean(frameId), flags, animationUntil, lastDrawAt }; } };
  };
  TacticalMap.worldPoint = (state, cell) => [state.size.w / 2 + state.camera.pan.x + (cell[0] - state.camera.anchor[0]) * state.camera.scale, state.size.h / 2 + state.camera.pan.y + (cell[1] - state.camera.anchor[1]) * state.camera.scale];
  TacticalMap.worldCoordinate = (state, x, y) => [(x - state.size.w / 2 - state.camera.pan.x) / state.camera.scale + state.camera.anchor[0], (y - state.size.h / 2 - state.camera.pan.y) / state.camera.scale + state.camera.anchor[1]];
  TacticalMap.worldCell = (state, x, y) => TacticalMap.worldCoordinate(state, x, y).map(Math.round);
})();
