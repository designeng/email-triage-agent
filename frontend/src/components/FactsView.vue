<script setup>
import { ref } from 'vue'
import { api } from '../api'

const facts = ref([])
const query = ref('')
const error = ref('')

async function load() {
  error.value = ''
  try {
    facts.value = await api.facts(query.value.trim())
  } catch (e) {
    error.value = e.message
  }
}
load()
</script>

<template>
  <div class="stack">
    <p class="muted">Semantic memory: facts the agent saved about people and preferences.</p>
    <form class="row" @submit.prevent="load">
      <input v-model="query" class="grow" placeholder="Semantic search, e.g. Sarah" />
      <button>Search</button>
    </form>
    <p v-if="error" class="error-text">{{ error }}</p>
    <div class="card">
      <p v-if="!facts.length" class="muted">No facts yet.</p>
      <p v-for="f in facts" :key="f.id" class="fact">
        {{ f.content }} <span v-if="f.score != null" class="muted">({{ f.score.toFixed(2) }})</span>
      </p>
    </div>
  </div>
</template>

<style scoped>
.fact { margin: 0; padding: 6px 0; border-top: 1px solid var(--border); }
.fact:first-child { border-top: 0; }
</style>
