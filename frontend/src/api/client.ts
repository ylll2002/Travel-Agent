import type {
  ChatMessage,
  ChatResponse,
  Destination,
  Trip,
  TripCreatePayload,
  TripMemory,
  UserProfile,
} from './types';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api';

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers ?? {}),
    },
    ...options,
  });

  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const parsed: unknown = JSON.parse(body);
      if (parsed && typeof parsed === 'object' && 'detail' in parsed) {
        const value = (parsed as { detail: unknown }).detail;
        if (typeof value === 'string') detail = value;
      }
    } catch { /* 非JSON错误保留原始文本 */ }
    throw new Error(`请求失败 (${response.status}): ${detail}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

export const api = {
  listTrips: (userId?: string) =>
    request<Trip[]>(`/trips${userId ? `?user_id=${encodeURIComponent(userId)}` : ''}`),
  getTrip: (id: number) => request<Trip>(`/trips/${id}`),
  createTrip: (payload: TripCreatePayload) =>
    request<Trip>('/trips', { method: 'POST', body: JSON.stringify(payload) }),
  updateTrip: (id: number, payload: Partial<TripCreatePayload>) =>
    request<Trip>(`/trips/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  deleteTrip: (id: number) =>
    request<void>(`/trips/${id}`, { method: 'DELETE' }),
  listDestinations: () => request<Destination[]>('/destinations'),
  saveProfile: (userId: string, profile: UserProfile) =>
    request<UserProfile>('/profile', {
      method: 'POST',
      body: JSON.stringify({ user_id: userId, ...profile }),
    }),
  getProfile: (userId: string) => request<UserProfile>(`/profile/${userId}`),
  reportBehavior: (userId: string, action: string, target = '', detail = '') =>
    request<void>('/behavior-signals', {
      method: 'POST',
      body: JSON.stringify({ user_id: userId, action, target, detail }),
    }),
  plan: (payload: {
    user_id?: string;
    query?: string;
    destination?: string;
    start_date?: string;
    end_date?: string;
    profile?: unknown;
    basic?: unknown;
    modify?: unknown;
    plan?: unknown;
    search?: unknown;
  }, signal?: AbortSignal) =>
    request<Record<string, unknown>>('/plan', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
    }),
  replan: (payload: {
    plan: unknown;
    revision: number;
    plan_style: string;
    target_block_ids: string[];
    instruction: string;
    profile?: unknown;
    basic?: unknown;
    locked_block_ids?: string[];
  }, signal?: AbortSignal) =>
    request<{
      revision: number;
      plan: Record<string, unknown>;
      changed_block_ids: string[];
      affected_days: number[];
      changes: string[];
    }>('/plan/replan', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
    }),
  chat: (messages: ChatMessage[]) =>
    request<ChatResponse>('/agent/chat', {
      method: 'POST',
      body: JSON.stringify({ messages }),
    }),
  sendCode: (phone: string) =>
    request<{ phone: string; code: string; dev: boolean }>('/auth/send-code', {
      method: 'POST',
      body: JSON.stringify({ phone }),
    }),
  login: (phone: string, code: string) =>
    request<{ user_id: string; phone: string; nickname: string; is_new: boolean }>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify({ phone, code }) },
    ),
  saveTripMemory: (payload: {
    user_id: string;
    destination: string;
    start_date: string;
    end_date: string;
    chosen_plan_style?: string;
    final_plan?: unknown;
    conversation?: unknown[];
    rating?: number;
    feedback?: string;
  }) =>
    request<TripMemory>('/trip-memory', { method: 'POST', body: JSON.stringify(payload) }),
  listTripMemory: (userId: string) =>
    request<TripMemory[]>(`/trip-memory/${encodeURIComponent(userId)}`),
  getTripMemory: (id: number, userId?: string) =>
    request<TripMemory>(
      `/trip-memory/item/${id}${userId ? `?user_id=${encodeURIComponent(userId)}` : ''}`,
    ),
  search: (payload: {
    query?: string;
    destination?: string;
    start_date?: string;
    end_date?: string;
    origin?: string;
  }) =>
    request<Record<string, unknown>>('/search', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  question: (payload: {
    messages: ChatMessage[];
    has_plan?: boolean;
    trip_data?: Record<string, unknown>;
  }, signal?: AbortSignal) =>
    request<Record<string, unknown>>('/question', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
    }),
};
