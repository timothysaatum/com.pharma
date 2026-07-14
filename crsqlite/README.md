# CR-SQLite native extensions

These binaries are the vetted Linux CR-SQLite extension builds used by the
desktop client and the backend shadow database.

Source: <https://github.com/vlcn-io/cr-sqlite/releases/tag/v0.16.3>

## Layout

```text
crsqlite/
  linux-x86_64/crsqlite.so
  linux-aarch64/crsqlite.so
```

Runtime resolvers must select the directory that matches the process target
architecture. Do not add or depend on a top-level `crsqlite.so`; that filename
does not encode architecture and caused x86_64 development hosts to load an
ARM64 library.

## Verified release artifacts

The checked-in binaries were extracted from the official `vlcn-io/cr-sqlite`
v0.16.3 GitHub release assets.

| Target | Release asset | Asset SHA-256 | Library SHA-256 |
| --- | --- | --- | --- |
| Linux x86_64 | `crsqlite-linux-x86_64.zip` | `8f6fd31a2be2ba8c3101aad067a504a2e63c8e9b51cc4ace786009c02e7ecbae` | `6548af9fe19554dc972975ae7ed0e1a39aafd15944bf0292080d22965cd0eb96` |
| Linux aarch64 | `crsqlite-linux-aarch64.zip` | `c26aac668db9f0c455a37cbfba2ae129a11313a1f5b30b4bdcee69cfa1d1f94a` | `fdac7d80b4443a96e107f08f0d6e873cbd6666b884ae900b803394bdea9f31ab` |

