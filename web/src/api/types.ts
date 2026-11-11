// The API's shapes, as src/production_rag/api/schemas.py defines them.

export type DocumentStatus = "queued" | "processing" | "ready" | "failed";
export type AnswerStatus = "answered" | "insufficient_evidence" | "refused";

export interface DocumentError {
  code: string;
  message: string;
}

export interface Document {
  id: string;
  source_name: string;
  title: string;
  content_type: string;
  size_bytes: number;
  num_chunks: number;
  content_hash: string;
  metadata: Record<string, unknown>;
  status: DocumentStatus;
  error: DocumentError | null;
  attempts: number;
  created_at: string;
  updated_at: string;
  replaces: string | null;
}

export interface DocumentList {
  items: Document[];
  total: number;
  limit: number;
  offset: number;
}

export interface IngestResponse {
  document: Document;
  created: boolean;
}

/** Where a passage sits in its document; shared by search hits and citations. */
export interface Passage {
  chunk_id: number;
  chunk_key: string;
  document_id: string;
  document_title: string;
  source_name: string;
  heading: string | null;
  page: number | null;
  start_char: number;
  end_char: number;
}

export interface SearchHit extends Passage {
  text: string;
  score: number;
  vector_score: number | null;
  lexical_score: number | null;
  rerank_score: number | null;
}

export interface Citation extends Passage {
  cited_text: string;
}

export interface Segment {
  text: string;
  /** Indexes into the response's `citations`. */
  citations: number[];
}

export interface QueryResponse {
  status: AnswerStatus;
  answer: string;
  segments: Segment[];
  citations: Citation[];
  retrieved: SearchHit[];
  model: string;
  stop_reason: string | null;
  usage: Record<string, number> | null;
  timings_ms: Record<string, number>;
}

export interface ErrorBody {
  code: string;
  message: string;
  request_id?: string | null;
}

/** The events of POST /v1/query/stream, in the order they arrive. */
export type StreamEvent =
  | { type: "retrieval"; retrieved: SearchHit[]; abstained_before_generation: boolean }
  | { type: "text"; text: string }
  | { type: "citation"; index: number; citation: Citation }
  | { type: "done"; response: QueryResponse }
  | { type: "error"; error: ErrorBody };
