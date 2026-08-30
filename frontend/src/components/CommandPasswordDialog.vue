<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { COMMAND_PASSWORD_STORAGE_KEY, useDashboardStore } from '../state/dashboard'

const dashboard = useDashboardStore()
const password = ref('')
const saving = ref(false)
const storageKey = COMMAND_PASSWORD_STORAGE_KEY

function readPassword() {
  try { return window.localStorage.getItem(storageKey) || '' } catch { return '' }
}

function focusPassword() { void nextTick(() => document.getElementById('password')?.focus()) }

watch(() => dashboard.authDialogOpen.value, open => {
  if (open) {
    password.value = readPassword()
    focusPassword()
  }
})

async function submit() {
  if (!password.value.trim() || saving.value) return
  saving.value = true
  try {
    const authenticated = await dashboard.login(password.value)
    if (authenticated) dashboard.closeAuthDialog()
  } finally { saving.value = false }
}

function clearCache() {
  try { window.localStorage.removeItem(storageKey) } catch { /* private browsing may deny storage */ }
  password.value = ''
  dashboard.clearAuth()
}

function closeOnEscape(event: KeyboardEvent) {
  if (event.key === 'Escape' && dashboard.authDialogOpen.value) dashboard.closeAuthDialog()
}

onMounted(() => {
  password.value = readPassword()
  window.addEventListener('keydown', closeOnEscape)
})
onUnmounted(() => window.removeEventListener('keydown', closeOnEscape))
</script>

<template>
  <Transition name="auth-dialog">
    <div v-if="dashboard.authDialogOpen.value" id="commandPasswordDialog" class="auth-dialog-backdrop" @click.self="dashboard.closeAuthDialog">
      <section class="auth-dialog" role="dialog" aria-modal="true" aria-labelledby="authDialogTitle" aria-describedby="authDialogDescription">
        <header class="auth-dialog-head">
          <div><span class="eyebrow">COMMAND ACCESS</span><h2 id="authDialogTitle">配置作战口令</h2></div>
          <button id="authDialogClose" class="dialog-close" type="button" aria-label="关闭口令配置" @click="dashboard.closeAuthDialog">×</button>
        </header>
        <div class="auth-dialog-body">
          <p id="authDialogDescription" class="auth-dialog-note">口令会缓存于当前浏览器的 localStorage；提交时只发送给当前命令中心做认证，不写入日志或其他服务。</p>
          <label class="auth-field" for="password"><span>管理员口令</span><input id="password" v-model="password" type="password" autocomplete="current-password" placeholder="输入命令中心口令" @keyup.enter="submit"></label>
          <p id="loginState" class="auth-dialog-state" :class="{ error: dashboard.loginState.value.includes('失败') }" aria-live="polite">{{ dashboard.loginState.value || '保存后将立即验证口令。' }}</p>
        </div>
        <footer class="auth-dialog-actions"><button class="neutral" type="button" @click="clearCache">清除本机缓存</button><button id="login" class="primary-action" type="button" :disabled="saving || !password.trim()" @click="submit">{{ saving ? '认证中…' : '保存并认证' }}</button></footer>
      </section>
    </div>
  </Transition>
</template>
