import { describe, expect, it } from "vitest";
import { nextSyncVersion } from "@/lib/localWrite";

describe("offline mutation sync versions", () => {
  it("keeps the initial version for creates", () => {
    expect(nextSyncVersion(undefined, "create")).toBe(1);
    expect(nextSyncVersion(4, "create")).toBe(4);
  });

  it("advances updates to the version required by the server", () => {
    expect(nextSyncVersion(1, "update")).toBe(2);
    expect(nextSyncVersion(7, "update")).toBe(8);
  });
});
