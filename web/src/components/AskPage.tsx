import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { api, ApiError } from "../api/client";
import type { Citation, QueryResponse, SearchHit } from "../api/types";
import { Button, ErrorNotice, Notice, Spinner } from "./ui";

type Phase = "retrieving" | "writing" | "done" | "stopped" | "failed";

interface Exchange {
  id: number;
  question: string;
  phase: Phase;
  text: string;
  sources: Citation[];
  retrieved: SearchHit[];
  response: QueryResponse | null;
  error: unknown;
}

interface Selection {
  exchange: number;
  source: number;
}

const MAX_QUESTION = 2000;

export function AskPage({ onUnauthorized }: { onUnauthorized: () => void }) {
  const inputId = useId();
  const [question, setQuestion] = useState("");
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [selected, setSelected] = useState<Selection | null>(null);
  const controller = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const panel = useRef<HTMLElement>(null);
  const busy = exchanges.some((e) => e.phase === "retrieving" || e.phase === "writing");

  useEffect(() => () => controller.current?.abort(), []);

  // Move focus to the passage when a source is chosen, so keyboard and screen-reader
  // users land on what they asked for.
  useEffect(() => {
    if (selected) panel.current?.focus();
  }, [selected]);

  function update(id: number, change: (exchange: Exchange) => Exchange) {
    setExchanges((all) => all.map((e) => (e.id === id ? change(e) : e)));
  }

  async function ask(text: string) {
    const id = nextId.current++;
    const abort = new AbortController();
    controller.current = abort;
    setExchanges((all) => [
      ...all,
      { id, question: text, phase: "retrieving", text: "", sources: [], retrieved: [], response: null, error: null },
    ]);
    setQuestion("");
    try {
      for await (const event of api.ask(text, abort.signal)) {
        if (event.type === "retrieval") {
          update(id, (e) => ({ ...e, phase: "writing", retrieved: event.retrieved }));
        } else if (event.type === "text") {
          update(id, (e) => ({ ...e, text: e.text + event.text }));
        } else if (event.type === "citation") {
          update(id, (e) => {
            const sources = [...e.sources];
            sources[event.index] = event.citation;
            return { ...e, sources };
          });
        } else if (event.type === "done") {
          update(id, (e) => ({
            ...e,
            phase: "done",
            text: event.response.answer,
            sources: event.response.citations,
            retrieved: event.response.retrieved,
            response: event.response,
          }));
        } else {
          const { error } = event;
          update(id, (e) => ({ ...e, phase: "failed", error: new ApiError(502, error) }));
        }
      }
      // A stream that ends without `done` was cut off.
      update(id, (e) => (e.phase === "writing" || e.phase === "retrieving" ? { ...e, phase: "stopped" } : e));
    } catch (error) {
      if (abort.signal.aborted) {
        update(id, (e) => ({ ...e, phase: "stopped" }));
      } else {
        if (error instanceof ApiError && error.unauthorized) onUnauthorized();
        update(id, (e) => ({ ...e, phase: "failed", error }));
      }
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (text && !busy) void ask(text);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  const chosen = selected ? exchanges.find((e) => e.id === selected.exchange) : undefined;
  const source = selected ? chosen?.sources[selected.source] : undefined;

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
      <section aria-labelledby="ask-heading" className="flex min-w-0 flex-col gap-5">
        <div>
          <h2 id="ask-heading" className="text-xl font-semibold">
            Ask
          </h2>
          <p className="text-sm text-slate-600 dark:text-slate-300">
            Answers are written from your documents, with a source for each claim.
          </p>
        </div>

        {exchanges.length === 0 && (
          <Notice title="Ask a question about your documents">
            If the documents don't hold the answer, you'll be told so instead of getting a guess.
          </Notice>
        )}

        <ol className="flex flex-col gap-6">
          {exchanges.map((exchange) => (
            <li key={exchange.id}>
              <ExchangeView
                exchange={exchange}
                selected={selected?.exchange === exchange.id ? selected.source : null}
                onSelect={(index) => setSelected({ exchange: exchange.id, source: index })}
              />
            </li>
          ))}
        </ol>

        <form onSubmit={submit} className="sticky bottom-0 flex flex-col gap-2 bg-slate-50 pb-4 pt-2 dark:bg-slate-950">
          <label htmlFor={inputId} className="text-sm font-medium">
            Your question
          </label>
          <textarea
            id={inputId}
            rows={2}
            maxLength={MAX_QUESTION}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="For example: how long does the canary run?"
            className="resize-y rounded-md border border-slate-300 bg-white px-3 py-2 dark:border-slate-600 dark:bg-slate-900"
          />
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-slate-600 dark:text-slate-300">
              Enter sends; Shift+Enter adds a line.
            </p>
            {busy ? (
              <Button onClick={() => controller.current?.abort()}>Stop</Button>
            ) : (
              <Button type="submit" variant="primary" disabled={question.trim() === ""}>
                Send
              </Button>
            )}
          </div>
        </form>
      </section>

      <aside
        ref={panel}
        tabIndex={-1}
        aria-labelledby="passage-heading"
        className="h-fit rounded-lg border border-slate-200 bg-white p-4 lg:sticky lg:top-4 dark:border-slate-700 dark:bg-slate-900"
      >
        <h2 id="passage-heading" className="font-semibold">
          Source passage
        </h2>
        {source && chosen && selected ? (
          <Passage
            number={selected.source + 1}
            source={source}
            context={chosen.retrieved.find((hit) => hit.chunk_id === source.chunk_id)}
          />
        ) : (
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">
            Choose a numbered source in an answer to read the passage it came from.
          </p>
        )}
      </aside>
    </div>
  );
}

function ExchangeView({
  exchange,
  selected,
  onSelect,
}: {
  exchange: Exchange;
  selected: number | null;
  onSelect: (index: number) => void;
}) {
  const { phase, response } = exchange;
  const status = response?.status;
  return (
    <article className="flex flex-col gap-3">
      <h3 className="self-end rounded-lg bg-blue-700 px-4 py-2 text-white">
        <span className="sr-only">You asked: </span>
        {exchange.question}
      </h3>
      <div className="rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-700 dark:bg-slate-900">
        <div aria-live="polite" aria-busy={phase === "retrieving" || phase === "writing"}>
          {phase === "retrieving" && <Spinner label="Searching your documents…" />}
          {phase === "writing" && exchange.text === "" && <Spinner label="Writing the answer…" />}
          {status === "insufficient_evidence" && (
            <p className="mb-2 text-sm font-medium text-amber-800 dark:text-amber-200">
              Not enough evidence in your documents
            </p>
          )}
          {status === "refused" && (
            <p className="mb-2 text-sm font-medium text-amber-800 dark:text-amber-200">
              The model declined to answer
            </p>
          )}
          {response ? (
            <p className="whitespace-pre-wrap leading-relaxed">
              {response.segments.map((segment, i) => (
                <span key={i}>
                  {segment.text}
                  {segment.citations.map((index) => (
                    <SourceMark
                      key={index}
                      index={index}
                      source={exchange.sources[index]}
                      selected={selected === index}
                      onSelect={onSelect}
                    />
                  ))}
                </span>
              ))}
            </p>
          ) : (
            exchange.text && <p className="whitespace-pre-wrap leading-relaxed">{exchange.text}</p>
          )}
        </div>
        {phase === "stopped" && (
          <p className="mt-2 text-sm text-slate-600 dark:text-slate-300">Stopped before the answer finished.</p>
        )}
        {phase === "failed" && <div className="mt-2"><ErrorNotice error={exchange.error} /></div>}

        {exchange.sources.length > 0 && (
          <div className="mt-4 border-t border-slate-200 pt-3 dark:border-slate-700">
            <h4 className="text-sm font-semibold">Sources</h4>
            <ol className="mt-1 flex flex-col gap-1">
              {exchange.sources.map(
                (source, index) =>
                  source && (
                    <li key={index}>
                      <button
                        type="button"
                        onClick={() => onSelect(index)}
                        aria-pressed={selected === index}
                        className={`w-full rounded-md px-2 py-1 text-left text-sm hover:bg-slate-100 dark:hover:bg-slate-800 ${
                          selected === index ? "bg-blue-50 dark:bg-blue-950" : ""
                        }`}
                      >
                        <span className="font-medium">[{index + 1}]</span> {source.document_title}
                        <span className="text-slate-600 dark:text-slate-300">{where(source)}</span>
                      </button>
                    </li>
                  ),
              )}
            </ol>
          </div>
        )}

        {phase === "done" && exchange.retrieved.length > 0 && (
          <details className="mt-3 text-sm">
            <summary className="cursor-pointer text-slate-600 dark:text-slate-300">
              {exchange.retrieved.length} passages were considered
            </summary>
            <ol className="mt-2 flex flex-col gap-2">
              {exchange.retrieved.map((hit) => (
                <li key={hit.chunk_id} className="rounded-md bg-slate-50 p-2 dark:bg-slate-800">
                  <p className="font-medium">
                    {hit.document_title}
                    <span className="font-normal text-slate-600 dark:text-slate-300">{where(hit)}</span>
                  </p>
                  <p className="line-clamp-3 text-slate-700 dark:text-slate-200">{hit.text}</p>
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </article>
  );
}

function SourceMark({
  index,
  source,
  selected,
  onSelect,
}: {
  index: number;
  source: Citation | undefined;
  selected: boolean;
  onSelect: (index: number) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onSelect(index)}
      aria-pressed={selected}
      aria-label={`Source ${index + 1}${source ? `: ${source.document_title}` : ""}`}
      className={`mx-0.5 rounded px-1 align-baseline text-xs font-semibold ${
        selected
          ? "bg-blue-700 text-white"
          : "bg-blue-100 text-blue-900 hover:bg-blue-200 dark:bg-blue-900 dark:text-blue-100"
      }`}
    >
      {index + 1}
    </button>
  );
}

/** " · section · page 3", without the document's own title at the start of the section path. */
function where(passage: { document_title: string; heading: string | null; page: number | null }): string {
  const prefix = `${passage.document_title} > `;
  const heading = passage.heading?.startsWith(prefix)
    ? passage.heading.slice(prefix.length)
    : passage.heading === passage.document_title
      ? null
      : passage.heading;
  const parts = [heading, passage.page !== null ? `page ${passage.page}` : null].filter(Boolean);
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

/** Source files are often hard-wrapped; join those lines, and keep paragraph breaks. */
const unwrap = (text: string): string => text.replace(/(?<!\n)\n(?!\n)/g, " ");

function Passage({ number, source, context }: { number: number; source: Citation; context?: SearchHit }) {
  const cited = unwrap(source.cited_text);
  const around = context ? unwrap(context.text) : "";
  const at = around.indexOf(cited);
  return (
    <div className="mt-2 flex flex-col gap-3 text-sm">
      <p className="font-medium">
        [{number}] {source.document_title}
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-slate-700 dark:text-slate-200">
        <dt className="text-slate-600 dark:text-slate-300">File</dt>
        <dd className="break-all">{source.source_name}</dd>
        {source.heading && (
          <>
            <dt className="text-slate-600 dark:text-slate-300">Section</dt>
            <dd>{source.heading}</dd>
          </>
        )}
        {source.page !== null && (
          <>
            <dt className="text-slate-600 dark:text-slate-300">Page</dt>
            <dd>{source.page}</dd>
          </>
        )}
        <dt className="text-slate-600 dark:text-slate-300">Position</dt>
        <dd>
          characters {source.start_char.toLocaleString()} to {source.end_char.toLocaleString()}
        </dd>
      </dl>
      {context && at !== -1 ? (
        <p className="whitespace-pre-wrap rounded-md bg-slate-50 p-3 leading-relaxed dark:bg-slate-800">
          {around.slice(0, at)}
          <mark className="rounded bg-yellow-200 px-0.5 text-slate-900">{cited}</mark>
          {around.slice(at + cited.length)}
        </p>
      ) : (
        <blockquote className="whitespace-pre-wrap rounded-md border-l-4 border-blue-600 bg-slate-50 p-3 leading-relaxed dark:bg-slate-800">
          {cited}
        </blockquote>
      )}
    </div>
  );
}
