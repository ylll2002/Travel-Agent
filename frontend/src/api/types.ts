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
  title: string;
  destination: string;
  start_date: string;
  end_date: string;
  status: string;
  budget: number | null;
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
  title: string;
  destination: string;
  start_date: string;
  end_date: string;
  status?: string;
  budget?: number | null;
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
  session_id?: string | null;
  workflow_status?: string | null;
  current_stage?: string | null;
  pending_question?: string | null;
  trip_brief: Record<string, unknown>;
  candidate_destinations: Record<string, unknown>[];
  time_candidates: TimeCandidate[];
  draft_cards: ItineraryCard[];
  card_dependencies: Record<string, string[]>;
  evidence_refs: EvidenceRef[];
  validation_results: Record<string, unknown>;
  affected_cards: number[];
  modification_scope?: string | null;
  revision_comparison: RevisionComparison[];
  specialist_results?: SpecialistResult[];
  agent_tasks?: AgentTask[];
  workflow_events?: WorkflowEvent[];
}

export interface WorkflowTraceDataFlow {
  reads: string[];
  writes: string[];
  key_payload: Record<string, unknown>;
}

export interface WorkflowTraceItem {
  node: string;
  data_flow: WorkflowTraceDataFlow;
  input_state: Record<string, unknown>;
  output_update: Record<string, unknown>;
  state_passed_forward: Record<string, unknown>;
  next_node: string;
}

export interface WorkflowTraceResponse {
  initial_state: Record<string, unknown>;
  trace: WorkflowTraceItem[];
  executed_nodes: string[];
}

export interface SpecialistResult {
  agent?: string;
  status?: string;
  error?: string;
  report?: {
    status?: string;
    summary?: string;
    uncertainties?: string[];
  };
}

export interface AgentTask {
  agent?: string;
  status?: string;
  error?: string | null;
}

export interface WorkflowEvent {
  event?: string;
  node?: string;
  status?: string;
  step?: number;
  elapsed_ms?: number;
  steps?: number;
  writes?: string[];
  error?: string;
}

export interface EvidenceRef {
  source?: string;
  source_type?: string;
  url?: string;
  retrieved_at?: string;
  status?: string;
}

export interface TimeCandidate {
  label: string;
  start_date?: string;
  end_date?: string;
  duration_days?: number;
  reason?: string;
  activities?: string[];
}

export interface ItineraryCard {
  card_id: string;
  day: number;
  type: string;
  title: string;
  time?: string;
  duration?: string;
  location?: string;
  description?: string;
  reason?: string;
  cost?: number;
  evidence_refs: EvidenceRef[];
  dependency_ids: string[];
  status: string;
}

export interface RevisionComparison {
  card_id: string;
  before: ItineraryCard;
  after?: ItineraryCard;
}

