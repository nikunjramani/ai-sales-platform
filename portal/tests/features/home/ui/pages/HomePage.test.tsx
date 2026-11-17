import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { HomePage } from "@/features/home/ui/pages/HomePage";

import { jsonResponse, renderWithProviders } from "../../../../support/renderWithProviders";

const quotation = (id: string) => ({ quotation_id: id });

describe("HomePage", () => {
  it("shows a count card for each list the approver may see, and asks for nothing else", async () => {
    const fetch = vi.fn((url: string) =>
      Promise.resolve(
        jsonResponse(200, url.includes("/quotations") ? [quotation("q1"), quotation("q2"), quotation("q3")] : []),
      ),
    );
    vi.stubGlobal("fetch", fetch);
    renderWithProviders(<HomePage />);
    expect(await screen.findByText("3")).toBeTruthy();
    expect(screen.getByText("Quotes waiting for approval")).toBeTruthy();
    expect(screen.getByText("Customer requests that need you")).toBeTruthy();
    // The test approver may not draft, see orders or messages: those cards and lists stay away.
    expect(screen.queryByText("Quotes that need changes")).toBeNull();
    expect(screen.queryByText("Orders on hold")).toBeNull();
    const urls = fetch.mock.calls.map(([url]) => url);
    expect(urls.some((url) => url.includes("/orders") || url.includes("/conversations"))).toBe(false);
  });

  it("says All done when nothing needs the person", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(jsonResponse(200, []))));
    renderWithProviders(<HomePage />);
    expect(await screen.findByText("All done!")).toBeTruthy();
  });
});
