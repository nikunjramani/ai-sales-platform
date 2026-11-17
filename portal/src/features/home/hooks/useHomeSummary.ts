import { useSession } from "@/features/auth";
import { useSetupChecklist } from "@/features/business";
import { useInbox } from "@/features/conversations";
import { useEnquiryList } from "@/features/enquiries";
import { useOrders } from "@/features/orders";
import { useQuotationList } from "@/features/quotations";
import { useCurrentTenant } from "@/features/tenant";

import { homeCards, setupProgress, type CountResult } from "../services/homeCards";

// The same filters the list pages use, so a card's number matches what "Open" shows.
const WAITING = ["pending_approval"] as const;
const CHANGES = ["revision_requested", "pricing_failed"] as const;
const REQUESTS = ["needs_review", "needs_clarification"];
const ON_HOLD = { status: "on_hold" } as const;

function result(query: { data?: readonly unknown[]; isError: boolean }): CountResult {
  return { count: query.data?.length, failed: query.isError };
}

/**
 * Everything the Home page shows, built only from the lists the portal already has (no new API).
 * A list the person may not open is never requested.
 */
export function useHomeSummary() {
  const session = useSession();
  const { tenant, can } = useCurrentTenant();
  const settingUp = tenant?.status === "setting_up" && can("tenant:manage");

  const waiting = useQuotationList(WAITING, { enabled: can("quotations:approve") });
  const changes = useQuotationList(CHANGES, { enabled: can("quotations:draft") });
  const requests = useEnquiryList(REQUESTS, { enabled: can("enquiries:view") });
  const orders = useOrders(ON_HOLD, { enabled: can("orders:view") });
  const inbox = useInbox({ enabled: can("conversations:view") });
  const setup = useSetupChecklist({ enabled: settingUp });

  const user = session.data?.user;
  return {
    name: user?.display_name ?? null,
    businessName: tenant?.name ?? "",
    cards: homeCards(can, {
      approve: result(waiting),
      changes: result(changes),
      requests: result(requests),
      orders: result(orders),
    }),
    messages: can("conversations:view") ? { list: inbox.data?.slice(0, 5), query: inbox } : null,
    setup: settingUp && setup.data ? setupProgress(setup.data.items) : null,
  };
}
