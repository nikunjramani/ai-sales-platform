import { Package, Plus, Tags } from "lucide-react";
import { useDeferredValue } from "react";
import { Link } from "react-router";

import { PageHeader } from "@/shared/ui/PageHeader";
import { useCanEditCatalogue, useCatalogueSummary, useCategories, useProducts } from "../../hooks/useCatalogue";
import { useProductFilters } from "../../hooks/useProductFilters";
import { CatalogueSourceBanner } from "../components/CatalogueSourceBanner";
import { ProductFilters } from "../components/ProductFilters";
import { ProductTable } from "../components/ProductTable";
import { QueryState } from "@/shared/ui/QueryState";

export function ProductListPage() {
  const summary = useCatalogueSummary();
  const categories = useCategories();
  const canEdit = useCanEditCatalogue();
  const filters = useProductFilters();
  const text = useDeferredValue(filters.text.trim());
  const products = useProducts({ q: text, category_id: filters.categoryId, state: filters.state });
  const items = products.data?.pages.flatMap((page) => page.items) ?? [];
  const counts = summary.data;
  return (
    <section>
      <PageHeader
        icon={Package}
        title="Products"
        hint="Everything you sell, with its price and stock. Quotes use these prices."
        actions={
          canEdit && (
            <>
              <Link className="button primary" to="/catalogue/new">
                <Plus size={20} aria-hidden="true" />
                Add product
              </Link>
              <Link className="button" to="/catalogue/categories">
                <Tags size={20} aria-hidden="true" />
                Categories
              </Link>
            </>
          )
        }
      />
      {counts && <CatalogueSourceBanner summary={counts} />}
      {counts && (
        <p className="muted small">
          {counts.active_products} active of {counts.max_active_products.toLocaleString()} allowed ·{" "}
          {counts.products_without_price} without a price · {counts.stale_stock} with an old stock
          count · prices exclude tax
        </p>
      )}
      <ProductFilters
        text={filters.text}
        categoryId={filters.categoryId}
        state={filters.state}
        categories={categories.data ?? []}
        onText={filters.setText}
        onCategory={filters.setCategoryId}
        onState={filters.setState}
      />
      <QueryState
        isLoading={products.isLoading}
        error={products.error}
        isEmpty={items.length === 0}
        emptyText={canEdit ? "No products yet. Add your first product." : "No products found."}
      >
        <ProductTable products={items} />
        {products.hasNextPage && (
          <button className="secondary" disabled={products.isFetchingNextPage} onClick={() => void products.fetchNextPage()}>
            Show more
          </button>
        )}
      </QueryState>
    </section>
  );
}
