import { Navigate, useNavigate } from "react-router";

import { useSession } from "@/features/auth";

import { LoginForm } from "../components/LoginForm";

export function LoginPage() {
  const navigate = useNavigate();
  const session = useSession();
  // Home decides where to go: the business's setup, its quotations, or "create a business".
  if (session.data) return <Navigate to="/" replace />;
  return (
    <main className="narrow">
      <h1>Sales Portal</h1>
      <LoginForm onSignedIn={() => navigate("/", { replace: true })} />
    </main>
  );
}
