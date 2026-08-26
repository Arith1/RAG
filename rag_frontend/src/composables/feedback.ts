import { reactive } from 'vue'

export interface ToastItem {
  id: number
  message: string
  kind: 'success' | 'error'
}

interface ConfirmRequest {
  message: string
  detail?: string
  confirmText: string
  cancelText: string
  danger: boolean
  resolve: (value: boolean) => void
}

const state = reactive({
  toasts: [] as ToastItem[],
  confirm: null as ConfirmRequest | null,
})

let toastId = 0

function dismiss(id: number) {
  const idx = state.toasts.findIndex((t) => t.id === id)
  if (idx >= 0) state.toasts.splice(idx, 1)
}

function notify(message: string, kind: 'success' | 'error' = 'success', duration = 3200) {
  const id = ++toastId
  state.toasts.push({ id, message, kind })
  if (duration > 0) window.setTimeout(() => dismiss(id), duration)
  return id
}

function confirm(opts: {
  message: string
  detail?: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}): Promise<boolean> {
  return new Promise((resolve) => {
    state.confirm = {
      message: opts.message,
      detail: opts.detail,
      confirmText: opts.confirmText ?? '确认',
      cancelText: opts.cancelText ?? '取消',
      danger: opts.danger ?? false,
      resolve,
    }
  })
}

function resolveConfirm(value: boolean) {
  const req = state.confirm
  state.confirm = null
  req?.resolve(value)
}

export function useFeedback() {
  return { state, notify, dismiss, confirm, resolveConfirm }
}
