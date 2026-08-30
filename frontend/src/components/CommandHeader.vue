<template>
  <header class="command-bar">
    <div class="brand"><span class="eyebrow">ARENA HERO</span><h1>作战指挥中心</h1></div>
    <div class="top-metrics" aria-label="实时作战状态">
      <div class="metric"><span>TICK</span><strong id="tick">—</strong></div>
      <div class="metric"><span>资源 / 容量</span><strong id="resources">—</strong></div>
      <div class="metric"><span>策略模式</span><strong id="mode">—</strong><button id="modeCausality" class="mode-causality" type="button" title="等待策略因果链">查看判定链</button></div>
      <div class="metric"><span>单位在线</span><strong id="unitCount">—</strong></div>
    </div>
    <div id="status" class="status">正在获取状态</div>
  </header>
</template>
