import type { ProductQuery } from "../model/types";

export const catalogueKeys = {
  all: (tenantId: string) => ["tenants", tenantId, "catalogue"] as const,
  summary: (tenantId: string) => ["tenants", tenantId, "catalogue", "summary"] as const,
  products: (tenantId: string, query: Omit<ProductQuery, "after">) =>
    ["tenants", tenantId, "catalogue", "products", query] as const,
  product: (tenantId: string, productId: string) =>
    ["tenants", tenantId, "catalogue", "product", productId] as const,
  prices: (tenantId: string, productId: string) =>
    ["tenants", tenantId, "catalogue", "product", productId, "prices"] as const,
  stock: (tenantId: string, productId: string) =>
    ["tenants", tenantId, "catalogue", "product", productId, "stock"] as const,
  changes: (tenantId: string, productId: string) =>
    ["tenants", tenantId, "catalogue", "product", productId, "changes"] as const,
  categories: (tenantId: string) => ["tenants", tenantId, "catalogue", "categories"] as const,
};
