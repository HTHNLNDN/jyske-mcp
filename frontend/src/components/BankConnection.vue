<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const status = ref(null)      // raw /consent/status payload, or null while loading/unfetched
const loading = ref(false)
const reconnecting = ref(false)
const reconnectError = ref('')

const showSyncControl = ref(false)
const monthsBack = ref(3)
const syncing = ref(false)
const syncError = ref(null)
const syncResult = ref(null)

async function loadStatus() {
  loading.value = true
  reconnectError.value = ''
  try {
    status.value = await api.getConsentStatus()
  } catch (err) {
    // Logged (not just swallowed) so a real auth/network failure here is
    // distinguishable from the "never fetched" null state at a glance.
    console.error('Could not load connection status:', err)
    status.value = null
  } finally {
    loading.value = false
  }
}

// This component is only mounted once the "Bank connection" section is
// selected (SettingsPanel v-if's it in/out), so a plain onMounted refetch
// gives the same "always fresh when shown" behaviour the old inline
// watch(open, immediate: true) provided.
onMounted(loadStatus)

async function reconnect() {
  if (reconnecting.value) return
  reconnecting.value = true
  reconnectError.value = ''
  try {
    const res = await api.startConsent()
    if (res && res.auth_url) {
      window.location.href = res.auth_url
      return
    }
    reconnectError.value = 'Could not start reconnection — no redirect URL returned.'
  } catch (_) {
    reconnectError.value = 'Could not reach the server — check your connection and try again.'
  } finally {
    reconnecting.value = false
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function pollSyncStatus() {
  // Poll until the backend reports the sync has finished. A network hiccup
  // here surfaces via the same error styling as the reconnect flow rather
  // than silently spinning forever.
  while (true) {
    const s = await api.getSyncStatus()
    if (s.error) {
      syncError.value = s.error
      break
    }
    if (!s.running) {
      syncResult.value = s.last_sync
      break
    }
    await sleep(2000)
  }
}

async function triggerSync() {
  if (syncing.value) return
  syncing.value = true
  syncError.value = null
  syncResult.value = null
  try {
    const res = await api.triggerSync(monthsBack.value)
    // A 409 (already_running) just means someone else kicked off a sync —
    // fall through to polling instead of treating it as a failure.
    if (!res.ok && res.status !== 409) {
      syncError.value = 'Could not start sync — check your connection and try again.'
      return
    }
    await pollSyncStatus()
    await loadStatus()
  } catch (_) {
    syncError.value = 'Could not reach the server — check your connection and try again.'
  } finally {
    syncing.value = false
  }
}

// syncResult.details is the raw `errors` column value — a JSON string of
// {accounts: [{product, truncated, ...}], errors: [...]} — or already null/
// empty when nothing was recorded yet. Parse defensively: a malformed or
// missing value just means "no truncation info", not an error to surface.
function truncatedReasons(result) {
  if (!result || !result.details) return []
  let parsed = result.details
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed)
    } catch (_) {
      return []
    }
  }
  const accounts = parsed && Array.isArray(parsed.accounts) ? parsed.accounts : []
  return accounts
    .filter((acct) => acct && acct.truncated)
    .map((acct) => `${acct.product}: ${acct.truncated}`)
}

function formatDate(value) {
  if (!value) return null
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleDateString('da-DK', { year: 'numeric', month: 'short', day: 'numeric' })
}

// Pill label/emphasis — no accent colors; urgency reads through weight and
// fill (outline → semibold outline → solid ink), matching BudgetCard's
// over/warning treatment rather than a red/amber/green convention.
const PILL = {
  connected:      { label: 'Connected',   class: 'border border-hairline text-fog' },
  expiring_soon:  { label: 'Expiring',    class: 'border border-ink text-ink font-semibold' },
  expired:        { label: 'Expired',     class: 'bg-ink text-paper font-semibold' },
  none:           { label: 'Not connected', class: 'border border-hairline border-dashed text-fog' },
}

function pill(s) {
  return PILL[s] ?? PILL.none
}
</script>

<template>
  <h2 class="text-sm font-condensed font-medium text-ink mb-3">Bank connection</h2>

  <div v-if="loading" class="text-xs font-mono text-fog/60 py-4">Checking connection…</div>

  <template v-else-if="status">
    <div class="flex items-center gap-2 mb-3">
      <span
        class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded text-[11px] font-mono tracking-[0.06em] uppercase"
        :class="pill(status.status).class"
      >{{ pill(status.status).label }}</span>
      <span
        v-if="status.status === 'expiring_soon' && status.days_remaining != null"
        class="text-xs font-mono text-fog"
      >{{ status.days_remaining }} day{{ status.days_remaining === 1 ? '' : 's' }} left</span>
    </div>

    <div v-if="status.valid_until" class="text-xs font-mono text-fog mb-4">
      Valid until {{ formatDate(status.valid_until) }}
    </div>

    <div v-if="status.accounts && status.accounts.length" class="mb-5 border-t border-hairline pt-3">
      <p class="text-[11px] font-mono tracking-[0.1em] uppercase text-fog mb-2">Accounts</p>
      <ul class="space-y-1.5">
        <li
          v-for="acct in status.accounts"
          :key="acct.uid"
          class="flex items-baseline justify-between text-xs"
        >
          <span class="text-ink">{{ acct.product }}</span>
          <span class="font-mono text-fog tabular-nums">{{ acct.iban }}</span>
        </li>
      </ul>
    </div>

    <div v-if="status.last_error" class="mb-4 text-xs text-ink border border-hairline px-3 py-2">
      {{ status.last_error }}
    </div>

    <button
      @click="reconnect"
      :disabled="reconnecting"
      class="w-full text-sm font-medium bg-ink text-paper py-2.5 rounded transition-opacity disabled:opacity-50"
    >{{ reconnecting ? 'Redirecting…' : 'Reconnect' }}</button>

    <p v-if="reconnectError" class="mt-2 text-xs text-ink font-semibold">{{ reconnectError }}</p>

    <button
      @click="showSyncControl = !showSyncControl"
      :disabled="syncing"
      class="w-full mt-2 text-sm font-medium border border-hairline text-ink py-2.5 rounded transition-opacity disabled:opacity-50"
    >{{ syncing ? 'Syncing…' : 'Sync now' }}</button>

    <div v-if="showSyncControl" class="mt-3 border-t border-hairline pt-3">
      <div class="flex items-end gap-2">
        <label class="flex-1">
          <span class="block text-[11px] font-mono tracking-[0.1em] uppercase text-fog mb-1">Months back</span>
          <input
            type="number"
            min="1"
            max="12"
            v-model.number="monthsBack"
            :disabled="syncing"
            class="w-full text-sm font-mono tabular-nums bg-paperdim border border-hairline rounded px-2.5 py-1.5 disabled:opacity-50"
          />
        </label>
        <button
          @click="triggerSync"
          :disabled="syncing"
          class="text-sm font-medium bg-ink text-paper py-1.5 px-4 rounded transition-opacity disabled:opacity-50"
        >{{ syncing ? 'Fetching…' : 'Fetch' }}</button>
      </div>

      <p v-if="syncResult" class="mt-2 text-xs font-mono text-fog tabular-nums">
        Fetched {{ syncResult.transactions_fetched }} transaction{{ syncResult.transactions_fetched === 1 ? '' : 's' }}
        across {{ syncResult.accounts_synced }} account{{ syncResult.accounts_synced === 1 ? '' : 's' }}
        ({{ syncResult.new_transactions }} new)
      </p>

      <p
        v-if="syncResult && truncatedReasons(syncResult).length"
        class="mt-2 text-xs font-mono text-ink border border-hairline px-3 py-2"
      >
        ⚠ Some history may be incomplete — {{ truncatedReasons(syncResult).join('; ') }}. It will finish on the next sync.
      </p>

      <p v-if="syncError" class="mt-2 text-xs text-ink font-semibold">{{ syncError }}</p>
    </div>
  </template>

  <div v-else class="text-xs font-mono text-fog/60 py-4">Could not load connection status.</div>
</template>
