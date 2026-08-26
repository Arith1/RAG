<script setup lang="ts">
import { ref } from 'vue'
import type { SourceItem } from '../api/client'

const props = defineProps<{ sources: SourceItem[] }>()

const open = ref(false)
const expanded = ref<number | null>(null)

function basename(path: string | null): string {
  return String(path ?? '').split('/').pop() || '未知来源'
}

function toggle(i: number) {
  expanded.value = expanded.value === i ? null : i
}
</script>

<template>
  <div v-if="props.sources.length" class="source-strip">
    <button class="source-toggle" :aria-expanded="open" @click="open = !open">
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" :class="{ flipped: open }">
        <path d="M5 6.5 8 9.5l3-3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      来源引用
      <span class="count">{{ props.sources.length }}</span>
      <span class="hint">来自你的文档 · 他人共享</span>
    </button>

    <div v-if="open" class="source-list">
      <div
        v-for="s in props.sources"
        :key="s.index"
        class="source-item"
      >
        <button class="source-head" @click="toggle(s.index)">
          <span class="idx">{{ s.index }}</span>
          <span class="name">{{ basename(s.source) }}</span>
          <span v-if="s.question" class="q-tag" :title="s.question">子问题</span>
          <span v-if="s.score != null" class="score">{{ s.score.toFixed(3) }}</span>
          <svg viewBox="0 0 16 16" width="12" height="12" class="chev" :class="{ flipped: expanded === s.index }" aria-hidden="true">
            <path d="M5 6.5 8 9.5l3-3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <div v-if="expanded === s.index" class="snippet">
          <p v-if="s.content">{{ s.content }}</p>
          <p v-else class="no-snippet">该来源未返回内容片段。</p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.source-strip {
  margin-top: 14px;
  border-top: 1px solid var(--border);
  padding-top: 10px;
}
.source-toggle {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--text-2);
  font-size: 12.5px;
  font-weight: 500;
  padding: 3px 6px;
  margin-left: -6px;
  border-radius: var(--radius-xs);
}
.source-toggle:hover { color: var(--text); background: var(--surface-3); }
.source-toggle svg, .chev { transition: transform 0.18s ease; }
.flipped { transform: rotate(180deg); }
.count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: var(--radius-pill);
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
}
.hint { color: var(--text-3); font-weight: 400; }

.source-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.source-item {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  overflow: hidden;
}
.source-head {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  color: var(--text-2);
  font-size: 13px;
  text-align: left;
}
.source-head:hover { background: var(--surface-3); }
.idx {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  flex: none;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 600;
}
.name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--font-mono);
  font-size: 12.5px;
  color: var(--text);
}
.q-tag {
  flex: none;
  font-size: 11px;
  color: var(--accent);
  background: var(--accent-soft);
  padding: 2px 7px;
  border-radius: var(--radius-pill);
}
.score {
  flex: none;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-3);
}
.chev { color: var(--text-3); }
.snippet {
  padding: 10px 12px;
  border-top: 1px solid var(--border);
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.65;
}
.snippet p { margin: 0; }
.no-snippet { color: var(--text-3); font-style: italic; }
</style>
