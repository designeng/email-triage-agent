<script setup>
import { ref, watch } from 'vue'
import { api } from '../api'
import EmailDetail from './EmailDetail.vue'

const props = defineProps({ emails: Array })
const emit = defineEmits(['changed'])

const selectedId = ref(null)
const samples = ref([])
const sampleId = ref('')
const compose = ref({ author: '', subject: '', email_thread: '' })
const showCompose = ref(false)
const error = ref('')

api.samples().then((s) => {
  samples.value = s
  sampleId.value = s[0]?.id ?? ''
}).catch(() => {})

// Keep a sensible selection: first email needing review, else the newest.
watch(
  () => props.emails,
  (list) => {
    if (selectedId.value && list.some((e) => e.id === selectedId.value)) return
    selectedId.value = (list.find((e) => e.status === 'pending_review') ?? list[0])?.id ?? null
  },
  { immediate: true },
)

async function run(fn) {
  error.value = ''
  try {
    const record = await fn()
    if (record?.id) selectedId.value = record.id
    emit('changed')
  } catch (e) {
    error.value = e.message
  }
}

const addSample = () => {
  const { id, ...email } = samples.value.find((s) => s.id === sampleId.value)
  return run(() => api.addEmail(email))
}
const addComposed = () =>
  run(async () => {
    const record = await api.addEmail({ to: 'me', ...compose.value })
    compose.value = { author: '', subject: '', email_thread: '' }
    showCompose.value = false
    return record
  })

const label = { processing: 'processing…', pending_review: 'needs review', done: 'done', error: 'error' }
</script>

<template>
  <div class="layout">
    <aside class="stack">
      <div class="card stack">
        <h3>Add email (manual)</h3>
        <div class="row">
          <select v-model="sampleId" class="grow">
            <option v-for="s in samples" :key="s.id" :value="s.id">{{ s.subject }}</option>
          </select>
          <button :disabled="!sampleId" @click="addSample">Add</button>
        </div>
        <button @click="showCompose = !showCompose">{{ showCompose ? 'Cancel' : 'Compose…' }}</button>
        <form v-if="showCompose" class="stack" @submit.prevent="addComposed">
          <input v-model="compose.author" placeholder="From" />
          <input v-model="compose.subject" placeholder="Subject" required />
          <textarea v-model="compose.email_thread" placeholder="Body" required />
          <button class="primary">Send to assistant</button>
        </form>
        <p v-if="error" class="error-text">{{ error }}</p>
      </div>

      <div class="card list">
        <p v-if="!emails.length" class="muted">No emails yet. With the Google backend they arrive via Gmail push.</p>
        <button
          v-for="e in emails"
          :key="e.id"
          class="item"
          :class="{ active: e.id === selectedId }"
          @click="selectedId = e.id"
        >
          <span class="subject">{{ e.email.subject || '(no subject)' }}</span>
          <span class="muted from">{{ e.email.author }}</span>
          <span class="row">
            <span class="badge" :class="e.status">{{ label[e.status] }}</span>
            <span v-if="e.triage" class="badge" :class="e.triage.classification">{{ e.triage.classification }}</span>
          </span>
        </button>
      </div>
    </aside>

    <section>
      <EmailDetail
        v-if="selectedId"
        :key="selectedId"
        :record="emails.find((e) => e.id === selectedId)"
        @changed="emit('changed')"
      />
      <p v-else class="muted">Select an email.</p>
    </section>
  </div>
</template>

<style scoped>
.layout { display: grid; grid-template-columns: 340px 1fr; gap: 20px; align-items: start; }
@media (max-width: 800px) { .layout { grid-template-columns: 1fr; } }
.list { padding: 6px; display: flex; flex-direction: column; gap: 4px; max-height: 70vh; overflow: auto; }
.item { display: flex; flex-direction: column; gap: 4px; align-items: flex-start; text-align: left; border-color: transparent; width: 100%; }
.item.active { background: var(--tint); border-color: var(--accent); }
.subject { font-weight: 600; }
.from { font-size: 0.85em; }
</style>
