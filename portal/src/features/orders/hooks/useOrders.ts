import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useTenantId } from "@/features/tenant";
import { isApiError } from "@/shared/api/httpClient";

import { orderRepository } from "../api/orderRepository";
import type { Order, OrderCreation, OrderListFilter, OrderStatus } from "../model/types";

export const orderKeys = {
  all: (tenantId: string) => ["tenants", tenantId, "orders"] as const,
  list: (tenantId: string, filter: OrderListFilter) =>
    ["tenants", tenantId, "orders", "list", filter] as const,
  one: (tenantId: string, id: string) => ["tenants", tenantId, "orders", "one", id] as const,
  quotation: (tenantId: string, id: string) =>
    ["tenants", tenantId, "orders", "quotation", id] as const,
  settings: (tenantId: string) => ["tenants", tenantId, "orders", "settings"] as const,
};

// Automatic orders and status changes happen in the background: the screens follow them.
const REFRESH_MS = 15_000;

export function useOrders(filter: OrderListFilter, { enabled = true } = {}) {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: orderKeys.list(tenantId, filter),
    queryFn: () => orderRepository.list(tenantId, filter),
    refetchInterval: REFRESH_MS,
    enabled,
  });
}

export function useOrder(orderId: string) {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: orderKeys.one(tenantId, orderId),
    queryFn: () => orderRepository.detail(tenantId, orderId),
    refetchInterval: REFRESH_MS,
  });
}

/** The quotation's order, or null while it has none (the API answers 404). */
export function useQuotationOrder(quotationId: string) {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: orderKeys.quotation(tenantId, quotationId),
    queryFn: async (): Promise<Order | null> => {
      try {
        return await orderRepository.forQuotation(tenantId, quotationId);
      } catch (error) {
        if (isApiError(error, 404)) return null;
        throw error;
      }
    },
  });
}

export function useCreateOrder(quotationId: string) {
  const tenantId = useTenantId();
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => orderRepository.create(tenantId, quotationId),
    onSettled: () => client.invalidateQueries({ queryKey: orderKeys.all(tenantId) }),
  });
}

export function useOrderActions(orderId: string) {
  const tenantId = useTenantId();
  const client = useQueryClient();
  const refresh = () => client.invalidateQueries({ queryKey: orderKeys.all(tenantId) });
  const move = useMutation({
    mutationFn: ({ status, reason }: { status: OrderStatus; reason: string | null }) =>
      orderRepository.move(tenantId, orderId, status, reason),
    onSettled: refresh,
  });
  return { move };
}

export function useOrderSettings() {
  const tenantId = useTenantId();
  const client = useQueryClient();
  const query = useQuery({
    queryKey: orderKeys.settings(tenantId),
    queryFn: () => orderRepository.settings(tenantId),
  });
  const save = useMutation({
    mutationFn: (value: OrderCreation) =>
      orderRepository.setSettings(tenantId, { order_creation: value }),
    onSettled: () => client.invalidateQueries({ queryKey: orderKeys.settings(tenantId) }),
  });
  return { query, save };
}
