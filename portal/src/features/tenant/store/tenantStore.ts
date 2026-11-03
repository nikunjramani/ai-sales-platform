import { create } from "zustand";
import { createJSONStorage, persist, type StateStorage } from "zustand/middleware";

// Client state (not server data): which business the person is currently working in.
// Kept in localStorage only as a convenience; the server still checks membership on every call.
interface TenantState {
  selectedTenantId: string | null;
  selectTenant: (tenantId: string) => void;
  clear: () => void;
}

// localStorage can be unavailable (private mode, blocked site data); then remember in memory.
const memory = new Map<string, string>();
const memoryStorage: StateStorage = {
  getItem: (key) => memory.get(key) ?? null,
  setItem: (key, value) => void memory.set(key, value),
  removeItem: (key) => void memory.delete(key),
};

function browserStorage(): StateStorage {
  try {
    const storage = window.localStorage;
    storage.getItem("sales-portal.probe");
    return storage;
  } catch {
    return memoryStorage;
  }
}

export const useTenantStore = create<TenantState>()(
  persist(
    (set) => ({
      selectedTenantId: null,
      selectTenant: (tenantId) => set({ selectedTenantId: tenantId }),
      clear: () => set({ selectedTenantId: null }),
    }),
    { name: "sales-portal.tenant", storage: createJSONStorage(browserStorage) },
  ),
);
