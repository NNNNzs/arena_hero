<template>
  <section class="map-panel" aria-label="战术地图工作区">
    <div class="map-heading"><div><span class="eyebrow">LIVE BATTLESPACE / GRID CONTROL</span><h2>战术地图</h2></div><div class="map-status"><span id="mapModeBadge" class="tag">实时态势</span><span id="mapSummary">—</span><span id="rendererStatus">渲染器待命</span></div></div>
    <div class="map-toolbar" aria-label="地图控件">
      <div class="tool-group"><button id="mapZoomIn" class="map-tool" title="放大">＋</button><button id="mapZoomOut" class="map-tool" title="缩小">－</button><button id="mapReset" class="map-tool" title="重置视口">归中</button></div>
      <label class="layer-toggle"><input id="layerFog" type="checkbox" checked> 迷雾</label>
      <label class="layer-toggle">视野 <select id="visionMode" aria-label="视野显示模式"><option value="selected">选中 + 核心</option><option value="all">全部单位</option><option value="off">关闭</option></select></label>
      <label class="layer-toggle"><input id="layerCoordinates" type="checkbox" checked> 边尺</label>
      <label class="layer-toggle"><input id="layerLabels" type="checkbox" checked> 标注</label>
      <span class="map-legend"><i class="legend-unit worker"></i>工人 <i class="legend-unit vanguard"></i>先锋 <i class="legend-unit ranger"></i>游侠 <i class="legend-resource"></i>资源</span>
      <button id="mapPickTarget" class="neutral map-order">地图选点下令</button>
    </div>
    <div id="map-viewport" class="map" role="application" tabindex="0" aria-label="战术地图；拖拽平移，滚轮缩放，方向键移动视口">
      <div id="map-stage" class="map-stage"><div class="map-loading">正在初始化战术态势渲染器…</div></div>
      <div id="map-axis-x" class="map-axis map-axis-x" aria-hidden="true"></div><div id="map-axis-y" class="map-axis map-axis-y" aria-hidden="true"></div>
      <div class="map-reticle" aria-hidden="true"></div>
      <div class="map-coordinate-strip"><span id="mapCenterCoordinate">中心 —</span><span id="mapCursor">光标 —</span><span id="mapSelectionCoordinate">选中 —</span><span id="mapTargetCoordinate">目标 —</span></div>
      <div id="mapTargetMode" class="map-target-mode" hidden>选点下单 · 移动光标预览路径 · 单击锁定目标</div>
      <div id="mapRendererState" class="map-renderer-state" hidden></div><pre id="mapDebugHud" class="map-debug-hud" hidden></pre>
    </div>
  </section>
</template>
