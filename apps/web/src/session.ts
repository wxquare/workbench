import { ref } from 'vue'
import { ApiClient } from './api'

const storageKey = 'workbench-bearer-token'
const legacyStorageKey = 'dod-bearer-token'
const storedToken = sessionStorage.getItem(storageKey) ?? sessionStorage.getItem(legacyStorageKey) ?? ''
if (storedToken) sessionStorage.setItem(storageKey, storedToken)
sessionStorage.removeItem(legacyStorageKey)
export const bearerToken = ref(storedToken)
export function setBearerToken(value: string) {
  bearerToken.value = value.trim().replace(/^Bearer\s+/i, '')
  if (bearerToken.value) sessionStorage.setItem(storageKey, bearerToken.value)
  else sessionStorage.removeItem(storageKey)
}
export const api = new ApiClient(() => bearerToken.value)
