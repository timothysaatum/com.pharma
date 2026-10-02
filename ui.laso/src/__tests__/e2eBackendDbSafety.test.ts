/**
 * The e2e backend helper must never be able to reach a real database.
 *
 * It used to default to the production database:
 *   process.env.TEST_DATABASE_URL || 'postgresql://...@localhost:5432/atlasdb'
 * so any test constructing BackendDatabase without an argument silently talked
 * to live pharmacy data. These assert both failure modes are closed.
 */
import { describe, expect, it, afterEach } from "vitest";
import { resolveTestDatabaseUrl } from "../../tests/e2e/helpers/backend-db";

const original = process.env.TEST_DATABASE_URL;

afterEach(() => {
  if (original === undefined) delete process.env.TEST_DATABASE_URL;
  else process.env.TEST_DATABASE_URL = original;
});

describe("resolveTestDatabaseUrl", () => {
  it("refuses to run when TEST_DATABASE_URL is unset", () => {
    delete process.env.TEST_DATABASE_URL;
    expect(() => resolveTestDatabaseUrl()).toThrow(/TEST_DATABASE_URL is not set/);
  });

  it("refuses to run when TEST_DATABASE_URL is empty", () => {
    process.env.TEST_DATABASE_URL = "";
    expect(() => resolveTestDatabaseUrl()).toThrow(/TEST_DATABASE_URL is not set/);
  });

  it("refuses atlasdb", () => {
    expect(() =>
      resolveTestDatabaseUrl("postgresql://cassie1:secret@localhost:5432/atlasdb")
    ).toThrow(/Refusing to run against database "atlasdb"/);
  });

  it("refuses atlasdb regardless of case or port", () => {
    expect(() =>
      resolveTestDatabaseUrl("postgresql://u:p@127.0.0.1:5433/ATLASDB")
    ).toThrow(/Refusing to run/);
    expect(() =>
      resolveTestDatabaseUrl("postgresql://u:p@db.internal:6543/atlasdb")
    ).toThrow(/Refusing to run/);
  });

  it("refuses the postgres maintenance database", () => {
    expect(() =>
      resolveTestDatabaseUrl("postgresql://u:p@localhost:5432/postgres")
    ).toThrow(/Refusing to run/);
  });

  it("the refusal names the production database explicitly", () => {
    // The error has to tell someone what not to do, not just "no".
    try {
      resolveTestDatabaseUrl("postgresql://u:p@localhost:5432/atlasdb");
      expect.unreachable("should have thrown");
    } catch (err) {
      expect((err as Error).message).toMatch(/atlasdb/);
      expect((err as Error).message).toMatch(/DROP SCHEMA/);
    }
  });

  it("accepts a disposable database", () => {
    const url = "postgresql://laso@localhost:5432/laso_test";
    expect(resolveTestDatabaseUrl(url)).toBe(url);
  });

  it("reads TEST_DATABASE_URL when no explicit argument is given", () => {
    const url = "postgresql://laso@localhost:5432/laso_test";
    process.env.TEST_DATABASE_URL = url;
    expect(resolveTestDatabaseUrl()).toBe(url);
  });

  it("an explicit argument cannot smuggle atlasdb past a safe env value", () => {
    process.env.TEST_DATABASE_URL = "postgresql://laso@localhost:5432/laso_test";
    expect(() =>
      resolveTestDatabaseUrl("postgresql://u:p@localhost:5432/atlasdb")
    ).toThrow(/Refusing to run/);
  });

  it("rejects an unparseable URL rather than passing it to pg", () => {
    expect(() => resolveTestDatabaseUrl("not-a-url")).toThrow(/not a parseable URL/);
  });

  it("rejects a URL with no database name", () => {
    expect(() => resolveTestDatabaseUrl("postgresql://laso@localhost:5432/")).toThrow(
      /not a parseable URL|names no database/
    );
  });
});