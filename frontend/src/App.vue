<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from './api'
import InboxView from './components/InboxView.vue'
import PromptsView from './components/PromptsView.vue'
import FactsView from './components/FactsView.vue'

const tab = ref('inbox')
const emails = ref([])
const status = ref(null)
const loadError = ref('')
let timer

async function refresh() {
  try {
    emails.value = await api.emails()
    loadError.value = ''
  } catch (e) {
    loadError.value = `API unreachable: ${e.message}`
  }
}

const pending = computed(() => emails.value.filter((e) => e.status === 'pending_review').length)

onMounted(async () => {
  api.status().then((s) => (status.value = s)).catch(() => {})
  await refresh()
  timer = setInterval(refresh, 3000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <header class="top">
    <strong>Email assistant</strong>
    <nav>
      <button :class="{ primary: tab === 'inbox' }" @click="tab = 'inbox'">
        Inbox<span v-if="pending"> · {{ pending }} to review</span>
      </button>
      <button :class="{ primary: tab === 'prompts' }" @click="tab = 'prompts'">Prompts</button>
      <button :class="{ primary: tab === 'facts' }" @click="tab = 'facts'">Facts</button>
    </nav>
    <span class="muted grow end" v-if="status">
      user {{ status.user }} · {{ status.backend }}<span v-if="status.gmail_push"> · Gmail push</span>
    </span>
  </header>
  <p v-if="loadError" class="error-text banner">{{ loadError }}</p>
  <main>
    <InboxView v-if="tab === 'inbox'" :emails="emails" @changed="refresh" />
    <PromptsView v-else-if="tab === 'prompts'" />
    <FactsView v-else />
  </main>
</template>

<style scoped>
.top { display: flex; gap: 16px; align-items: center; padding: 10px 20px; background: var(--surface); border-bottom: 1px solid var(--border); flex-wrap: wrap; }
nav { display: flex; gap: 6px; }
.end { text-align: right; }
.banner { margin: 0; padding: 8px 20px; }
main { max-width: 1200px; margin: 0 auto; padding: 20px; }
</style>
