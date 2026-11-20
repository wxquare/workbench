export interface UsageSummary {
  model_calls?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
  amount_micros?: number | null
  currency?: string | null
  usage_pending?: boolean
  cost_pending?: boolean
  known_costs?: Record<string, number>
}

const formatAmount = (amount: number, currency: string) =>
  `${currency} ${(amount / 1_000_000).toFixed(6)}`

export function formatUsageCost(summary: UsageSummary): string {
  const known = Object.entries(summary.known_costs ?? {}).filter(([, amount]) =>
    Number.isSafeInteger(amount) && amount >= 0,
  )
  if (known.length > 1) {
    return known.map(([currency, amount]) => formatAmount(amount, currency)).join(' + ')
  }
  if (
    summary.cost_pending ||
    !Number.isSafeInteger(summary.amount_micros) ||
    !summary.currency
  ) {
    if (known.length === 1) {
      const [currency, amount] = known[0]
      return `已确认 ${formatAmount(amount, currency)}；其余计费待确认`
    }
    return '计费待确认'
  }
  return formatAmount(summary.amount_micros as number, summary.currency)
}
