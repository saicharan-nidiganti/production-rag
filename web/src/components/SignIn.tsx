import { useId, useState, type FormEvent } from "react";
import { api, ApiError, apiKey, type Access } from "../api/client";
import { Button, ErrorNotice, Notice } from "./ui";

/**
 * Shown when the server wants to know who is asking: an email address and password when it
 * has accounts, or the shared API key when it doesn't.
 */
export function SignIn({
  mode,
  onSignedIn,
}: {
  mode: "password" | "key";
  onSignedIn: (access: Access) => void;
}) {
  const id = useId();
  const [email, setEmail] = useState("");
  const [secret, setSecret] = useState("");
  const [checking, setChecking] = useState(false);
  const [rejected, setRejected] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const accounts = mode === "password";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setChecking(true);
    setRejected(false);
    setError(null);
    try {
      if (accounts) {
        onSignedIn(await api.login(email.trim(), secret));
        return;
      }
      apiKey.set(secret.trim());
      const access = await api.access();
      if (access.state === "granted") {
        onSignedIn(access);
        return;
      }
      apiKey.clear();
      setRejected(true);
    } catch (caught) {
      apiKey.clear();
      // A wrong password is an answer, not a failure; a lockout (429) is shown as it is.
      if (caught instanceof ApiError && caught.unauthorized) setRejected(true);
      else setError(caught);
    } finally {
      setChecking(false);
    }
  }

  const field =
    "rounded-md border border-slate-300 bg-white px-3 py-2 dark:border-slate-600 dark:bg-slate-900";
  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-4">
      <div>
        <h1 className="text-2xl font-semibold">Production RAG</h1>
        <p className="mt-1 text-slate-600 dark:text-slate-300">
          {accounts
            ? "Sign in to see your documents."
            : "This server needs an API key. Ask whoever runs it for one."}
        </p>
      </div>
      <form onSubmit={submit} className="flex flex-col gap-3">
        {accounts && (
          <>
            <label htmlFor={`${id}-email`} className="text-sm font-medium">
              Email address
            </label>
            <input
              id={`${id}-email`}
              type="email"
              autoComplete="username"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              aria-invalid={rejected}
              className={field}
            />
          </>
        )}
        <label htmlFor={id} className="text-sm font-medium">
          {accounts ? "Password" : "API key"}
        </label>
        <input
          id={id}
          type="password"
          autoComplete={accounts ? "current-password" : "off"}
          required
          value={secret}
          onChange={(event) => setSecret(event.target.value)}
          aria-invalid={rejected}
          aria-describedby={accounts ? undefined : `${id}-help`}
          className={field}
        />
        {!accounts && (
          <p id={`${id}-help`} className="text-sm text-slate-600 dark:text-slate-300">
            It stays in this browser tab and is forgotten when the tab closes.
          </p>
        )}
        {rejected && (
          <Notice
            tone="error"
            title={accounts ? "Wrong email address or password" : "That key wasn't accepted"}
          >
            {accounts
              ? "Check both and try again. An administrator can set a new password for you."
              : "Check it for missing characters and try again."}
          </Notice>
        )}
        {error !== null && <ErrorNotice error={error} />}
        <Button
          type="submit"
          variant="primary"
          disabled={checking || secret.trim() === "" || (accounts && email.trim() === "")}
        >
          {checking ? "Checking…" : accounts ? "Sign in" : "Continue"}
        </Button>
      </form>
    </main>
  );
}
