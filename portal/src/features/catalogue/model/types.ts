// The business's own product list (phase 3). Numbers arrive as strings and are shown as they
// are: the portal never does maths on prices or quantities.

export type ProductState = "active" | "archived" | "all";
export type PriceState = "current" | "scheduled" | "past";

export interface CategoryRef {
  category_id: string;
  name: string;
  archived: boolean;
}

export interface Price {
  price_id: string;
  unit_price: string;
  currency: string;
  unit: string;
  price_list: string;
  revision: number;
  effective_from: string;
  effective_to: string | null;
  note: string | null;
  created_at: string;
  created_by: string;
  state: PriceState | null; // only in the price history
}

export interface StockCount {
  stock_id: string;
  quantity: string | null; // null: not tracked
  unit: string;
  location: string;
  as_of: string;
  source: string;
  note: string | null;
  recorded_at: string;
  recorded_by: string;
  stale: boolean | null; // only on the current count
}

export interface ProductSummary {
  product_id: string;
  sku: string;
  name: string;
  unit: string;
  category: CategoryRef | null;
  archived_at: string | null;
  row_version: number;
  current_price: Price | null;
  stock: StockCount | null;
}

export interface Product extends ProductSummary {
  description: string | null;
  minimum_quantity: string;
  quantity_step: string;
  scheduled_price: Price | null;
  created_at: string;
  updated_at: string;
}

export interface ProductPage {
  items: ProductSummary[];
  next_after: string | null;
}

export interface CatalogueSource {
  provider_key: string;
  display_name: string;
  editable: boolean;
}

export interface CatalogueSummary {
  source: CatalogueSource | null;
  active_products: number;
  priced_products: number;
  products_without_price: number;
  stale_stock: number;
  unknown_stock: number;
  max_active_products: number;
  units: string[];
  currency: string | null;
  minor_units: number | null;
  max_stock_age_seconds: number | null;
  time_zone: string | null;
}

export interface Category {
  category_id: string;
  name: string;
  archived_at: string | null;
  row_version: number;
  active_products: number;
}

export interface Change {
  entity: string;
  action: string;
  changes: Record<string, (string | null)[]>;
  actor: string;
  created_at: string;
}

/** The product form as typed (all text); "" category means none. */
export interface ProductForm {
  sku: string;
  name: string;
  unit: string;
  description: string;
  category_id: string;
  minimum_quantity: string;
  quantity_step: string;
}

export interface ProductPayload {
  sku: string;
  name: string;
  unit: string;
  description: string | null;
  category_id: string | null;
  minimum_quantity: string;
  quantity_step: string;
}

export type ProductPatch = Partial<Omit<ProductPayload, "sku">> & { expected_version: number };

export interface PricePayload {
  unit_price: string;
  effective_from: string | null; // null: now
  note: string | null;
  command_id: string;
}

export interface StockPayload {
  quantity: string | null; // null: not tracked
  as_of: string | null; // null: now
  note: string | null;
  command_id: string;
}

export interface ProductQuery {
  q?: string;
  category_id?: string;
  state: ProductState;
  after?: string;
  limit?: number;
}

/** A problem the server (or the form) found in one field. */
export interface FieldProblem {
  field: string;
  message: string;
}

