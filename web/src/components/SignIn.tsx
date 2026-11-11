import { useId, useState, type FormEvent } from "react";
import { api, apiKey } from "../api/client";
import { Button, ErrorNotice, Notice } from "./ui";

/** Shown when the server wants an API key. The key is kept for this tab only. */
export function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const id = useId();
  const [key, setKey] = useState("");
  const [checking, setChecking] = useState(false);
  const [rejected, setRejected] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setChecking(true);
    setRejected(false);
    setError(null);
    apiKey.set(key.trim());
    try {
      if (await api.checkAccess()) {
        onSignedIn();
        return;
      }
      apiKey.clear();
      setRejected(true);
    } catch (caught) {
      apiKey.clear();
      setError(caught);
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-4">
      <div>
        <h1 className="text-2xl font-semibold">Production RAG</h1>
        <p className="mt-1 text-slate-600 dark:text-slate-300">
          This server needs an API key. Ask whoever runs it for one.
        </p>
      </div>
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label htmlFor={id} className="text-sm font-medium">
          API key
        </label>
        <input
          id={id}
          type="password"
          autoComplete="off"
          required
          value={key}
          onChange={(event) => setKey(event.target.value)}
          aria-invalid={rejected}
          aria-describedby={`${id}-help`}
          className="rounded-md border border-slate-300 bg-white px-3 py-2 dark:border-slate-600 dark:bg-slate-900"
        />
        <p id={`${id}-help`} className="text-sm text-slate-600 dark:text-slate-300">
          It stays in this browser tab and is forgotten when the tab closes.
        </p>
        {rejected && (
          <Notice tone="error" title="That key wasn't accepted">
            Check it for missing characters and try again.
          </Notice>
        )}
        {error !== null && <ErrorNotice error={error} />}
        <Button type="submit" variant="primary" disabled={checking || key.trim() === ""}>
          {checking ? "Checking…" : "Continue"}
        </Button>
      </form>
    </main>
  );
}
