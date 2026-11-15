import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { useAuth } from "@/store/auth";
import { apiError } from "@/lib/api";
import AuthLayout from "@/components/AuthLayout";

export default function Register() {
  const nav = useNavigate();
  const register = useAuth((s) => s.register);
  const loading = useAuth((s) => s.loading);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    try {
      await register(email, password, name);
      nav("/");
    } catch (e: any) {
      setErr(apiError(e, "Registration failed. Check your connection and try again."));
    }
  }

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Free to try. No credit card needed."
      footer={
        <>
          Already have an account?{" "}
          <Link to="/login" className="font-medium text-accent-strong underline underline-offset-2 hover:text-accent-deep">
            Sign in
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <div>
          <label htmlFor="name" className="label">Name</label>
          <input
            id="name" className="field" autoComplete="name"
            value={name} onChange={(e) => setName(e.target.value)} required
          />
        </div>
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
            id="password" className="field" type="password" autoComplete="new-password"
            minLength={8} value={password} onChange={(e) => setPassword(e.target.value)}
            aria-describedby="password-hint" required
          />
          <p id="password-hint" className="meta mt-1.5">At least 8 characters.</p>
        </div>
        {err && <div role="alert" className="field-error">{err}</div>}
        <button className="btn-primary w-full" disabled={loading}>
          {loading && <Loader2 className="animate-spin" size={16} aria-hidden="true" />}
          {loading ? "Creating account…" : "Create account"}
        </button>
      </form>
    </AuthLayout>
  );
}
