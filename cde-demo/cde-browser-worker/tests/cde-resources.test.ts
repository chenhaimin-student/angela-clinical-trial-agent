import { describe, expect, it } from "vitest";

import { buildCdeResourceUrl } from "../src/cde-resources.js";

describe("CDE resource policy", () => {
  it("builds the official empty-condition catalog URL", () => {
    expect(buildCdeResourceUrl({ kind: "catalog_page", page: 321 })).toBe(
      "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlist.dhtml?currentpage=321",
    );
  });
  it("builds a fixed encoded RSS URL", () => {
    expect(
      buildCdeResourceUrl({
        kind: "rss_page",
        query_term: "示例申办方",
        page: 2,
      }),
    ).toBe(
      "https://www.chinadrugtrials.org.cn/clinicaltrials.search.rss.dhtml?keywords=%E7%A4%BA%E4%BE%8B%E7%94%B3%E5%8A%9E%E6%96%B9&currentpage=2",
    );
  });

  it("builds a fixed detail URL", () => {
    expect(
      buildCdeResourceUrl({
        kind: "detail",
        detail_id: "22199abfda4c47769ab76e5870115461",
      }),
    ).toBe(
      "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlistdetail.dhtml?id=22199abfda4c47769ab76e5870115461",
    );
  });

  it("preserves the official _p suffix in a detail URL", () => {
    expect(
      buildCdeResourceUrl({
        kind: "detail",
        detail_id: "2457661b7c934cbbb45a6bfe91a97cfc_p",
      }),
    ).toBe(
      "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlistdetail.dhtml?id=2457661b7c934cbbb45a6bfe91a97cfc_p",
    );
  });

  it("preserves an official legacy numeric detail id", () => {
    expect(
      buildCdeResourceUrl({ kind: "detail", detail_id: "2128_p" }),
    ).toBe(
      "https://www.chinadrugtrials.org.cn/clinicaltrials.searchlistdetail.dhtml?id=2128_p",
    );
  });
});
