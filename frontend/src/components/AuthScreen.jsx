import { useState } from "react";
import { api, saveToken } from "../api";

export default function AuthScreen({ onAuthed }) {
  const [mode, setMode] = useState("login"); // 'login' | 'signup'
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const isLogin = mode === "login";

  async function submit(e) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const result = isLogin
        ? await api.login(email, password)
        : await api.signup(email, password);
      saveToken(result.access_token);
      onAuthed();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <div className="auth__pitch">
        <div className="brandmark">
          <span className="brandmark__glyph" aria-hidden="true" />
          <span className="brandmark__word">Watermark</span>
        </div>

        <h1 className="auth__headline">
          Not another price tracker. <em>An attention filter.</em>
        </h1>

        <p className="auth__sub">
          Markets drift all day. Watermark tells you what actually changed since{" "}
          <strong>you</strong> last looked — and stays quiet about everything else.
        </p>

        <ul className="auth__points">
          <li>
            <span>
              <strong>Scored, not sorted.</strong> A 2% move is noise for one stock and
              a headline for another. Every move is measured against that stock's own
              volatility, its volume, and the index.
            </span>
          </li>
          <li>
            <span>
              <strong>Since your last visit.</strong> Not a fixed 24-hour window — a
              read watermark, like an unread inbox.
            </span>
          </li>
          <li>
            <span>
              <strong>Catches what reverted.</strong> If it spiked and came back while
              you were away, the price looks unchanged. We still tell you.
            </span>
          </li>
        </ul>
      </div>

      <div className="auth__panel">
        <div className="auth-card">
          <h2 className="auth-card__title">{isLogin ? "Welcome back" : "Create account"}</h2>
          <p className="auth-card__hint">
            {isLogin
              ? "Pick up where your watermark left off."
              : "Start tracking what actually matters."}
          </p>

          <form onSubmit={submit}>
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
            </div>

            <div className="field">
              <label htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                placeholder="••••••••"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
              />
            </div>

            {error && <p className="error-text">{error}</p>}

            <button className="btn-primary" type="submit" disabled={busy}>
              {busy ? "One moment…" : isLogin ? "Log in" : "Create account"}
            </button>
          </form>

          <p className="auth-card__switch">
            {isLogin ? "No account yet? " : "Already have one? "}
            <button
              type="button"
              className="link-button"
              onClick={() => {
                setMode(isLogin ? "signup" : "login");
                setError(null);
              }}
            >
              {isLogin ? "Sign up" : "Log in"}
            </button>
          </p>
        </div>
      </div>
    </div>
  );
}
