import { execFileSync } from "node:child_process";
import { cpSync, mkdirSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

const [archive, destinationRoot, platformArg] = process.argv.slice(2);

if (!archive || !destinationRoot) {
  console.error(
    "Usage: node install-crsqlite-extension.mjs <archive.zip> <destination-root> [linux-x86_64|linux-aarch64|windows-x86_64|darwin-aarch64|darwin-x86_64]",
  );
  process.exit(1);
}

const platformExtensions = new Map([
  ["linux-x86_64", ".so"],
  ["linux-aarch64", ".so"],
  ["windows-x86_64", ".dll"],
  ["darwin-aarch64", ".dylib"],
  ["darwin-x86_64", ".dylib"],
]);
const defaultPlatform = () => {
  if (process.platform === "linux" && process.arch === "x64") {
    return "linux-x86_64";
  }
  if (process.platform === "linux" && process.arch === "arm64") {
    return "linux-aarch64";
  }
  if (process.platform === "win32" && process.arch === "x64") {
    return "windows-x86_64";
  }
  if (process.platform === "darwin" && process.arch === "arm64") {
    return "darwin-aarch64";
  }
  if (process.platform === "darwin" && process.arch === "x64") {
    return "darwin-x86_64";
  }
  throw new Error(`No packaged cr-sqlite target for ${process.platform}/${process.arch}`);
};

if (/\.(?:so|dll|dylib)$/i.test(destinationRoot)) {
  throw new Error("Destination must be the crsqlite root directory, not a shared-library filename");
}

const platformDir = platformArg ?? defaultPlatform();
const nativeExtension = platformExtensions.get(platformDir);
if (!nativeExtension) {
  throw new Error(`Unsupported cr-sqlite platform directory: ${platformDir}`);
}

const extractionDir = mkdtempSync(join(tmpdir(), "crsqlite-extension-"));
const nativeDestination = join(destinationRoot, platformDir, `crsqlite${nativeExtension}`);

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

  mkdirSync(join(destinationRoot, platformDir), { recursive: true });
  cpSync(library, nativeDestination);
  console.log(`Installed ${basename(library)} as ${nativeDestination}`);
} finally {
  rmSync(extractionDir, { recursive: true, force: true });
}
