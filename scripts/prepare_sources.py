#!/usr/bin/env python3
"""将最新 SukiSU (builtin) 与 SuSFS v2.4.0 (gki-android14-6.1) 集成至小米 14 OS3.0.307.0 原厂基线 (0c3d559bcd85)。"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import urllib.request


STOCK_LOCALVERSION = "-android14-11-g0c3d559bcd85-ab14529422"
STOCK_UTS_VERSION = "#1 SMP PREEMPT Wed Dec  3 02:05:30 UTC 2025"
STOCK_COMPILE_BY = "build-user"
STOCK_COMPILE_HOST = "build-host"

# 保持与手机已安装的 SukiSU-Ultra v4.3.0-r2 (com.sukisu.ultra, 41150) 及 SukiSU-Nova v3.0.0 双向兼容
KSU_VERSION_BASE = 40000
KSU_VERSION_OFFSET = 2815
KSU_PACKAGE_NAME = "com.sukisu.ultra"


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
    """在 Bazel 沙箱外计算 KSU_VERSION 与 KSU_VERSION_FULL。"""
    short_sha = run(["git", "rev-parse", "--short=8", "HEAD"], cwd=ksu_dir, capture=True).stdout.strip()

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
        commit_count = int(res.stdout.strip()) if res.returncode == 0 else 4005

    ksu_version = KSU_VERSION_BASE + commit_count - KSU_VERSION_OFFSET
    ksu_version_full = f"v4.3.0-{short_sha}@{branch}"
    return ksu_version, ksu_version_full


def wire_kernelsu_kbuild(common_dir: str, ksu_dir: str, ksu_version: int, ksu_version_full: str) -> None:
    dst_ksu = os.path.join(common_dir, "drivers", "kernelsu")
    if os.path.exists(dst_ksu):
        shutil.rmtree(dst_ksu)
    shutil.copytree(os.path.join(ksu_dir, "kernel"), dst_ksu, symlinks=False)

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
    pin_lines = (
        f"KSU_VERSION := {ksu_version}\n"
        f"KSU_VERSION_FULL := {ksu_version_full}\n"
        f"KSU_PACKAGE_NAME := {KSU_PACKAGE_NAME}\n"
        "$(info -- $(REPO_NAME) pinned version: $(KSU_VERSION) [$(KSU_VERSION_FULL)] pkg=$(KSU_PACKAGE_NAME))\n"
    )
    ksu_mk_txt = ksu_mk_txt.replace(
        "ccflags-y += -DKSU_VERSION=$(KSU_VERSION)",
        pin_lines + "ccflags-y += -DKSU_VERSION=$(KSU_VERSION)",
        1,
    )
    with open(ksu_mk, "w", encoding="utf-8", newline="\n") as f:
        f.write(ksu_mk_txt)


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

    run(["git", "apply", "--check", tmp_patch], cwd=common_dir)
    run(["git", "apply", tmp_patch], cwd=common_dir)

    susfs_h = os.path.join(common_dir, "include", "linux", "susfs.h")
    with open(susfs_h, "r", encoding="utf-8") as f:
        m = re.search(r'^#define\s+SUSFS_VERSION\s+"([^"]+)"', f.read(), re.M)
    ver = m.group(1) if m else "unknown"
    print(f"Integrated SUSFS version: {ver}")
    return ver


def lock_stock_uts_and_timestamp(common_dir: str) -> None:
    """强制锁定 UTS_RELEASE 与 UTS_VERSION，确保与 OS3.0.307.0 /system_dlkm/lib/modules/ 目录及 vermagic 100% 匹配。"""
    # 1. Replace scripts/setlocalversion
    setlocal_path = os.path.join(common_dir, "scripts", "setlocalversion")
    setlocal_script = f"""#!/bin/sh
if [ "$1" = "--save-scmversion" ]; then
    printf '%s\\n' "{STOCK_LOCALVERSION}" > .scmversion
    exit 0
fi
printf '%s\\n' "{STOCK_LOCALVERSION}"
"""
    with open(setlocal_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(setlocal_script)
    os.chmod(setlocal_path, 0o755)

    with open(os.path.join(common_dir, ".scmversion"), "w", encoding="utf-8", newline="\n") as f:
        f.write(STOCK_LOCALVERSION + "\n")

    # 2. Lock LINUX_COMPILE_BY / LINUX_COMPILE_HOST in scripts/mkcompile_h
    mkcompile_path = os.path.join(common_dir, "scripts", "mkcompile_h")
    with open(mkcompile_path, "r", encoding="utf-8") as f:
        mkc = f.read()
    override_block = (
        f'\nLINUX_COMPILE_BY="{STOCK_COMPILE_BY}"\n'
        f'LINUX_COMPILE_HOST="{STOCK_COMPILE_HOST}"\n'
    )
    if "cat <<EOF" not in mkc:
        raise RuntimeError("scripts/mkcompile_h missing 'cat <<EOF'")
    mkc = mkc.replace("cat <<EOF", override_block + "cat <<EOF", 1)
    with open(mkcompile_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(mkc)

    # 3. Lock UTS_VERSION in init/Makefile
    init_mk_path = os.path.join(common_dir, "init", "Makefile")
    with open(init_mk_path, "r", encoding="utf-8") as f:
        imk = f.read()
    old_filechk = (
        'filechk_uts_version = \\\n'
        '\tutsver=$$(echo \'$(pound)\'"$(build-version)" $(smp-flag-y) $(preempt-flag-y) "$(build-timestamp)" | cut -b -64); \\\n'
        '\techo \'$(pound)\'define UTS_VERSION \\""$${utsver}"\\"'
    )
    new_filechk = (
        'filechk_uts_version = \\\n'
        f'\techo \'$(pound)\'define UTS_VERSION \\"\'$(pound)\'1 SMP PREEMPT Wed Dec  3 02:05:30 UTC 2025\\"'
    )
    if old_filechk not in imk:
        raise RuntimeError("init/Makefile filechk_uts_version block did not match expected template")
    imk = imk.replace(old_filechk, new_filechk, 1)
    with open(init_mk_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(imk)
    print(f"Locked UTS_RELEASE suffix={STOCK_LOCALVERSION} and UTS_VERSION={STOCK_UTS_VERSION}")


def configure_kernel(common_dir: str) -> None:
    defconfig = os.path.join(common_dir, "arch", "arm64", "configs", "gki_defconfig")
    with open(defconfig, "r", encoding="utf-8") as f:
        cfg = f.read()

    additions = [
        "",
        "# SukiSU (builtin) + SuSFS v2.4.0 for houji OS3.0.307.0",
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

    bc_gki = os.path.join(common_dir, "build.config.gki")
    with open(bc_gki, "r", encoding="utf-8") as f:
        bc_txt = f.read()
    bc_txt = re.sub(r'^POST_DEFCONFIG_CMDS=.*$', 'POST_DEFCONFIG_CMDS=""', bc_txt, flags=re.M)
    with open(bc_gki, "w", encoding="utf-8", newline="\n") as f:
        f.write(bc_txt)

    for pattern in ("abi_gki_protected_exports*", "gki_*_protected_modules"):
        for path in glob.glob(os.path.join(common_dir, "android", pattern)):
            print(f"Clearing {os.path.relpath(path, common_dir)}")
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("")


def commit_clean_tree(common_dir: str, message: str) -> str:
    run(["git", "add", "-f", "drivers/kernelsu", ".scmversion"], cwd=common_dir)
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
    print(f"Resolved SukiSU: {ksu_version} ({ksu_version_full})")

    wire_kernelsu_kbuild(common_dir, ksu_dir, ksu_version, ksu_version_full)
    susfs_ver = apply_susfs(common_dir, susfs_dir)
    lock_stock_uts_and_timestamp(common_dir)
    configure_kernel(common_dir)
    head_sha = commit_clean_tree(
        common_dir,
        f"houji OS3.0.307.0: integrate SukiSU {ksu_version_full} and SUSFS {susfs_ver}",
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
