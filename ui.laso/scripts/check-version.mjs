import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const appRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const read = (file) => readFileSync(join(appRoot, file), "utf8");
const errors = [];

const packageMetadata = JSON.parse(read("package.json"));
const tauriConfig = JSON.parse(read("src-tauri/tauri.conf.json"));
const cargoManifest = read("src-tauri/Cargo.toml");
const cargoLock = read("src-tauri/Cargo.lock");
const version = packageMetadata.version;

if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(version)) {
  errors.push(`package.json has invalid SemVer: ${version}`);
}

if (tauriConfig.version !== "../package.json") {
  errors.push(
    `tauri.conf.json must use ../package.json as its version source (found ${tauriConfig.version})`,
  );
}

const cargoVersion = cargoManifest.match(
  /\[package\][\s\S]*?\nversion\s*=\s*"([^"]+)"/,
)?.[1];
if (cargoVersion !== version) {
  errors.push(`Cargo.toml version ${cargoVersion ?? "(missing)"} does not match ${version}`);
}

const lockVersion = cargoLock.match(
  /\[\[package\]\]\nname = "pharmacare"\nversion = "([^"]+)"/,
)?.[1];
if (lockVersion !== version) {
  errors.push(`Cargo.lock version ${lockVersion ?? "(missing)"} does not match ${version}`);
}

const releaseTag = process.argv[2] || process.env.RELEASE_TAG;
if (releaseTag && releaseTag !== `v${version}`) {
  errors.push(`release tag ${releaseTag} does not match app version v${version}`);
}

if (errors.length > 0) {
  console.error(errors.map((error) => `- ${error}`).join("\n"));
  process.exit(1);
}

console.log(
  releaseTag
    ? `Version check passed: ${releaseTag}`
    : `Version check passed: ${version}`,
);
