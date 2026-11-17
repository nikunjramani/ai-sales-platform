// Service: which "to do" cards the Home page shows and what they say. Pure, so it is easy to test.
//
// Input: the person's permissions and the counts already loaded from the existing lists.
// Output: the cards in a fixed order. A card the person may not use is never returned; a card
// whose count is still loading or failed carries that state, so it is never shown as 0.

import type { Permission } from "@/features/auth";

export type HomeCardId = "approve" | "changes" | "requests" | "orders";

export interface CountResult {
  /** Rows the list returned; undefined while loading or after an error. */
  count: number | undefined;
  failed: boolean;
}

export interface HomeCard {
  id: HomeCardId;
  title: string;
  /** The button's target: the list page, already filtered to what needs doing. */
  link: string;
  count: CountResult;
}

// The lists return at most this many rows on their first page (orders: 50 by default), so a
// full page means "at least this many", shown as "50+", never as an exact number.
export const LIST_CAP = 50;

export const HOME_CARDS: readonly {
  id: HomeCardId;
  title: string;
  link: string;
  permission: Permission;
}[] = [
  { id: "approve", title: "Quotes waiting for approval", link: "/quotations?filter=waiting", permission: "quotations:approve" },
  { id: "changes", title: "Quotes that need changes", link: "/quotations?filter=revision", permission: "quotations:draft" },
  { id: "requests", title: "Customer requests that need you", link: "/enquiries?filter=attention", permission: "enquiries:view" },
  { id: "orders", title: "Orders on hold", link: "/orders?filter=on_hold", permission: "orders:view" },
];

/** The cards this person may use, in order, with their counts. */
export function homeCards(
  can: (permission: Permission) => boolean,
  counts: Record<HomeCardId, CountResult>,
): HomeCard[] {
  return HOME_CARDS.filter((card) => can(card.permission)).map(({ id, title, link }) => ({
    id,
    title,
    link,
    count: counts[id],
  }));
}

/** "3", "50+", or "…" while unknown. */
export function countText(count: CountResult): string {
  if (count.count === undefined) return count.failed ? "?" : "…";
  return count.count >= LIST_CAP ? `${LIST_CAP}+` : String(count.count);
}

/** True only when every card has loaded and shows 0 (so "All done!" is never shown too early). */
export function allDone(cards: readonly HomeCard[]): boolean {
  return cards.length > 0 && cards.every((card) => card.count.count === 0);
}

/** "Good morning" / "Good afternoon" / "Good evening" by the person's local hour (0-23). */
export function greeting(hour: number): string {
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}

/** Setup progress for the "Get started" card: done and total of the checklist. */
export function setupProgress(items: readonly { done: boolean }[]): { done: number; total: number } {
  return { done: items.filter((item) => item.done).length, total: items.length };
}
