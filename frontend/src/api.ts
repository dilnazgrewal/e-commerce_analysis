// Small fetch wrapper: the dashboard token lives in localStorage (per browser), sent as a Bearer header.

const KEY = 'offeriq-token'

export function getToken(): string {
  try {
    return localStorage.getItem(KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string) {
  try {
    if (token) localStorage.setItem(KEY, token)
    else localStorage.removeItem(KEY)
  } catch {
    // storage blocked (private window): the session just won't survive a reload
  }
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${getToken()}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!res.ok) {
    let message = res.statusText
    try {
      const data = await res.json()
      const detail = data.detail
      message = typeof detail === 'string' ? detail : (detail?.errors?.join('; ') ?? JSON.stringify(detail))
    } catch {
      // not JSON
    }
    if (res.status === 401) window.dispatchEvent(new Event('offeriq-logout'))
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<T>
}

export const inr = (n: number | null | undefined, digits = 0) =>
  n === null || n === undefined
    ? '—'
    : (n < 0 ? '−₹' : '₹') +
      Math.abs(n).toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: 0 })

export const num = (n: number | null | undefined, digits = 0) =>
  n === null || n === undefined ? '—' : n.toLocaleString('en-IN', { maximumFractionDigits: digits })

export const pct = (n: number | null | undefined, digits = 1) =>
  n === null || n === undefined ? '—' : `${(n * 100).toFixed(digits)}%`

export const OFFER_LABELS: Record<string, string> = {
  CART_NUDGE: 'Cart reminder',
  BONUS_POINTS: 'Bonus points',
  FREE_SHIPPING: 'Free delivery',
  PCT_OFF: '% off',
  FLAT_OFF: '₹ off',
  UPI_CASHBACK: 'UPI cashback',
  WIN_BACK: 'Win-back',
  SECOND_ORDER: '2nd-order ₹ off',
  POINTS_EXPIRY: 'Points expiry',
  EARLY_ACCESS: 'Early access',
}
