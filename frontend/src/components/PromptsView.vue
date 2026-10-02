<script setup>
import { ref } from 'vue'
import { api } from '../api'

const prompts = ref([])
const open = ref({})
const error = ref('')

async function load() {
  prompts.value = await api.prompts()
}
async function act(fn) {
  error.value = ''
  try {
    await fn()
    await load()
  } catch (e) {
    error.value = e.message
  }
}
load().catch((e) => (error.value = e.message))
</script>

<template>
  <div class="stack">
    <p class="muted">
      Procedural memory: prompts live in the store as versions. Optimizer output is a candidate; triage
      candidates become active only if they pass the eval gate.
    </p>
    <p v-if="error" class="error-text">{{ error }}</p>
    <div v-for="p in prompts" :key="p.name" class="card">
      <div class="row">
        <h2 class="grow mono">{{ p.name }}</h2>
        <span class="muted">active v{{ p.active }}</span>
        <button @click="act(() => api.rollbackPrompt(p.name))">Roll back</button>
      </div>
      <div v-for="v in [...p.versions].reverse()" :key="v.version" class="version">
        <div class="row">
          <strong>v{{ v.version }}</strong>
          <span class="badge" :class="v.status === 'active' ? 'done' : v.status === 'rejected' ? 'error' : 'processing'">
            {{ v.status }}
          </span>
          <span class="muted grow">{{ v.source }}<span v-if="v.note"> · {{ v.note.slice(0, 80) }}</span></span>
          <button @click="open[p.name + v.version] = !open[p.name + v.version]">
            {{ open[p.name + v.version] ? 'Hide' : 'Show' }}
          </button>
          <button v-if="v.version !== p.active" @click="act(() => api.activatePrompt(p.name, v.version))">
            Activate
          </button>
        </div>
        <pre v-if="open[p.name + v.version]" class="mono text">{{ v.prompt }}</pre>
        <p v-if="v.reason" class="error-text">{{ v.reason }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.version { padding: 8px 0; border-top: 1px solid var(--border); }
.text { background: var(--tint); padding: 10px; border-radius: 8px; margin-top: 8px; }
</style>
