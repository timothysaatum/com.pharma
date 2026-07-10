import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

const [archive, destination] = process.argv.slice(2);

if (!archive || !destination) {
  console.error("Usage: node install-crsqlite-extension.mjs <archive.zip> <destination>");
  process.exit(1);
}

const extractionDir = mkdtempSync(join(tmpdir(), "crsqlite-extension-"));

try {
  const python = process.platform === "win32" ? "python" : "python3";
  execFileSync(python, ["-m", "zipfile", "-e", archive, extractionDir], {
    stdio: "inherit",
  });
  const library = readdirSync(extractionDir, { recursive: true })
    .map((entry) => join(extractionDir, entry.toString()))
    .find((entry) => /crsqlite\.(?:so|dll|dylib)$/i.test(entry));

  if (!library) {
    throw new Error(`No cr-sqlite shared library found in ${archive}`);
  }

  cpSync(library, destination);
  console.log(`Installed ${basename(library)} as ${destination}`);
} finally {
  rmSync(extractionDir, { recursive: true, force: true });
}
