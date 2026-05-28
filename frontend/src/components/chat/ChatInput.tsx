"use client";

import { FileSearch, FolderUp, Paperclip, SendHorizonal, Upload, X } from "lucide-react";
import { useRef, useState } from "react";

import { ingestDocument, reviewContract, uploadFile } from "@/lib/api";

export function ChatInput({
  disabled,
  onSend,
  onReviewResult,
}: {
  disabled: boolean;
  onSend: (value: string) => Promise<void>;
  onReviewResult: (content: string) => void;
}) {
  const [value, setValue] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadAction, setUploadAction] = useState<"upload" | "review" | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  const [lastUpload, setLastUpload] = useState<{
    filename: string;
    action: "upload" | "review";
  } | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setPendingFile(file);
    setLastUpload(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  function handleCancelFile() {
    setPendingFile(null);
  }

  /** 上传 + 入库，返回 saved_path */
  async function doUpload(file: File): Promise<string> {
    const result = await uploadFile(file);
    // 异步入库（不等）
    void ingestDocument(result.saved_path, file.name).then((r) => {
      if (r.ok) console.log("[ingest] 入库成功:", r.doc_id, r.chunk_count, "chunks");
      else console.warn("[ingest] 入库失败:", r.error);
    }).catch((err) => { console.warn("[ingest] 入库异常:", err); });
    return result.saved_path;
  }

  /** 合同上传：只上传入库，不发消息 */
  async function handleUploadOnly() {
    if (!pendingFile || uploading) return;
    setUploadAction("upload");
    setUploading(true);

    const fileName = pendingFile.name;
    setLastUpload({ filename: `${fileName} 上传并入库中...`, action: "upload" });

    try {
      await doUpload(pendingFile);
      setLastUpload({ filename: `已上传并入库: ${fileName}`, action: "upload" });
      setPendingFile(null);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "上传失败";
      setLastUpload({ filename: `上传失败: ${msg}`, action: "upload" });
      setPendingFile(null);
    } finally {
      setUploading(false);
      setUploadAction(null);
    }
  }

  /** 合同审查：上传 → 快速通道 API（绕过 Agent），后台入库 */
  async function handleUploadAndReview() {
    if (!pendingFile || uploading) return;
    setUploadAction("review");
    setUploading(true);

    const fileName = pendingFile.name;
    setLastUpload({ filename: `${fileName} 上传中...`, action: "review" });

    let savedPath = "";
    try {
      // 步骤 1: 上传
      const result = await uploadFile(pendingFile);
      savedPath = result.saved_path;
      setPendingFile(null);
      setLastUpload({ filename: `${fileName} 正在解析并审核...`, action: "review" });

      // 步骤 2: 快速通道审核
      const reviewResult = await reviewContract(savedPath, fileName);
      const reviewMsg = [
        `## 📋 合同审查完成：${reviewResult.contract_name}`,
        ``,
        `**报告编号**: ${reviewResult.report_id}`,
        `**摘要**: ${reviewResult.summary}`,
        `**报告路径**: ${reviewResult.report_path}`,
      ].join("\n");
      onReviewResult(reviewMsg);
      setLastUpload({ filename: `审查完成: ${fileName}`, action: "review" });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "审查失败";
      setPendingFile(null);
      onReviewResult(`合同审查失败: ${msg}`);
      setLastUpload({ filename: `审查失败: ${msg}`, action: "review" });
    } finally {
      setUploading(false);
      setUploadAction(null);
    }
  }

  /** 普通发送（文本消息，不涉及文件） */
  async function handleSend() {
    const text = value.trim();
    if (!text || uploading) return;
    void onSend(text);
    setValue("");
  }

  const isBusy = disabled || uploading;

  return (
    <div className="panel rounded-[28px] p-3">
      {/* 文件预览条：文件名 + 两个操作按钮 */}
      {pendingFile && (
        <div className="mb-2 flex items-center gap-2 rounded-full bg-amber-50 border border-amber-200 px-3 py-1.5 text-sm">
          <Upload size={14} className="text-amber-600" />
          <span className="text-amber-700 font-medium">{pendingFile.name}</span>
          <span className="text-amber-500">({(pendingFile.size / 1024).toFixed(1)} KB)</span>

          {/* 合同上传 */}
          <button
            className="ml-auto flex items-center gap-1 rounded-full bg-blue-500 px-3 py-1 text-xs text-white hover:bg-blue-600 disabled:opacity-40"
            disabled={isBusy}
            onClick={() => { void handleUploadOnly(); }}
            type="button"
          >
            <FolderUp size={12} />
            {uploadAction === "upload" ? "上传中..." : "合同上传"}
          </button>

          {/* 合同审查 */}
          <button
            className="flex items-center gap-1 rounded-full bg-green-500 px-3 py-1 text-xs text-white hover:bg-green-600 disabled:opacity-40"
            disabled={isBusy}
            onClick={() => { void handleUploadAndReview(); }}
            type="button"
          >
            <FileSearch size={12} />
            {uploadAction === "review" ? "上传中..." : "合同审查"}
          </button>

          <button
            className="text-amber-500 hover:text-red-500"
            onClick={handleCancelFile}
            type="button"
          >
            <X size={14} />
          </button>
        </div>
      )}

      {/* 上传/审查完成提示 */}
      {lastUpload && (
        <div className={`mb-2 flex items-center gap-2 rounded-full px-3 py-1.5 text-sm ${
          lastUpload.filename.startsWith("上传失败") ? "bg-red-50 border border-red-200" : "bg-ocean/10"
        }`}>
          <Paperclip size={14} />
          <span className={lastUpload.filename.startsWith("上传失败") ? "text-red-600" : "text-ocean"}>
            {lastUpload.action === "upload" ? "已上传并入库" : "已上传并发送审查请求"}
          </span>
          <button
            className="ml-auto text-[var(--color-ink-soft)] hover:text-red-500"
            onClick={() => setLastUpload(null)}
            type="button"
          >
            <X size={14} />
          </button>
        </div>
      )}

      <textarea
        className="min-h-28 w-full resize-none rounded-[22px] border border-[var(--color-line)] bg-white/70 px-4 py-3 outline-none"
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            event.preventDefault();
            void handleSend();
          }
        }}
        placeholder={pendingFile ? "选择'合同上传'入库，或'合同审查'直接生成报告" : "输入你的问题，Cmd/Ctrl + Enter 发送"}
        value={value}
      />

      <div className="mt-3 flex items-center justify-between">
        <p className="text-sm text-[var(--color-ink-soft)]">
          支持工具调用、Memory 检索和多段响应。
        </p>

        <div className="flex items-center gap-2">
          <input
            accept=".pdf,.docx,.doc,.md,.txt"
            className="hidden"
            onChange={handleFileSelect}
            ref={fileInputRef}
            type="file"
          />

          {/* 选择文件 */}
          <button
            className="flex items-center gap-2 rounded-full border border-[var(--color-line)] bg-white/70 px-4 py-2 text-sm text-[var(--color-ink-soft)] disabled:cursor-not-allowed disabled:opacity-40"
            disabled={isBusy}
            onClick={() => fileInputRef.current?.click()}
            type="button"
          >
            <Paperclip size={16} />
            选择文件
          </button>

          {/* 发送 */}
          <button
            className="flex items-center gap-2 rounded-full bg-ocean px-4 py-2 text-sm text-white disabled:cursor-not-allowed disabled:bg-[rgba(15,139,141,0.45)]"
            disabled={isBusy || !value.trim()}
            onClick={() => { void handleSend(); }}
            type="button"
          >
            <SendHorizonal size={16} />
            发送
          </button>
        </div>
      </div>
    </div>
  );
}
