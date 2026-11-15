import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/store/auth";
import { apiError } from "@/lib/api";
import AuthLayout from "@/components/AuthLayout";

export default function Login() {
  const nav = useNavigate();
  const login = useAuth((s) => s.login);
  const loading = useAuth((s) => s.loading);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await login(email, password);
      nav("/");
    } catch (e: any) {
      setErr(apiError(e, "Sign-in failed. Check your connection and try again."));
    }
  }

  return (
    <AuthLayout
      title="Sign in"
      subtitle="Pick up where you left off."
      footer={
        <>
          New to Lumen?{" "}
          <Link to="/register" className="font-medium text-accent-strong underline underline-offset-2 hover:text-accent-deep">
            Create an account
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4" noValidate={false}>
        <div>
          <label htmlFor="email" className="label">Email</label>
          <input
            id="email" className="field" type="email" autoComplete="email"
            value={email} onChange={(e) => setEmail(e.target.value)}
            placeholder="you@company.com" required
          />
        </div>
        <div>
          <label htmlFor="password" className="label">Password</label>
          <input
            id="password" className="field" type="password" autoComplete="current-password"
            value={password} onChange={(e) => setPassword(e.target.value)} required
          />
        </div>
        {err && <div role="alert" className="field-error">{err}</div>}
        <button className="btn-primary w-full" disabled={loading}>
          {loading && <Loader2 className="animate-spin" size={16} aria-hidden="true" />}
          {loading ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </AuthLayout>
  );
}
