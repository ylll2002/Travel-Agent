export interface ItineraryItem {
  id: number;
  trip_id: number;
  day: number;
  title: string;
  description: string;
  location: string;
  start_time: string | null;
  end_time: string | null;
}

export interface Trip {
  id: number;
  user_id: string;
  title: string;
  destination: string;
  origin: string;
  start_date: string;
  end_date: string;
  travelers: string;
  status: string;
  budget: number | null;
  budget_tiers: string[];
  purposes: string[];
  notes: string;
  created_at: string;
  updated_at: string;
  items: ItineraryItem[];
}

export interface Destination {
  id: number;
  name: string;
  country: string;
  description: string;
  tags: string;
}

export interface TripCreatePayload {
  user_id?: string;
  title: string;
  destination: string;
  origin?: string;
  start_date: string;
  end_date: string;
  travelers?: string;
  status?: string;
  budget?: number | null;
  budget_tiers?: string[];
  purposes?: string[];
  notes?: string;
  items?: Array<{
    day: number;
    title: string;
    description?: string;
    location?: string;
  }>;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface ChatResponse {
  reply: string;
}

export interface UserProfile {
  age_group: string;
  gender: string;
  identity: string;
  city: string;
  travel_style: string[];
}

export interface TripInfo {
  destination: string;
  origin: string;
  start_date: string;
  end_date: string;
  travelers: string;
  companions: string[];
  budget_tiers: string[];
  total_budget: string;
  purposes: string[];
  special_needs: string[];
}


export interface SavedRouteBlock {
  id: string;
  plan_style?: string;
  day: number;
  date?: string;
  type: string;
  time: string;
  name: string;
  note?: string;
  match_score?: number;
  lng?: number;
  lat?: number;
}

export interface SavedRouteLeg {
  plan_style?: string;
  day: number;
  from: string;
  to: string;
  mode: 'walk' | 'transit' | 'drive' | 'bike' | 'metro' | 'bus';
  distance_m: number;
  duration_s: number;
  lines?: string[];
  polyline: [number, number][];
  options?: Array<{
    mode: 'metro' | 'bus' | 'drive' | 'bike';
    label: string;
    duration_s: number;
    distance_m: number;
    lines?: string[];
    price?: number;
  }>;
}

export interface SavedWeatherDay {
  date?: string;
  weather?: string;
  temp_min?: string | number;
  temp_max?: string | number;
  humidity?: string | number;
  [key: string]: unknown;
}

export interface SavedTripPlan {
  destination?: string;
  start_date?: string;
  end_date?: string;
  styles?: string[];
  summaries?: Record<string, string>;
  blocks?: SavedRouteBlock[];
  legs?: SavedRouteLeg[];
  weather?: { days?: SavedWeatherDay[] } | null;
}

export interface TripMemory {
  id: number;
  user_id: string;
  destination: string;
  start_date: string;
  end_date: string;
  chosen_plan_style: string | null;
  final_plan: SavedTripPlan;
  conversation: unknown[];
  user_edits: unknown[];
  feedback: string;
  rating: number | null;
  created_at: string;
}
