# Fork versioning and upstream collaboration

This fork keeps FlyingDiver's three-part upstream version as its base and appends a
fourth numeric component for fork releases:

```text
UPSTREAM_VERSION.FORK_REVISION
```

For example, `2026.0.1.1` is fork revision 1 based on upstream `2026.0.1`.
Additional fork releases from that same upstream base become `2026.0.1.2`,
`2026.0.1.3`, and so on. After incorporating a new upstream `2026.0.2`, the fork
counter resets and the first fork release becomes `2026.0.2.1`.

This format is intentional:

- Indigo requires `PluginVersion` to contain only numbers and periods and accepts
  four-part versions.
- The fork does not consume or imply ownership of FlyingDiver's next three-part
  upstream version.
- A later upstream `2026.0.2` sorts after fork build `2026.0.1.1`.
- Functional commits remain independent of the versioning commit, making them easy
  for upstream to review, cherry-pick, or merge.

## Git conventions

- `upstream/main` tracks `FlyingDiver/Indigo-miniUniFi`.
- `origin/main` is the tested integration branch for this fork.
- Focused changes should be developed as discrete commits so they can be proposed
  upstream without requiring fork-specific release metadata.
- Release tags exactly match the Indigo version: for example `v2026.0.1.1`.
- Fork-only versioning and release-workflow changes may be omitted from an upstream
  pull request when FlyingDiver prefers to assign the upstream release number.
