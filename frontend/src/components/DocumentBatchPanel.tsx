"use client";

import { CheckCircle2, FileText, Loader2, Upload, X, XCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { batchIngestDocuments, getBatchStatus, subscribeBatchEvents } from "@/lib/api";

const STATUS_LABELS: Record<string, string> = {
  queued: "等待处理",
  parsing: "解析中",
  indexing: "入库中",
  indexed: "已完成",
  cached: "已存在",
  failed: "失败",
};

export function DocumentBatchPanel() {
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [progress, setProgress] = useState(0);
  const [items, setItems] = useState<Array<{
    filename?: string; doc_name?: string; source_path?: string; status: string;
    job_id?: string; doc_id?: string; cached?: boolean; error?: string; error_message?: string;
  }>>([]);
  const [complete, setComplete] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // 清理 SSE
  useEffect(() => {
    return () => { esRef.current?.close(); };
  }, []);

  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files || []);
    if (selected.length) setFiles((prev) => [...prev, ...selected]);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  const removeFile = useCallback((idx: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  const handleStart = useCallback(async () => {
    if (!files.length || uploading) return;
    setUploading(true);
    setItems([]);
    setProgress(0);
    setComplete(false);

    try {
      const result = await batchIngestDocuments(files);
      setBatchId(result.batch_id);
      setItems(result.items || []);

      // 启动 SSE
      const es = subscribeBatchEvents(
        result.batch_id,
        (data) => {
          setProgress((data.progress as number) || 0);
          if (data.items) setItems((data.items as typeof items) || []);
        },
        async () => {
          setProgress(100);
          setComplete(true);
          // 兜底：获取最终状态
          if (result.batch_id) {
            try {
              const final = await getBatchStatus(result.batch_id);
              setItems(final.items || []);
              setProgress(final.progress);
            } catch { /* ignore */ }
          }
        },
        async (err) => {
          console.warn("[batch] SSE error:", err);
          // 兜底：GET batch-status
          if (result.batch_id) {
            try {
              const fallback = await getBatchStatus(result.batch_id);
              setItems(fallback.items);
              setProgress(fallback.progress);
              setComplete(fallback.progress >= 100);
            } catch { /* ignore */ }
          }
        },
      );
      esRef.current = es;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "入库失败";
      setItems([{ status: "failed", doc_name: msg, error: msg }]);
    } finally {
      setUploading(false);
    }
  }, [files, uploading]);

  const safeItems = items || [];
  const doneCount = safeItems.filter(
    (i) => i.status === "indexed" || i.status === "cached" || i.status === "failed"
  ).length;
  const statusLabel = complete ? "入库完成" : uploading ? "上传并入库中..." : `${doneCount}/${safeItems.length || files.length}`;

  return (
    <div className="panel rounded-[28px] p-5 space-y-4">
      <h3 className="text-lg font-semibold">批量文档入库</h3>

      {/* 文件选择 */}
      <div className="space-y-2">
        <input
          accept=".md,.txt,.pdf,.doc,.docx"
          className="hidden"
          multiple
          onChange={handleFileSelect}
          ref={fileInputRef}
          type="file"
        />
        <div className="flex items-center gap-3">
          <button
            className="flex items-center gap-2 rounded-full border border-[var(--color-line)] bg-white/70 px-4 py-2 text-sm"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
            type="button"
          >
            <Upload size={16} /> 选择文件
          </button>
          <button
            className="flex items-center gap-2 rounded-full bg-ocean px-4 py-2 text-sm text-white disabled:opacity-40"
            disabled={!files.length || uploading}
            onClick={() => { void handleStart(); }}
            type="button"
          >
            {uploading ? (
              <><Loader2 size={16} className="animate-spin" /> 上传中...</>
            ) : (
              "开始入库"
            )}
          </button>
          <span className="text-sm text-[var(--color-ink-soft)]">{statusLabel}</span>
        </div>

        {/* 已选文件列表 */}
        {files.length > 0 && !batchId && (
          <div className="max-h-32 space-y-1 overflow-y-auto rounded-xl border border-[var(--color-line)] p-2">
            {files.map((f, i) => (
              <div key={i} className="flex items-center justify-between rounded-lg px-2 py-1 text-sm">
                <span className="flex items-center gap-2">
                  <FileText size={14} /> {f.name}
                  <span className="text-[var(--color-ink-soft)]">({(f.size / 1024).toFixed(1)} KB)</span>
                </span>
                <button className="hover:text-red-500" onClick={() => removeFile(i)}>
                  <X size={14} />
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* 进度条 */}
      {safeItems.length > 0 && (
        <div className="space-y-3">
          <div className="h-3 overflow-hidden rounded-full bg-ocean/10">
            <div
              className="h-full rounded-full bg-ocean transition-all duration-500"
              style={{ width: `${Math.max(progress, 2)}%` }}
            />
          </div>
          <p className="text-sm text-[var(--color-ink-soft)]">
            进度 {progress}%，已完成 {doneCount} / {safeItems.length}
            {safeItems.filter(i => i.status === "failed").length > 0 && (
              <span className="text-red-500">，失败 {safeItems.filter(i => i.status === "failed").length}</span>
            )}
          </p>

          {/* 文件状态列表 */}
          <div className="max-h-64 space-y-1 overflow-y-auto">
            {safeItems.map((item, idx) => (
              <div
                key={item.job_id || idx}
                className="flex items-center justify-between rounded-lg border border-[var(--color-line)] px-3 py-2 text-sm"
              >
                <span className="flex items-center gap-2 truncate">
                  {item.status === "indexed" || item.status === "cached" ? (
                    <CheckCircle2 size={14} className="text-green-500" />
                  ) : item.status === "failed" ? (
                    <XCircle size={14} className="text-red-500" />
                  ) : (
                    <Loader2 size={14} className="animate-spin text-ocean" />
                  )}
                  <span className="truncate">{item.filename || item.doc_name || item.source_path || "未知文件"}</span>
                </span>
                <span className="flex items-center gap-2 text-[var(--color-ink-soft)]">
                  {item.error_message || item.error ? (
                    <span className="text-red-500 text-xs">{item.error_message || item.error}</span>
                  ) : (
                    <span>
                      {STATUS_LABELS[item.status] || item.status}
                      {item.doc_id ? ` · ${item.doc_id}` : ""}
                      {item.cached ? " · 已复用" : ""}
                    </span>
                  )}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
