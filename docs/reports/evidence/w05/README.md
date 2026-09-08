# W05 verification evidence

Each batch retains readable task.json and result.json. Original candidate file hashes, candidate patch and process output are preserved in batch-artifacts.tar.xz, sharing compression across repeated snapshots. completed-batches.json maps each original artifact to its member, raw length and SHA-256. archive-manifest.json records the archive checksum. Every decompressed member was checked against the uncompressed original before removing duplicate copies. Local .work-package-runs/W05 retains the original files.

Inspect an original with `tar -xOf batch-artifacts.tar.xz <batch>/output.log`. Failed batches remain failures. ci-002 failed at the final package build because of external PyPI TLS; package-001 separately passed the same actual build and isolated-install gate using cached dependencies. Neither local result closes the external Provider acceptance requirement.
