// services/full-stack-agent-app/src/lib/types.ts
// Mirrors QueryResponse and the chat_turns row shape exactly as returned
// by services/agent-service/main.py -- keep these in sync if that
// Pydantic model ever changes.

export interface Distribution {
  wahlperiode?: number;
  date_range?: string;
  counts?: Record<string, number>;
  percentages?: Record<string, number>;
  data_notes?: string;
  chart_type?: "bar" | "scatter";
}

export interface PartyDistributionChart {
  type: "party_distribution";
  distributions: Distribution[];
  chart_type: "bar" | "scatter";
}

// One entry in the presentation-layer transcript (chat_turns table),
// returned by GET /threads/{id}/messages -- and also the shape used
// locally in React state for messages appended during a live turn.
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  chart_data?: PartyDistributionChart | null;
  faithfulness_score?: number | null;
  completeness_score?: number | null;
  thinking_log?: string[] | null;
  generation_time_seconds?: number | null;
}

// The exact body POST /query returns from agent-service.
export interface QueryResponse {
  answer: string;
  faithfulness_score: number | null;
  completeness_score: number | null;
  party_distribution: PartyDistributionChart | null;
  thinking_log: string[];
}