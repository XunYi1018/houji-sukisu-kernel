#!/usr/bin/env python3
"""Integrate SukiSU-Ultra (builtin) and SuSFS (gki-android14-6.1) into AOSP common GKI 6.1.138."""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import urllib.request


KSU_VERSION_BASE = 40000
KSU_VERSION_OFFSET = 2815


def run(cmd, cwd=None, check=True, capture=False):
    print(f"+ {' '.join(cmd)}")
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
    )


def compute_sukisu_version(ksu_dir: str, branch: str) -> tuple[int, str]:
    """Compute real KSU_VERSION and KSU_VERSION_FULL outside Bazel sandbox."""
    short_sha = run(["git", "rev-parse", "--short=8", "HEAD"], cwd=ksu_dir, capture=True).stdout.strip()

    tag = None
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                "https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/releases/latest",
                headers={"User-Agent": "houji-builder"},
            ),
            timeout=15,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tag = (data.get("tag_name") or "").lstrip("v")
    except Exception as exc:
        print(f"Warning: GitHub releases API fallback ({exc})")

    if not tag:
        res = run(["git", "describe", "--tags", "--abbrev=0"], cwd=ksu_dir, check=False, capture=True)
        tag = res.stdout.strip().lstrip("v") if res.returncode == 0 and res.stdout.strip() else "4.3.0"

    commit_count = None
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/SukiSU-Ultra/SukiSU-Ultra/commits?sha=main&per_page=1",
            method="HEAD",
            headers={"User-Agent": "houji-builder"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            link = resp.headers.get("Link", "")
            m = re.search(r"page=(\d+)>;\s*rel=[\"']last[\"']", link, re.I)
            if m:
                commit_count = int(m.group(1))
    except Exception as exc:
        print(f"Warning: GitHub commits API fallback ({exc})")

    if commit_count is None:
        res = run(["git", "rev-list", "--count", "HEAD"], cwd=ksu_dir, check=False, capture=True)
        commit_count = int(res.stdout.strip()) if res.returncode == 0 else 3994

    ksu_version = KSU_VERSION_BASE + commit_count - KSU_VERSION_OFFSET
    ksu_version_full = f"v{tag}-{short_sha}@{branch}"
    return ksu_version, ksu_version_full


def wire_kernelsu_kbuild(common_dir: str, ksu_dir: str, branch: str, ksu_version: int, ksu_version_full: str) -> None:
    dst_ksu = os.path.join(common_dir, "drivers", "kernelsu")
    if os.path.exists(dst_ksu):
        shutil.rmtree(dst_ksu)
    shutil.copytree(os.path.join(ksu_dir, "kernel"), dst_ksu, symlinks=False)

    # Wire drivers/Makefile
    drivers_mk = os.path.join(common_dir, "drivers", "Makefile")
    with open(drivers_mk, "r", encoding="utf-8") as f:
        mk_txt = f.read()
    entry = "obj-$(CONFIG_KSU) += kernelsu/\n"
    if "kernelsu/" not in mk_txt:
        if not mk_txt.endswith("\n"):
            mk_txt += "\n"
        mk_txt += entry
        with open(drivers_mk, "w", encoding="utf-8", newline="\n") as f:
            f.write(mk_txt)

    # Wire drivers/Kconfig right before final endmenu
    drivers_kc = os.path.join(common_dir, "drivers", "Kconfig")
    with open(drivers_kc, "r", encoding="utf-8") as f:
        kc_txt = f.read()
    if 'drivers/kernelsu/Kconfig' not in kc_txt:
        idx = kc_txt.rfind("endmenu")
        if idx == -1:
            raise RuntimeError("drivers/Kconfig missing endmenu")
        kc_txt = kc_txt[:idx] + 'source "drivers/kernelsu/Kconfig"\n\n' + kc_txt[idx:]
        with open(drivers_kc, "w", encoding="utf-8", newline="\n") as f:
            f.write(kc_txt)

    # Install sanitized kernel UAPI headers inside drivers/kernelsu/uapi_hdrs
    # (Bazel kernel_build does not run `make headers_install`, yet fs/exec.c expects preinit symbols unless KSU_NO_PREINIT matches everywhere.)
    uapi_dir = os.path.join(dst_ksu, "uapi_hdrs")
    hdr_tmp = "/tmp/houji_uapi_hdr_build"
    if os.path.exists(hdr_tmp):
        shutil.rmtree(hdr_tmp)
    os.makedirs(hdr_tmp, exist_ok=True)
    run([
        "make", "-C", common_dir, "ARCH=arm64", f"O={hdr_tmp}",
        f"INSTALL_HDR_PATH={uapi_dir}", "headers_install",
    ])
    shutil.rmtree(hdr_tmp, ignore_errors=True)

    unistd_h = os.path.join(uapi_dir, "include", "linux", "unistd.h")
    if not os.path.isfile(unistd_h):
        raise FileNotFoundError(f"Expected {unistd_h} after headers_install")

    # Patch drivers/kernelsu/Makefile cleanly for Bazel offline sandbox
    ksu_mk = os.path.join(dst_ksu, "Makefile")
    with open(ksu_mk, "r", encoding="utf-8") as f:
        ksu_mk_txt = f.read()

    old_uapi = "KSU_UAPI_INC := $(objtree)/usr/include"
    new_uapi = (
        "KSU_UAPI_INC := $(if $(wildcard $(objtree)/usr/include/linux/unistd.h),"
        "$(objtree)/usr/include,$(KSU_KERNEL_DIR)/uapi_hdrs/include)"
    )
    if old_uapi not in ksu_mk_txt:
        raise RuntimeError("Could not find KSU_UAPI_INC line in drivers/kernelsu/Makefile")
    ksu_mk_txt = ksu_mk_txt.replace(old_uapi, new_uapi, 1)

    old_runner_cmd = "      cmd_ksu_runner = $(CC) $(KSU_RUNNER_CFLAGS) -c -o $@.o $< && \\"
    new_runner_cmd = "      cmd_ksu_runner = mkdir -p $(dir $@) && $(CC) $(KSU_RUNNER_CFLAGS) -c -o $@.o $< && \\"
    if old_runner_cmd in ksu_mk_txt:
        ksu_mk_txt = ksu_mk_txt.replace(old_runner_cmd, new_runner_cmd, 1)

    if "ccflags-y += -DKSU_VERSION=$(KSU_VERSION)" not in ksu_mk_txt:
        raise RuntimeError("Could not find KSU_VERSION ccflags in drivers/kernelsu/Makefile")
    # Pin exact computed KSU_VERSION / KSU_VERSION_FULL right before ccflags-y
    pin_lines = (
        f"KSU_VERSION := {ksu_version}\n"
        f"KSU_VERSION_FULL := {ksu_version_full}\n"
        "$(info -- $(REPO_NAME) pinned version: $(KSU_VERSION) [$(KSU_VERSION_FULL)])\n"
    )
    ksu_mk_txt = ksu_mk_txt.replace(
        "ccflags-y += -DKSU_VERSION=$(KSU_VERSION)",
        pin_lines + "ccflags-y += -DKSU_VERSION=$(KSU_VERSION)",
        1,
    )
    with open(ksu_mk, "w", encoding="utf-8", newline="\n") as f:
        f.write(ksu_mk_txt)


def normalize_susfs_patch_for_aosp(patch_text: str) -> str:
    """Fix 3 trailing #include context lines between android14-6.1-2025-06 (6.1.138) and newer 6.1 GKI."""
    text = patch_text.replace("\r\n", "\n")

    replacements = [
        (
            """ #include <linux/io_uring.h>
 #include <linux/syscall_user_dispatch.h>
 #include <linux/coredump.h>
+#ifdef CONFIG_KSU_SUSFS
+#include <linux/susfs_def.h>
+#endif
 
 #ifndef __GENKSYMS__
 #include <linux/dma-buf.h>""",
            """ #include <linux/io_uring.h>
 #include <linux/syscall_user_dispatch.h>
 #include <linux/coredump.h>
+#ifdef CONFIG_KSU_SUSFS
+#include <linux/susfs_def.h>
+#endif
 
 #include <linux/uaccess.h>
 #include <asm/mmu_context.h>""",
        ),
        (
            """@@ -32,11 +32,21 @@
 #include <linux/fs_context.h>
 #include <linux/shmem_fs.h>
 #include <linux/mnt_idmapping.h>
+#ifdef CONFIG_KSU_SUSFS
+#include <linux/susfs_def.h>
+#endif // #ifdef CONFIG_KSU_SUSFS
 
 #include "pnode.h"
 #include "internal.h"
 #include <trace/hooks/blk.h>""",
            """@@ -32,10 +32,20 @@
 #include <linux/fs_context.h>
 #include <linux/shmem_fs.h>
 #include <linux/mnt_idmapping.h>
+#ifdef CONFIG_KSU_SUSFS
+#include <linux/susfs_def.h>
+#endif // #ifdef CONFIG_KSU_SUSFS
 
 #include "pnode.h"
 #include "internal.h\"""",
        ),
        (
            """@@ -100,6 +100,10 @@
 #include <linux/cn_proc.h>
 #include <linux/cpufreq_times.h>
 #include <linux/dma-buf.h>
+#if defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)
+#include <linux/susfs_def.h>
+#endif // #if defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)
+
 #include <trace/events/oom.h>
 #include <trace/hooks/sched.h>
 #include "internal.h\"""",
            """@@ -100,5 +100,9 @@
 #include <linux/cn_proc.h>
 #include <linux/cpufreq_times.h>
+#if defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)
+#include <linux/susfs_def.h>
+#endif // #if defined(CONFIG_KSU_SUSFS_SUS_MAP) || defined(CONFIG_KSU_SUSFS_OPEN_REDIRECT)
+
 #include <trace/events/oom.h>
 #include <trace/hooks/sched.h>
 #include "internal.h\"""",
        ),
    ]

    for old, new in replacements:
        if old in text:
            text = text.replace(old, new, 1)
    return text


def apply_susfs(common_dir: str, susfs_dir: str) -> str:
    kp_dir = os.path.join(susfs_dir, "kernel_patches")
    for src in glob.glob(os.path.join(kp_dir, "fs", "*")):
        shutil.copy2(src, os.path.join(common_dir, "fs", os.path.basename(src)))
    for src in glob.glob(os.path.join(kp_dir, "include", "linux", "*")):
        shutil.copy2(src, os.path.join(common_dir, "include", "linux", os.path.basename(src)))

    patch_src = os.path.join(kp_dir, "50_add_susfs_in_gki-android14-6.1.patch")
    with open(patch_src, "r", encoding="utf-8") as f:
        raw_patch = f.read().replace("\r\n", "\n")

    tmp_patch = "/tmp/50_susfs_prepared.patch"
    with open(tmp_patch, "w", encoding="utf-8", newline="\n") as f:
        f.write(raw_patch)

    # Try exact upstream patch first; only adapt context if upstream didn't already fix it
    chk = run(["git", "apply", "--check", tmp_patch], cwd=common_dir, check=False, capture=True)
    if chk.returncode != 0:
        print("Upstream 50_add_susfs patch needed 2025-06 #include context adaptation; normalizing...")
        fixed = normalize_susfs_patch_for_aosp(raw_patch)
        with open(tmp_patch, "w", encoding="utf-8", newline="\n") as f:
            f.write(fixed)
        run(["git", "apply", "--check", tmp_patch], cwd=common_dir)

    run(["git", "apply", tmp_patch], cwd=common_dir)

    susfs_h = os.path.join(common_dir, "include", "linux", "susfs.h")
    with open(susfs_h, "r", encoding="utf-8") as f:
        m = re.search(r'^#define\s+SUSFS_VERSION\s+"([^"]+)"', f.read(), re.M)
    ver = m.group(1) if m else "unknown"
    print(f"Integrated SUSFS version: {ver}")
    return ver


def configure_kernel(common_dir: str) -> None:
    defconfig = os.path.join(common_dir, "arch", "arm64", "configs", "gki_defconfig")
    with open(defconfig, "r", encoding="utf-8") as f:
        cfg = f.read()

    additions = [
        "",
        "# SukiSU-Ultra (builtin) + SuSFS",
        "CONFIG_KSU=y",
        "CONFIG_KSU_FEATURE_ADBROOT=y",
        "# CONFIG_KSU_DEBUG is not set",
        "# CONFIG_KPM is not set",
        "CONFIG_KSU_SUSFS=y",
        "CONFIG_KSU_SUSFS_SUS_PATH=y",
        "CONFIG_KSU_SUSFS_SUS_MOUNT=y",
        "CONFIG_KSU_SUSFS_SUS_KSTAT=y",
        "CONFIG_KSU_SUSFS_SPOOF_UNAME=y",
        "CONFIG_KSU_SUSFS_ENABLE_LOG=y",
        "CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y",
        "CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y",
        "CONFIG_KSU_SUSFS_OPEN_REDIRECT=y",
        "CONFIG_KSU_SUSFS_SUS_MAP=y",
        "CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER=y",
        "CONFIG_OVERLAY_FS=y",
        "CONFIG_TMPFS_XATTR=y",
        "CONFIG_TMPFS_POSIX_ACL=y",
    ]
    if not cfg.endswith("\n"):
        cfg += "\n"
    cfg += "\n".join(additions) + "\n"
    with open(defconfig, "w", encoding="utf-8", newline="\n") as f:
        f.write(cfg)

    # Clear POST_DEFCONFIG_CMDS="check_defconfig" so added configs pass cleanly
    bc_gki = os.path.join(common_dir, "build.config.gki")
    with open(bc_gki, "r", encoding="utf-8") as f:
        bc_txt = f.read()
    bc_txt = re.sub(r'^POST_DEFCONFIG_CMDS=.*$', 'POST_DEFCONFIG_CMDS=""', bc_txt, flags=re.M)
    with open(bc_gki, "w", encoding="utf-8", newline="\n") as f:
        f.write(bc_txt)

    # Empty protected exports & protected modules lists while preserving Bazel target labels
    for pattern in ("abi_gki_protected_exports*", "gki_*_protected_modules"):
        for path in glob.glob(os.path.join(common_dir, "android", pattern)):
            print(f"Clearing {os.path.relpath(path, common_dir)}")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("")


def commit_clean_tree(common_dir: str, message: str) -> str:
    # Ensure git does not ignore uapi_hdrs/include/*.h or anything required by Bazel
    run(["git", "add", "-f", "drivers/kernelsu"], cwd=common_dir)
    run(["git", "add", "-A"], cwd=common_dir)
    run(["git", "commit", "-m", message], cwd=common_dir)
    status = run(["git", "status", "--porcelain"], cwd=common_dir, capture=True).stdout.strip()
    if status:
        raise RuntimeError(f"Expected clean git status in common/, found:\n{status}")
    sha = run(["git", "rev-parse", "--short=12", "HEAD"], cwd=common_dir, capture=True).stdout.strip()
    print(f"Clean committed common HEAD: {sha}")
    return sha


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--ksu-branch", default="builtin")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    common_dir = os.path.join(ws, "common")
    ksu_dir = os.path.join(ws, "upstream", "SukiSU-Ultra")
    susfs_dir = os.path.join(ws, "upstream", "susfs4ksu")

    ksu_version, ksu_version_full = compute_sukisu_version(ksu_dir, args.ksu_branch)
    print(f"Resolved SukiSU-Ultra: {ksu_version} ({ksu_version_full})")

    wire_kernelsu_kbuild(common_dir, ksu_dir, args.ksu_branch, ksu_version, ksu_version_full)
    susfs_ver = apply_susfs(common_dir, susfs_dir)
    configure_kernel(common_dir)
    head_sha = commit_clean_tree(
        common_dir,
        f"houji: integrate SukiSU-Ultra {ksu_version_full} and SUSFS {susfs_ver}",
    )

    out_env = os.environ.get("GITHUB_ENV")
    if out_env:
        with open(out_env, "a", encoding="utf-8") as f:
            f.write(f"KSU_VERSION={ksu_version}\n")
            f.write(f"KSU_VERSION_FULL={ksu_version_full}\n")
            f.write(f"SUSFS_VERSION={susfs_ver}\n")
            f.write(f"KERNEL_COMMIT_SHA={head_sha}\n")


if __name__ == "__main__":
    main()
