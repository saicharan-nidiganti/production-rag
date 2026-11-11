import { useCallback, useEffect, useState } from "react";
import { api, apiKey, type Access, type User } from "./api/client";
import { AskPage } from "./components/AskPage";
import { DocumentsPage } from "./components/DocumentsPage";
import { SignIn } from "./components/SignIn";
import { Button, ErrorNotice, Spinner } from "./components/ui";

type Screen = "checking" | "unreachable" | Access["state"];
type Page = "ask" | "documents";

const PAGES: { id: Page; label: string }[] = [
  { id: "ask", label: "Ask" },
  { id: "documents", label: "Documents" },
];

export function App() {
  const [access, setAccess] = useState<Screen>("checking");
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [page, setPage] = useState<Page>("ask");

  const enter = useCallback((result: Access) => {
    setUser(result.state === "granted" ? result.user : null);
    setAccess(result.state);
  }, []);

  const check = useCallback(async () => {
    try {
      enter(await api.access());
    } catch (caught) {
      setError(caught);
      setAccess("unreachable");
    }
  }, [enter]);

  // The first check: state starts as "checking", and is set only once the server answers.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void check();
  }, [check]);

  const retry = () => {
    setAccess("checking");
    void check();
  };

  // Also where a 401 from any page lands: the session ended, or the key stopped working.
  const signOut = useCallback(() => {
    const hadAccount = user !== null;
    apiKey.clear();
    setUser(null);
    setAccess(hadAccount ? "password-needed" : "key-needed");
    if (hadAccount) void api.logout().catch(() => undefined); // already signed out is fine
  }, [user]);

  if (access === "checking") {
    return (
      <main className="flex min-h-screen items-center justify-center">
        <Spinner label="Connecting…" />
      </main>
    );
  }
  if (access === "unreachable") {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-4 px-4">
        <h1 className="text-2xl font-semibold">Production RAG</h1>
        <ErrorNotice error={error} action={<Button onClick={retry}>Try again</Button>} />
      </main>
    );
  }
  if (access === "key-needed") return <SignIn mode="key" onSignedIn={enter} />;
  if (access === "password-needed") return <SignIn mode="password" onSignedIn={enter} />;

  return (
    <div className="mx-auto flex min-h-screen max-w-6xl flex-col px-4">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-2 focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:text-slate-900"
      >
        Skip to content
      </a>
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 py-3 dark:border-slate-800">
        <h1 className="text-lg font-semibold">Production RAG</h1>
        <nav aria-label="Pages" className="flex gap-1">
          {PAGES.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              onClick={() => setPage(id)}
              aria-current={page === id ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${
                page === id
                  ? "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"
                  : "text-slate-700 hover:bg-slate-200 dark:text-slate-200 dark:hover:bg-slate-800"
              }`}
            >
              {label}
            </button>
          ))}
        </nav>
        {user || apiKey.get() ? (
          <div className="flex items-center gap-2">
            {user && (
              <span className="text-sm text-slate-600 dark:text-slate-300">
                <span className="sr-only">Signed in as </span>
                {user.email}
              </span>
            )}
            <Button variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        ) : (
          <span />
        )}
      </header>
      <main id="main" className="flex-1 py-6">
        {/* Both stay mounted, so switching pages keeps a conversation and an upload going. */}
        <div hidden={page !== "ask"}>
          <AskPage onUnauthorized={signOut} />
        </div>
        <div hidden={page !== "documents"}>
          <DocumentsPage onUnauthorized={signOut} />
        </div>
      </main>
    </div>
  );
}
