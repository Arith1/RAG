<script setup lang="ts">
import { useFeedback } from '../composables/feedback'

const feedback = useFeedback()

function dismiss(id: number) {
  feedback.dismiss(id)
}
</script>

<template>
  <div class="feedback-layer">
    <!-- Toasts -->
    <TransitionGroup name="toast" tag="div" class="toast-stack" aria-live="polite">
      <div v-for="t in feedback.state.toasts" :key="t.id" class="toast" :class="t.kind" role="status">
        <span class="toast-dot" aria-hidden="true"></span>
        <span class="toast-text">{{ t.message }}</span>
        <button class="toast-close" aria-label="关闭" @click="dismiss(t.id)">
          <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
            <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          </svg>
        </button>
      </div>
    </TransitionGroup>

    <!-- Confirm dialog -->
    <div v-if="feedback.state.confirm" class="confirm-backdrop" @click.self="feedback.resolveConfirm(false)">
      <div class="confirm-panel" role="dialog" aria-modal="true" :aria-label="feedback.state.confirm.message">
        <h3 class="confirm-title">{{ feedback.state.confirm.message }}</h3>
        <p v-if="feedback.state.confirm.detail" class="confirm-detail">{{ feedback.state.confirm.detail }}</p>
        <div class="confirm-actions">
          <button class="btn btn-subtle" @click="feedback.resolveConfirm(false)">
            {{ feedback.state.confirm.cancelText }}
          </button>
          <button
            class="btn"
            :class="feedback.state.confirm.danger ? 'btn-danger' : 'btn-primary'"
            @click="feedback.resolveConfirm(true)"
          >
            {{ feedback.state.confirm.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.feedback-layer {
  position: fixed;
  inset: 0;
  z-index: 60;
  pointer-events: none;
}

/* Toasts */
.toast-stack {
  position: absolute;
  left: 50%;
  bottom: 26px;
  transform: translateX(-50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  width: min(92vw, 460px);
}
.toast {
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  max-width: 100%;
  padding: 11px 14px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-md);
  font-size: 13.5px;
  color: var(--text);
}
.toast-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex: none;
}
.toast.success .toast-dot { background: var(--ok); }
.toast.error .toast-dot { background: var(--err); }
.toast-text { line-height: 1.5; }
.toast-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--text-3);
  margin-left: auto;
}
.toast-close:hover { background: var(--surface-3); color: var(--text); }

.toast-enter-active, .toast-leave-active { transition: opacity 0.2s ease, transform 0.2s ease; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateY(8px); }

/* Confirm */
.confirm-backdrop {
  pointer-events: auto;
  position: absolute;
  inset: 0;
  background: rgba(23, 32, 58, 0.28);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--page-pad);
}
.confirm-panel {
  width: 100%;
  max-width: 400px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-md);
  padding: 24px 22px 20px;
}
.confirm-title {
  margin: 0 0 8px;
  font-size: 16px;
  font-weight: 650;
  letter-spacing: -0.01em;
  color: var(--text);
}
.confirm-detail {
  margin: 0 0 18px;
  font-size: 13.5px;
  color: var(--text-2);
  line-height: 1.6;
}
.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
