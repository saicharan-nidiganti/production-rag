import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { api, ApiError } from "../api/client";
import type { Document, DocumentStatus } from "../api/types";
import { Button, ErrorNotice, formatBytes, formatTime, Notice, Spinner } from "./ui";

const ACCEPT = ".txt,.md,.markdown,.pdf,.docx,.html,.htm";
const POLL_MS = 1500;

const STATUS: Record<DocumentStatus, { label: string; style: string }> = {
  queued: { label: "Queued", style: "bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-100" },
  processing: { label: "Processing", style: "bg-blue-100 text-blue-900 dark:bg-blue-900 dark:text-blue-100" },
  ready: { label: "Ready", style: "bg-green-100 text-green-900 dark:bg-green-900 dark:text-green-100" },
  failed: { label: "Failed", style: "bg-red-100 text-red-900 dark:bg-red-900 dark:text-red-100" },
};

const unfinished = (doc: Document) => doc.status === "queued" || doc.status === "processing";

interface UploadProblem {
  name: string;
  error: unknown;
}

export function DocumentsPage({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [total, setTotal] = useState(0);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [uploading, setUploading] = useState<string | null>(null);
  const [problems, setProblems] = useState<UploadProblem[]>([]);
  const [actionError, setActionError] = useState<unknown>(null);
  const [dragging, setDragging] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const fileInput = useRef<HTMLInputElement>(null);
  const replaceInput = useRef<HTMLInputElement>(null);
  const replacing = useRef<string | null>(null);

  const guard = useCallback(
    (error: unknown): unknown => {
      if (error instanceof ApiError && error.unauthorized) onUnauthorized();
      return error;
    },
    [onUnauthorized],
  );

  const load = useCallback(async () => {
    try {
      const page = await api.listDocuments();
      setDocuments(page.items);
      setTotal(page.total);
      setLoadError(null);
    } catch (error) {
      setLoadError(guard(error));
    }
  }, [guard]);

  // Fetch on mount. State is set only after the server answers, not during the effect.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  // While anything is still being processed, keep asking; stop as soon as nothing is.
  const waiting = documents?.some(unfinished) ?? false;
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [waiting, load]);

  async function upload(files: File[], replaces?: string) {
    const failed: UploadProblem[] = [];
    for (const file of files) {
      setUploading(file.name);
      try {
        const result = await api.upload(file, replaces);
        setAnnouncement(
          result.created
            ? `${file.name} uploaded and queued.`
            : `${file.name} was already uploaded.`,
        );
      } catch (error) {
        failed.push({ name: file.name, error: guard(error) });
      }
    }
    setUploading(null);
    setProblems(failed);
    await load();
  }

  async function act(action: () => Promise<unknown>, done: string) {
    setActionError(null);
    try {
      await action();
      setAnnouncement(done);
    } catch (error) {
      setActionError(guard(error));
    }
    await load();
  }

  function drop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    const files = [...event.dataTransfer.files];
    if (files.length) void upload(files);
  }

  return (
    <section aria-labelledby="documents-heading" className="flex flex-col gap-5">
      <div>
        <h2 id="documents-heading" className="text-xl font-semibold">
          Documents
        </h2>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          Answers come only from documents that are ready.
        </p>
      </div>

      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={drop}
        className={`flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-4 py-8 text-center ${
          dragging
            ? "border-blue-600 bg-blue-50 dark:bg-blue-950"
            : "border-slate-300 bg-white dark:border-slate-600 dark:bg-slate-900"
        }`}
      >
        <p className="font-medium">Drop files here, or</p>
        <Button variant="primary" onClick={() => fileInput.current?.click()} disabled={uploading !== null}>
          Choose files
        </Button>
        <p className="text-sm text-slate-600 dark:text-slate-300">
          PDF, Word (.docx), HTML, Markdown or plain text
        </p>
        <input
          ref={fileInput}
          type="file"
          multiple
          accept={ACCEPT}
          aria-label="Choose files to upload"
          className="sr-only"
          onChange={(event) => {
            const files = [...(event.target.files ?? [])];
            event.target.value = "";
            if (files.length) void upload(files);
          }}
        />
        <input
          ref={replaceInput}
          type="file"
          accept={ACCEPT}
          aria-label="Choose the new version"
          className="sr-only"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file && replacing.current) void upload([file], replacing.current);
          }}
        />
        {uploading && <Spinner label={`Uploading ${uploading}…`} />}
      </div>

      <p aria-live="polite" className="sr-only">
        {announcement}
      </p>

      {problems.map((problem) => (
        <ErrorNotice
          key={problem.name}
          error={problem.error instanceof Error ? withName(problem) : problem.error}
        />
      ))}
      {actionError !== null && <ErrorNotice error={actionError} />}
      {loadError !== null && (
        <ErrorNotice error={loadError} action={<Button onClick={() => void load()}>Try again</Button>} />
      )}

      {documents === null && loadError === null && <Spinner label="Loading documents…" />}

      {documents?.length === 0 && (
        <Notice title="No documents yet">
          Upload one above. It becomes searchable once its status is Ready.
        </Notice>
      )}

      {documents && documents.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-700 dark:bg-slate-900">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">
              {total} documents{waiting ? ", some still processing" : ""}
            </caption>
            <thead className="border-b border-slate-200 text-slate-600 dark:border-slate-700 dark:text-slate-300">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">Document</th>
                <th scope="col" className="px-4 py-2 font-medium">Status</th>
                <th scope="col" className="px-4 py-2 font-medium">Size</th>
                <th scope="col" className="px-4 py-2 font-medium">Updated</th>
                <th scope="col" className="px-4 py-2 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {documents.map((doc) => (
                <tr key={doc.id} className="border-b border-slate-100 last:border-0 dark:border-slate-800">
                  <th scope="row" className="max-w-xs px-4 py-3 font-normal">
                    <span className="block truncate font-medium" title={doc.title}>
                      {doc.title}
                    </span>
                    <span className="block truncate text-slate-600 dark:text-slate-300" title={doc.source_name}>
                      {doc.source_name}
                    </span>
                  </th>
                  <td className="px-4 py-3">
                    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS[doc.status].style}`}>
                      {STATUS[doc.status].label}
                    </span>
                    {doc.status === "ready" && (
                      <span className="ml-2 text-slate-600 dark:text-slate-300">
                        {doc.num_chunks} {doc.num_chunks === 1 ? "passage" : "passages"}
                      </span>
                    )}
                    {doc.error && (
                      <p className="mt-1 max-w-sm text-red-800 dark:text-red-200">{doc.error.message}</p>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-4 py-3">{formatBytes(doc.size_bytes)}</td>
                  <td className="whitespace-nowrap px-4 py-3">{formatTime(doc.updated_at)}</td>
                  <td className="px-4 py-3">
                    <div className="flex flex-wrap justify-end gap-2">
                      {confirmDelete === doc.id ? (
                        <>
                          <Button
                            variant="danger"
                            onClick={() => {
                              setConfirmDelete(null);
                              void act(() => api.remove(doc.id), `${doc.source_name} deleted.`);
                            }}
                          >
                            Delete {doc.source_name}
                          </Button>
                          <Button onClick={() => setConfirmDelete(null)}>Keep</Button>
                        </>
                      ) : (
                        <>
                          {doc.status === "failed" && (
                            <Button
                              aria-label={`Retry ${doc.source_name}`}
                              onClick={() => void act(() => api.retry(doc.id), `${doc.source_name} queued again.`)}
                            >
                              Retry
                            </Button>
                          )}
                          {doc.status === "ready" && (
                            <Button
                              aria-label={`Replace ${doc.source_name} with a new version`}
                              onClick={() => {
                                replacing.current = doc.id;
                                replaceInput.current?.click();
                              }}
                            >
                              Replace
                            </Button>
                          )}
                          <Button
                            variant="ghost"
                            aria-label={`Delete ${doc.source_name}`}
                            onClick={() => setConfirmDelete(doc.id)}
                          >
                            Delete
                          </Button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {total > documents.length && (
            <p className="border-t border-slate-200 px-4 py-2 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-300">
              Showing the newest {documents.length} of {total}.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function withName(problem: UploadProblem): unknown {
  const error = problem.error as Error;
  if (error instanceof ApiError) {
    return new ApiError(
      error.status,
      { code: error.code, message: `${problem.name}: ${error.message}`, request_id: error.requestId },
      error.retryAfter,
    );
  }
  return new Error(`${problem.name}: ${error.message}`);
}
