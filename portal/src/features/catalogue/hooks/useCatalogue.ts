import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { useCurrentTenant, useTenantId } from "@/features/tenant";
import { isApiError } from "@/shared/api/httpClient";

import { catalogueRepository } from "../api/catalogueRepository";
import type { ProductQuery } from "../model/types";
import { conflictCodeFrom } from "../services/productFormService";
import { catalogueKeys } from "./queryKeys";

export function useCatalogueSummary() {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: catalogueKeys.summary(tenantId),
    queryFn: () => catalogueRepository.summary(tenantId),
  });
}

/** May the current person change products here? (UI hint; the server checks again.) */
export function useCanEditCatalogue(): boolean {
  const { can } = useCurrentTenant();
  const summary = useCatalogueSummary();
  return can("catalogue:manage") && summary.data?.source?.editable === true;
}

export function useProducts(query: Omit<ProductQuery, "after">) {
  const tenantId = useTenantId();
  return useInfiniteQuery({
    queryKey: catalogueKeys.products(tenantId, query),
    queryFn: ({ pageParam }) =>
      catalogueRepository.products(tenantId, { ...query, after: pageParam ?? undefined }),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_after,
  });
}

export function useProduct(productId: string | undefined) {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: catalogueKeys.product(tenantId, productId ?? ""),
    queryFn: () => catalogueRepository.product(tenantId, productId ?? ""),
    enabled: Boolean(productId),
  });
}

export function usePriceHistory(productId: string) {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: catalogueKeys.prices(tenantId, productId),
    queryFn: () => catalogueRepository.prices(tenantId, productId),
  });
}

export function useStockHistory(productId: string) {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: catalogueKeys.stock(tenantId, productId),
    queryFn: () => catalogueRepository.stock(tenantId, productId),
  });
}

export function useChanges(productId: string) {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: catalogueKeys.changes(tenantId, productId),
    queryFn: () => catalogueRepository.changes(tenantId, productId),
  });
}

export function useCategories() {
  const tenantId = useTenantId();
  return useQuery({
    queryKey: catalogueKeys.categories(tenantId),
    queryFn: () => catalogueRepository.categories(tenantId),
  });
}

/** After any change, everything shown about the catalogue may be outdated. */
export function useRefreshCatalogue() {
  const tenantId = useTenantId();
  const client = useQueryClient();
  return () => client.invalidateQueries({ queryKey: catalogueKeys.all(tenantId) });
}

export function useProductActions(productId: string) {
  const tenantId = useTenantId();
  const refresh = useRefreshCatalogue();
  const archive = useMutation({
    mutationFn: (version: number) => catalogueRepository.archive(tenantId, productId, version),
    onSettled: refresh,
  });
  const restore = useMutation({
    mutationFn: (version: number) => catalogueRepository.restore(tenantId, productId, version),
    onSettled: refresh,
  });
  const remove = useMutation({
    mutationFn: () => catalogueRepository.remove(tenantId, productId),
    onSettled: refresh,
  });
  const error = archive.error ?? restore.error ?? remove.error;
  const referenced = isApiError(error, 409) && conflictCodeFrom(error.detail) === "product_referenced";
  return {
    archive,
    restore,
    remove,
    problem: referenced
      ? "This product has prices, stock counts or quotations, so it can only be archived."
      : (error?.message ?? null),
  };
}
