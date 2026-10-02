<script setup>
import { ref } from 'vue'

const props = defineProps({ draft: Object })
const emit = defineEmits(['decide'])

const mode = ref('view') // view | edit | feedback
const edited = ref({ to: props.draft.to, subject: props.draft.subject, content: props.draft.content })
const note = ref('')

function submitEdit() {
  const args = {}
  for (const k of ['to', 'subject', 'content']) if (edited.value[k] !== props.draft[k]) args[k] = edited.value[k]
  emit('decide', Object.keys(args).length ? { type: 'edit', args } : { type: 'accept' })
}
</script>

<template>
  <div class="card review">
    <h3>Draft awaiting your review — nothing is sent until you accept</h3>

    <template v-if="mode === 'edit'">
      <div class="stack">
        <input v-model="edited.to" placeholder="To" />
        <input v-model="edited.subject" placeholder="Subject" />
        <textarea v-model="edited.content" rows="10" />
      </div>
      <div class="row actions">
        <button class="primary" @click="submitEdit">Send edited</button>
        <button @click="mode = 'view'">Cancel</button>
      </div>
    </template>

    <template v-else>
      <p class="muted">To: {{ draft.to }}<br />Subject: {{ draft.subject }}</p>
      <pre class="body">{{ draft.content }}</pre>

      <template v-if="mode === 'feedback'">
        <textarea v-model="note" placeholder="What should the agent change?" />
        <div class="row actions">
          <button class="primary" :disabled="!note.trim()" @click="emit('decide', { type: 'response', text: note })">
            Ask for a new draft
          </button>
          <button @click="mode = 'view'">Cancel</button>
        </div>
      </template>
      <div v-else class="row actions">
        <button class="primary" @click="emit('decide', { type: 'accept' })">Accept &amp; send</button>
        <button @click="mode = 'edit'">Edit</button>
        <button @click="mode = 'feedback'">Feedback to agent</button>
        <button class="danger" @click="emit('decide', { type: 'ignore' })">Don't send</button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.review { border-color: var(--warn); }
.body { background: var(--tint); padding: 12px; border-radius: 8px; margin: 8px 0; }
.actions { margin-top: 12px; }
</style>
