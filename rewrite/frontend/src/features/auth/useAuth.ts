import { readonly, ref } from 'vue'
import { api, type Principal } from '../../shared/api'

const token = ref(sessionStorage.getItem('helpdesk.token') ?? '')
const principal = ref<Principal | null>(null)

function clear() {
  token.value = ''
  principal.value = null
  sessionStorage.removeItem('helpdesk.token')
}

export function useAuth() {
  async function restore() {
    if (!token.value) return
    try { principal.value = await api<Principal>('/auth/me', token.value) }
    catch (error) { clear(); throw error }
  }
  async function login(username: string, password: string) {
    const result = await api<{ token: string }>('/auth/login', '', { method: 'POST', body: JSON.stringify({ username, password }) })
    token.value = result.token
    sessionStorage.setItem('helpdesk.token', result.token)
    await restore()
  }
  async function logout() {
    try { await api<void>('/auth/logout', token.value, { method: 'POST' }) }
    finally { clear() }
  }
  return { token: readonly(token), principal: readonly(principal), login, logout, restore, clear }
}
