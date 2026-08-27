import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { canAccess, isAdmin } from "./auth";
import { SearchResults } from "./pages";

describe("route and search presentation policy", () => {
  it("guards administrator-only areas", () => {
    expect(isAdmin({ roles: ["ADMIN"] } as never)).toBe(true);
    expect(canAccess({ roles: ["USER"] } as never, true)).toBe(false);
  });
  it("renders evidence when an answer is intentionally empty", () => {
    const html = renderToStaticMarkup(<SearchResults result={{ query: "q", answer: "", results: [{ chunk_id: "c", document_id: "uuid", content: "근거 내용", score: 0.1, metadata: null }] }} />);
    expect(html).toContain("근거 내용"); expect(html).not.toContain(">답변<");
  });
});
