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
    throw new Error(`请求失败 (${response.status}): ${body}`);
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
    preferences?: unknown;
    recent_trips?: unknown;
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
  reviewPlan: (payload: { plan: unknown; search: unknown }, signal?: AbortSignal) =>
    request<Record<string, unknown>>('/plan/review', {
      method: 'POST', body: JSON.stringify(payload), signal,
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
  searchPoi: (payload: { destination: string; keyword: string }, signal?: AbortSignal) =>
    request<{ poi: unknown[] }>('/search/poi', {
      method: 'POST',
      body: JSON.stringify(payload),
      signal,
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
