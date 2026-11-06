/** Observability labels only; they do not grant privileges or change query execution. */
export const internalQueryTag = { client: "hardcharger-ui", internal: true };

export function observerQueryTag(page: string) {
  return { ...internalQueryTag, page, monitoring: page === "sql-monitor" };
}
