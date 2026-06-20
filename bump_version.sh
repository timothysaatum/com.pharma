#!/bin/bash

# Usage: ./bump_version.sh <new_version>
# Example: ./bump_version.sh 1.0.1

NEW_VERSION=$1

if [ -z "$NEW_VERSION" ]; then
  echo "Usage: ./bump_version.sh <new_version>"
  exit 1
fi

echo "Bumping version to $NEW_VERSION..."

# 1. ui.laso/package.json
sed -i "s/\"version\": \".*\"/\"version\": \"$NEW_VERSION\"/" ui.laso/package.json

# 2. ui.laso/src-tauri/tauri.conf.json
sed -i "s/\"version\": \".*\"/\"version\": \"$NEW_VERSION\"/" ui.laso/src-tauri/tauri.conf.json

# 3. ui.laso/src-tauri/Cargo.toml
sed -i "0,/version = \".*\"/s/version = \".*\"/version = \"$NEW_VERSION\"/" ui.laso/src-tauri/Cargo.toml

# 4. backend.laso/app/core/config.py
sed -i "s/VERSION: str = \".*\"/VERSION: str = \"$NEW_VERSION\"/" backend.laso/app/core/config.py

echo "Done. Version synchronized across all components."
