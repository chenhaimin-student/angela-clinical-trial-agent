import type { CdeResource } from "./contracts.js";

const CDE_ORIGIN = "https://www.chinadrugtrials.org.cn";

const PATH_BY_KIND = {
  catalog_page: "/clinicaltrials.searchlist.dhtml",
  result_page: "/clinicaltrials.searchlist.dhtml",
  rss_page: "/clinicaltrials.search.rss.dhtml",
  detail: "/clinicaltrials.searchlistdetail.dhtml",
} as const;

export function buildCdeResourceUrl(resource: CdeResource): string {
  const url = new URL(PATH_BY_KIND[resource.kind], CDE_ORIGIN);
  if (resource.kind === "detail") {
    url.searchParams.set("id", resource.detail_id);
  } else if (resource.kind === "catalog_page") {
    url.searchParams.set("currentpage", String(resource.page));
  } else {
    url.searchParams.set("keywords", resource.query_term);
    url.searchParams.set("currentpage", String(resource.page));
  }
  return url.toString();
}
