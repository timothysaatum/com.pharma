import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

vi.mock("../client", () => ({
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
}));

import { contractsApi } from "../contracts";
import { get as mockedGet } from "../client";

const typedGet = mockedGet as unknown as Mock;

describe("contractsApi", () => {
    beforeEach(() => {
        typedGet.mockReset();
    });

    it("should call the contract code availability endpoint with encoded contract codes", async () => {
        typedGet.mockResolvedValueOnce({ available: true, code: "NEW CODE" });

        const result = await contractsApi.checkCode("NEW CODE");

        expect(typedGet).toHaveBeenCalledTimes(1);
        expect(typedGet).toHaveBeenCalledWith("/contracts/check-code/NEW%20CODE");
        expect(result).toEqual({ available: true, code: "NEW CODE" });
    });
});
