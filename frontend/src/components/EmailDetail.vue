<script setup>
import { computed, ref } from 'vue'
import { api } from '../api'
import DraftReview from './DraftReview.vue'

const props = defineProps({ record: Object })
const emit = defineEmits(['changed'])

const error = ref('')
const busy = ref(false)
const feedback = ref('')
const report = ref(null)

const triage = computed(() => props.record.triage)
const labels = ['ignore', 'notify', 'respond']

async function guard(fn) {
  error.value = ''
  busy.value = true
  try {
    return await fn()
  } catch (e) {
    error.value = e.message
  } finally {
    busy.value = false
  }
}

const decide = (decision) => guard(async () => { await api.decide(props.record.id, decision); emit('changed') })
const correct = (label) => guard(async () => { await api.correctTriage(props.record.id, label); emit('changed') })
const sendFeedback = () =>
  guard(async () => {
    report.value = await api.feedback(props.record.id, feedback.value)
    feedback.value = ''
    emit('changed')
  })
const show = (v) => (typeof v === 'string' ? v : JSON.stringify(v, null, 2))
</script>

<template>
  <div class="stack">
    <div class="card">
      <div class="row">
        <h2 class="grow">{{ record.email.subject || '(no subject)' }}</h2>
        <span class="badge" :class="record.status">{{ record.status }}</span>
      </div>
      <p class="muted">From {{ record.email.author || '?' }} → {{ record.email.to || '?' }}</p>
      <pre>{{ record.email.email_thread }}</pre>
    </div>

    <div v-if="record.status === 'error'" class="card error-text">{{ record.error }}</div>

    <div class="card" v-if="triage">
      <h3>Triage</h3>
      <div class="row">
        <span class="badge" :class="triage.classification">{{ triage.classification }}</span>
        <span class="muted grow">{{ triage.reasoning }}</span>
      </div>
      <div class="row correct">
        <span class="muted">Wrong? Teach the router (episodic memory):</span>
        <button
          v-for="l in labels"
          :key="l"
          :disabled="busy || l === (record.triage_correction ?? triage.classification)"
          @click="correct(l)"
        >
          {{ l }}
        </button>
        <span v-if="record.triage_correction" class="muted">saved: {{ record.triage_correction }}</span>
      </div>
    </div>

    <DraftReview
      v-if="record.status === 'pending_review' && record.draft"
      :draft="record.draft"
      @decide="decide"
    />
    <div v-else-if="record.status === 'processing'" class="card muted">Working on it…</div>

    <div class="card" v-if="record.actions.length">
      <h3>Agent actions</h3>
      <ul class="actions">
        <li v-for="(a, i) in record.actions" :key="i">
          <code>{{ a.name }}</code>
          <pre class="mono muted">{{ show(a.args) }}</pre>
          <div v-if="a.result" class="result">→ {{ a.result }}</div>
        </li>
      </ul>
      <p v-if="record.reply"><strong>Agent:</strong> {{ record.reply }}</p>
    </div>

    <div class="card" v-if="record.status === 'done' && record.triage">
      <h3>Feedback → prompt optimizer (procedural memory)</h3>
      <textarea v-model="feedback" placeholder="e.g. Too formal. Write short, casual replies." />
      <div class="row">
        <button class="primary" :disabled="busy || !feedback.trim()" @click="sendFeedback">
          {{ busy ? 'Optimizing…' : 'Send feedback' }}
        </button>
        <span class="muted">Rewrites prompts as new versions; triage changes pass the eval gate.</span>
      </div>
      <pre v-if="report" class="mono report">{{ show(report) }}</pre>
      <p v-for="(f, i) in record.feedback" :key="i" class="muted">Sent: “{{ f }}”</p>
    </div>

    <p v-if="error" class="error-text">{{ error }}</p>
  </div>
</template>

<style scoped>
.correct { margin-top: 12px; }
.actions { list-style: none; padding: 0; margin: 0; }
.actions li { padding: 8px 0; border-top: 1px solid var(--border); }
.actions li:first-child { border-top: 0; }
.result { font-size: 0.9em; margin-top: 4px; }
.report { margin-top: 12px; background: var(--tint); padding: 10px; border-radius: 8px; }
</style>
