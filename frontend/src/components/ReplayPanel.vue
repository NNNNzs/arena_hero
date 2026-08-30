<template>
  <section class="replay-panel" aria-label="作战时间轴">
    <div class="replay-heading"><h2>作战时间轴</h2><div><span id="replayState" class="muted">等待回放快照</span><strong id="replayTick" class="tick">—</strong></div></div>
    <div id="replayTrack" class="replay-track"><input id="replaySlider" type="range" min="0" max="0" value="0" step="1" aria-label="回放 Tick"><div id="replayMarkers" class="replay-markers" aria-label="关键战局事件"></div><div id="replayHover" class="replay-hover" role="status" aria-live="polite" hidden></div></div>
    <div class="replay-controls"><button id="replayStart" class="neutral" title="回到当前窗口起点">⏮</button><button id="replayPrev" class="neutral" title="上一帧；到达起点时加载更早历史">⏪</button><button id="replayPlay" class="neutral" title="播放或暂停">▶ 播放</button><button id="replayNext" class="neutral" title="下一帧">⏩</button><button id="replayLoadEarlier" class="neutral" title="加载更早的历史">↞ 更早</button><button id="replayLive" class="neutral" title="跟随实时">⏭ 实时</button><button id="eventDrawerToggle" class="neutral event-drawer-toggle" aria-expanded="false" aria-controls="eventDrawer">📜 日志 <span id="eventCountAll" class="event-count">0</span></button></div>
  </section>
</template>
