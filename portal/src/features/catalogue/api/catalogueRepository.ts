import { http, tenantPath } from "@/shared/api/httpClient";

import type {
  CatalogueSummary,
  Category,
  Change,
  Price,
  PricePayload,
  Product,
  ProductPage,
  ProductPatch,
  ProductPayload,
  ProductQuery,
  StockCount,
  StockPayload,
} from "../model/types";

function path(tenantId: string, rest: string): string {
  return tenantPath(tenantId, `/catalogue${rest}`);
}

function product(tenantId: string, productId: string, rest = ""): string {
  return path(tenantId, `/products/${encodeURIComponent(productId)}${rest}`);
}

export const catalogueRepository = {
  summary: (tenantId: string) => http<CatalogueSummary>(path(tenantId, "/summary")),


  products: (tenantId: string, query: ProductQuery) =>
    http<ProductPage>(path(tenantId, "/products"), {
      query: {
        q: query.q || undefined,
        category_id: query.category_id || undefined,
        state: query.state,
        after: query.after,
        limit: query.limit === undefined ? undefined : String(query.limit),
      },
    }),

  product: (tenantId: string, productId: string) => http<Product>(product(tenantId, productId)),

  createProduct: (tenantId: string, body: ProductPayload) =>
    http<Product>(path(tenantId, "/products"), { method: "POST", body }),

  updateProduct: (tenantId: string, productId: string, body: ProductPatch) =>
    http<Product>(product(tenantId, productId), { method: "PATCH", body }),

  archive: (tenantId: string, productId: string, expectedVersion: number) =>
    http<Product>(product(tenantId, productId, "/archive"), {
      method: "POST",
      body: { expected_version: expectedVersion },
    }),

  restore: (tenantId: string, productId: string, expectedVersion: number) =>
    http<Product>(product(tenantId, productId, "/restore"), {
      method: "POST",
      body: { expected_version: expectedVersion },
    }),

  remove: (tenantId: string, productId: string) =>
    http<void>(product(tenantId, productId), { method: "DELETE" }),

  prices: (tenantId: string, productId: string) =>
    http<Price[]>(product(tenantId, productId, "/prices")),

  addPrice: (tenantId: string, productId: string, body: PricePayload) =>
    http<Price>(product(tenantId, productId, "/prices"), { method: "POST", body }),

  stock: (tenantId: string, productId: string) =>
    http<StockCount[]>(product(tenantId, productId, "/stock")),

  addStock: (tenantId: string, productId: string, body: StockPayload) =>
    http<StockCount>(product(tenantId, productId, "/stock"), { method: "POST", body }),

  changes: (tenantId: string, productId: string) =>
    http<Change[]>(product(tenantId, productId, "/changes")),

  categories: (tenantId: string) => http<Category[]>(path(tenantId, "/categories")),

  createCategory: (tenantId: string, name: string) =>
    http<Category>(path(tenantId, "/categories"), { method: "POST", body: { name } }),

  updateCategory: (
    tenantId: string,
    categoryId: string,
    body: { name?: string; archived?: boolean; expected_version: number },
  ) =>
    http<Category>(path(tenantId, `/categories/${encodeURIComponent(categoryId)}`), {
      method: "PATCH",
      body,
    }),
};
