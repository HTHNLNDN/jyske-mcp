import { defineStore } from 'pinia'
import { api } from '../api'

// Its own store, not folded into a combined dashboard.js — mirrors stores/tip.js:
// each widget's data stays independently owned so a widget stays portable/decoupled.
export const useBudgetsStore = defineStore('budgets', {
  state: () => ({
    budgets: [],
    loaded: false,
    // Mid-category breakdown per top-level category, keyed by `category`.
    breakdowns: {},
    // Line items per sub-category, keyed by `${category}::${mid ?? '__uncat__'}`.
    lineItems: {},
    // Full top-level → mid-level category taxonomy, for the recategorize picker.
    categoryTree: null,
    categoryTreeLoading: false,
  }),

  actions: {
    // Fetches budget status once. Safe to call repeatedly — only the first
    // call hits the network, later calls are no-ops via `loaded`.
    async load() {
      if (this.loaded) return
      try {
        const data = await api.getBudgetStatus()
        this.budgets = data?.budgets ?? []
      } catch (_) {
        this.budgets = []
      }
      this.loaded = true
    },

    // Force-refreshes the always-visible budget totals, bypassing the
    // `loaded` guard above — used after a recategorize so the top-level
    // progress bars reflect the change immediately.
    async reloadStatus() {
      this.loaded = false
      await this.load()
    },

    // Idempotent — only the first call hits the network.
    async loadCategoryTree() {
      if (this.categoryTree != null || this.categoryTreeLoading) return
      this.categoryTreeLoading = true
      try {
        const data = await api.getCategoryTree()
        this.categoryTree = data?.tree ?? {}
      } catch (_) {
        this.categoryTree = {}
      }
      this.categoryTreeLoading = false
    },

    // Drops cached breakdown/line-item data for a top-level category so the
    // next view re-fetches fresh totals. No-op if categoryTop is null/undefined
    // (e.g. a transaction that had no prior category).
    invalidateCategory(categoryTop) {
      if (categoryTop == null) return
      delete this.breakdowns[categoryTop]
      for (const key of Object.keys(this.lineItems)) {
        if (key.startsWith(`${categoryTop}::`)) delete this.lineItems[key]
      }
    },

    // Re-files a transaction (and every historical transaction from the same
    // merchant — an all-time, all-merchant rewrite done server-side) under a
    // new top/mid category, then invalidates and refreshes any cached views
    // affected on both the old and new side.
    async recategorize({ transactionId, categoryTop, categoryMid }) {
      const res = await api.recategorize(transactionId, categoryTop, categoryMid)
      if (!res.ok) return res // let the caller show the error, don't mutate state on failure
      this.invalidateCategory(res.data.old_category_top)
      this.invalidateCategory(res.data.new_category_top)
      await this.reloadStatus()
      return res
    },

    // Creates (or, since the backend upserts on category_top/category_mid/
    // period, edits) a budget. Only the top-level status list needs
    // refreshing — spending breakdowns/line items are unaffected by a budget
    // limit changing.
    async createBudget({ categoryTop, categoryMid, limitAmount, period }) {
      const res = await api.createBudget(categoryTop, categoryMid, limitAmount, period)
      if (!res.ok) return res
      await this.reloadStatus()
      return res
    },

    async deleteBudget(budgetId) {
      const res = await api.deleteBudget(budgetId)
      if (!res.ok) return res
      await this.reloadStatus()
      return res
    },

    // Same idempotent-cache shape as `load()`, keyed by top-level category
    // name — only the first call per category hits the network.
    async loadBreakdown(category) {
      const existing = this.breakdowns[category]
      if (existing && (existing.loading || existing.data != null)) return
      this.breakdowns[category] = { loading: true, data: existing?.data ?? null }
      try {
        const data = await api.getBudgetBreakdown(category)
        this.breakdowns[category] = { loading: false, data }
      } catch (_) {
        this.breakdowns[category] = { loading: false, data: null }
      }
    },

    // Same idempotent-cache shape, keyed by category + mid (or uncategorized).
    async loadLineItems(category, mid, uncategorized) {
      const key = `${category}::${uncategorized ? '__uncat__' : mid ?? '__uncat__'}`
      const existing = this.lineItems[key]
      if (existing && (existing.loading || existing.data != null)) return
      this.lineItems[key] = { loading: true, data: existing?.data ?? null }
      try {
        const data = await api.getBudgetTransactions(category, { mid, uncategorized })
        this.lineItems[key] = { loading: false, data }
      } catch (_) {
        this.lineItems[key] = { loading: false, data: null }
      }
    },

    reset() {
      this.budgets = []
      this.loaded = false
      this.breakdowns = {}
      this.lineItems = {}
      this.categoryTree = null
      this.categoryTreeLoading = false
    },
  },
})
