# houji-sukisu-kernel

Clean AOSP GKI (`android14-6.1-2025-06`, kernel `6.1.138`) build workflow for Xiaomi 14 (`houji`), integrating:
- **SukiSU-Ultra** (`builtin` branch, `UAPI=6`)
- **SuSFS v2.4.0** (`simonpunk/susfs4ksu`, `gki-android14-6.1` branch, including `CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=y`)
- **AOSP Kleaf / Bazel** (`--config=stamp --lto=thin --page_size=4k`), preserving GKI KMI compatibility (`CONFIG_TRIM_UNUSED_KSYMS=y`, `CONFIG_MODVERSIONS=y`) without unneeded third-party patches (no BBG, NoMount, BBRv3, or KPM).
