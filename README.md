# houji-sukisu-kernel (`OS3.0.307.0.WNCCNXM`)

Clean AOSP GKI (`6.1.138-android14-11-g0c3d559bcd85-ab14529422`, exact commit `0c3d559bcd85c64dae9c9c9687abbe5347fff3ad`) build workflow for Xiaomi 14 (`houji`, HyperOS `OS3.0.307.0.WNCCNXM`), integrating:
- **SukiSU-Ultra** (`builtin` branch, `UAPI=6`, `KSU_PACKAGE_NAME=com.sukisu.ultra`)
- **SuSFS v2.4.0** (`simonpunk/susfs4ksu`, `gki-android14-6.1` branch, including `CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=y` and `selinux_hide` dedicated handlers)
- **AOSP Kleaf / Bazel** (`--config=stamp --lto=none --page_size=4k`), strictly matching `OS3.0.307.0.WNCCNXM` `/proc/config.gz` (`CONFIG_LTO_NONE=y`, `CONFIG_CFI_CLANG=y`, `CONFIG_TRIM_UNUSED_KSYMS=y`, `CONFIG_MODVERSIONS=y`), locking `UTS_RELEASE` (`6.1.138-android14-11-g0c3d559bcd85-ab14529422`) and `UTS_VERSION` (`#1 SMP PREEMPT Wed Dec  3 02:05:30 UTC 2025`), and verifying `module_layout` CRC (`0xea759d7f`).
