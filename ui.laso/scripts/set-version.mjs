import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const requestedVersion = process.argv[2]?.replace(/^v/, "");

if (!requestedVersion || !/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(requestedVersion)) {
  console.error("Usage: pnpm version:set <major.minor.patch>");
  process.exit(1);
}

const packagePath = join(appRoot, "package.json");
const cargoPath = join(appRoot, "src-tauri/Cargo.toml");
const lockPath = join(appRoot, "src-tauri/Cargo.lock");

const packageMetadata = JSON.parse(readFileSync(packagePath, "utf8"));
packageMetadata.version = requestedVersion;
writeFileSync(packagePath, `${JSON.stringify(packageMetadata, null, 2)}\n`);

const cargoManifest = readFileSync(cargoPath, "utf8").replace(
  /(\[package\][\s\S]*?\nversion\s*=\s*")[^"]+(")/,
  `$1${requestedVersion}$2`,
);
writeFileSync(cargoPath, cargoManifest);

const cargoLock = readFileSync(lockPath, "utf8").replace(
  /(\[\[package\]\]\nname = "pharmacare"\nversion = ")[^"]+(")/,
  `$1${requestedVersion}$2`,
);
writeFileSync(lockPath, cargoLock);

console.log(`Updated desktop app version to ${requestedVersion}.`);
console.log(`Next: test, commit, and tag the release as v${requestedVersion}.`);
