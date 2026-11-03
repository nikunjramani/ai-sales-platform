import { FileText, LogOut, UserRound } from "lucide-react";
import { Link, NavLink, useNavigate } from "react-router";

import { useLogout, useSession } from "@/features/auth";
import { roleNames } from "@/features/business";
import { TenantSwitcher, useCurrentTenant } from "@/features/tenant";

/** The strip at the top: product name, which business, who is signed in, and Sign out. */
export function TopBar() {
  const session = useSession();
  const { tenant } = useCurrentTenant();
  const logout = useLogout();
  const navigate = useNavigate();
  const user = session.data?.user;
  return (
    <header className="topbar">
      <Link to="/" className="brand">
        <span className="brand-mark" aria-hidden="true">
          <FileText size={20} />
        </span>
        <span className="brand-name">Sales Portal</span>
      </Link>
      <div className="who">
        <TenantSwitcher />
        <NavLink to="/profile" className="link small profile-link">
          <UserRound size={18} aria-hidden="true" />
          <span>
            {user?.display_name ?? user?.email ?? session.data?.subject}
            {tenant && <span className="muted role-names"> · {roleNames(tenant.roles)}</span>}
          </span>
        </NavLink>
        <button
          className="secondary"
          disabled={logout.isPending}
          onClick={() => logout.mutate(undefined, { onSettled: () => navigate("/login") })}
        >
          <LogOut size={18} aria-hidden="true" />
          <span>Sign out</span>
        </button>
      </div>
    </header>
  );
}
