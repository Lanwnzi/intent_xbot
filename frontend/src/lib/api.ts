export type ToolCall = {
  tool: string;
  input: string;
  output: string;
};

export type RetrievalResult = {
  text: string;
  score: number;
  source: string;
};

export type SessionSummary = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  message_count: number;
};

export type SessionHistory = {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  compressed_context?: string;
  messages: Array<{
    role: "user" | "assistant";
    content: string;
    tool_calls?: ToolCall[];
  }>;
};

export type StreamHandlers = {
  onEvent: (event: string, data: Record<string, unknown>) => void;
};

function getApiBase() {
  if (typeof window === "undefined") {
    return "http://127.0.0.1:8002/api";
  }

  return `http://${window.location.hostname}:8002/api`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${getApiBase()}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    }
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }

  return (await response.json()) as T;
}

export async function listSessions() {
  return request<SessionSummary[]>("/sessions");
}

export async function createSession(title = "新会话") {
  return request<SessionSummary>("/sessions", {
    method: "POST",
    body: JSON.stringify({ title })
  });
}

export async function renameSession(sessionId: string, title: string) {
  return request<SessionSummary>(`/sessions/${sessionId}`, {
    method: "PUT",
    body: JSON.stringify({ title })
  });
}

export async function deleteSession(sessionId: string) {
  return request<{ ok: boolean }>(`/sessions/${sessionId}`, {
    method: "DELETE"
  });
}

export async function getSessionHistory(sessionId: string) {
  return request<SessionHistory>(`/sessions/${sessionId}/history`);
}

export async function getSessionTokens(sessionId: string) {
  return request<{
    system_tokens: number;
    message_tokens: number;
    total_tokens: number;
  }>(`/tokens/session/${sessionId}`);
}

export async function uploadFile(file: File): Promise<{
  ok: boolean;
  filename: string;
  saved_path: string;
  content_type: string;
}> {
  const formData = new FormData();
  formData.append("file", file);

  const response = await fetch(`${getApiBase()}/upload`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Upload failed: ${response.status}`);
  }

  return (await response.json()) as {
    ok: boolean;
    filename: string;
    saved_path: string;
    content_type: string;
  };
}

export async function ingestDocument(sourcePath: string, docName?: string): Promise<{
  ok?: boolean;
  doc_id?: string;
  doc_name?: string;
  chunk_count?: number;
  error?: string;
}> {
  return request<{
    ok?: boolean;
    doc_id?: string;
    doc_name?: string;
    chunk_count?: number;
    error?: string;
  }>("/documents/ingest", {
    method: "POST",
    body: JSON.stringify({ source_path: sourcePath, doc_name: docName || "" }),
  });
}

export async function reviewContract(filePath: string, contractName?: string): Promise<{
  report_id: string;
  report_path: string;
  summary: string;
  risk_count: { high: number; medium: number; low: number };
  contract_name: string;
}> {
  return request<{
    report_id: string;
    report_path: string;
    summary: string;
    risk_count: { high: number; medium: number; low: number };
    contract_name: string;
  }>("/contracts/review", {
    method: "POST",
    body: JSON.stringify({ file_path: filePath, contract_name: contractName || "" }),
  });
}

export type IngestedDocument = {
  doc_id: string;
  doc_name: string;
  source_path: string;
  status: string;
  session_id?: string | null;
  project_id?: string | null;
  company_id?: string | null;
  chunk_count?: number;
  char_count?: number;
  created_at?: string;
  updated_at?: string;
  error_message?: string | null;
};

export async function listDocuments(params?: {
  sessionId?: string | null;
  projectId?: string | null;
}): Promise<{ documents: IngestedDocument[] }> {
  const qs = new URLSearchParams();
  if (params?.sessionId) qs.set("session_id", params.sessionId);
  if (params?.projectId) qs.set("project_id", params.projectId);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<{ documents: IngestedDocument[] }>(`/documents${suffix}`);
}

export async function listContracts(): Promise<{
  files: Array<{
    filename: string;
    path: string;
    size: number;
    uploaded_at: number;
  }>;
}> {
  // Keep compatibility with older UI callers, but switch source-of-truth to DB documents.
  const result = await listDocuments();
  return {
    files: result.documents.map((doc) => ({
      filename: doc.doc_name,
      path: doc.source_path,
      size: 0,
      uploaded_at: doc.created_at ? Math.floor(Date.parse(doc.created_at) / 1000) : 0,
    })),
  };
}

export async function deleteContract(filename: string) {
  return request<{ ok: boolean }>(
    `/contracts?filename=${encodeURIComponent(filename)}`,
    { method: "DELETE" }
  );
}

export type BatchItem = {
  filename?: string;
  source_path?: string;
  doc_name?: string;
  status: string;
  job_id?: string;
  doc_id?: string;
  cached?: boolean;
  error?: string;
  error_message?: string;
};

export type BatchIngestResult = {
  ok: boolean;
  batch_id: string;
  total: number;
  items: BatchItem[];
};

export async function batchIngestDocuments(
  files: File[],
  sessionId?: string | null,
  projectId?: string | null,
): Promise<BatchIngestResult> {
  const formData = new FormData();
  files.forEach((f) => formData.append("files", f));
  if (sessionId) formData.append("session_id", sessionId);
  if (projectId) formData.append("project_id", projectId);

  const response = await fetch(`${getApiBase()}/documents/batch-ingest`, {
    method: "POST",
    body: formData,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Batch ingest failed: ${response.status}`);
  }
  return (await response.json()) as BatchIngestResult;
}

export async function getBatchStatus(batchId: string): Promise<BatchIngestResult & { progress: number }> {
  return request<BatchIngestResult & { progress: number }>(`/documents/batch-status/${batchId}`);
}

export function subscribeBatchEvents(
  batchId: string,
  onProgress: (data: Record<string, unknown>) => void,
  onDone: () => void,
  onError: (error: string) => void,
): EventSource {
  const es = new EventSource(`${getApiBase()}/documents/batch-events/${batchId}`);

  es.addEventListener("batch_progress", (e: MessageEvent) => {
    onProgress(JSON.parse(e.data) as Record<string, unknown>);
  });
  es.addEventListener("batch_done", () => {
    es.close();
    onDone();
  });
  es.addEventListener("heartbeat", () => { /* no-op */ });
  es.onerror = () => {
    es.close();
    onError("SSE 连接断开");
  };
  return es;
}

export async function listSkills() {
  return request<Array<{ name: string; description: string; path: string }>>("/skills");
}

export async function loadFile(path: string) {
  return request<{ path: string; content: string }>(
    `/files?path=${encodeURIComponent(path)}`
  );
}

export async function saveFile(path: string, content: string) {
  return request<{ ok: boolean; path: string }>("/files", {
    method: "POST",
    body: JSON.stringify({ path, content })
  });
}

export async function getRagMode() {
  return request<{ enabled: boolean }>("/config/rag-mode");
}

export async function setRagMode(enabled: boolean) {
  return request<{ enabled: boolean }>("/config/rag-mode", {
    method: "PUT",
    body: JSON.stringify({ enabled })
  });
}

export async function compressSession(sessionId: string) {
  return request<{ archived_count: number; remaining_count: number }>(
    `/sessions/${sessionId}/compress`,
    { method: "POST" }
  );
}

export async function streamChat(
  payload: {
    message: string;
    session_id: string;
  },
  handlers: StreamHandlers
) {
  const response = await fetch(`${getApiBase()}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      ...payload,
      stream: true
    })
  });

  if (!response.ok || !response.body) {
    throw new Error(`Chat request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const flushBlock = (block: string) => {
    const lines = block.split("\n");
    let event = "message";
    const dataLines: string[] = [];

    for (const line of lines) {
      if (line.startsWith("event:")) {
        event = line.slice(6).trim();
      }
      if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    }

    if (!dataLines.length) {
      return;
    }

    const data = JSON.parse(dataLines.join("\n")) as Record<string, unknown>;
    handlers.onEvent(event, data);
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });

    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      flushBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }

    if (done) {
      if (buffer.trim()) {
        flushBlock(buffer);
      }
      break;
    }
  }
}
