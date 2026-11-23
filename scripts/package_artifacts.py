#!/usr/bin/env python3
"""校验针对 OS3.0.307.0 编译的 Image (UTS_RELEASE / module_layout CRC / SuSFS v2.4.0)，构建 ksu_susfs_arm64 与模块。"""

import argparse
import glob
import os
import re
import shutil
import subprocess
import zipfile


EXPECTED_BANNER = (
    "Linux version 6.1.138-android14-11-g0c3d559bcd85-ab14529422 "
    "(build-user@build-host) (Android (10087095, +pgo, +bolt, +lto, -mlgo, based on r487747c) "
    "clang version 17.0.2 (https://android.googlesource.com/toolchain/llvm-project "
    "d9f89f4d16663d5012e5c09495f3b30ece3d2362), LLD 17.0.2) "
    "#1 SMP PREEMPT Wed Dec  3 02:05:30 UTC 2025"
)
EXPECTED_MODULE_LAYOUT_CRC_LE = bytes.fromhex("7f9d75ea")  # 0xea759d7f


ANYKERNEL_SH = """### AnyKernel3 Ramdisk Mod Script
## osm0sis @ xda-developers

### AnyKernel setup
properties() { '
kernel.string=__KERNEL_STRING__
do.devicecheck=0
do.modules=0
do.systemless=1
do.cleanup=1
do.cleanuponabort=0
device.name1=houji
device.name2=
device.name3=
device.name4=
device.name5=
supported.versions=14 - 16
supported.patchlevels=
supported.vendorpatchlevels=
'; } # end properties

### AnyKernel install
BLOCK=boot;
IS_SLOT_DEVICE=1;
RAMDISK_COMPRESSION=auto;
PATCH_VBMETA_FLAG=auto;

. tools/ak3-core.sh;

split_boot;
flash_boot;
## end boot install
"""


def run(cmd, cwd=None, env=None):
    print(f"+ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def verify_image(image_path: str, symvers_path: str) -> None:
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Missing Image at {image_path}")
    with open(image_path, "rb") as f:
        data = f.read()
    size_mb = len(data) / (1024 * 1024)
    print(f"Image size: {len(data)} bytes ({size_mb:.2f} MiB)")

    m = re.search(rb"Linux version 6\.1\.138[^\x00\n]+", data)
    if not m:
        raise RuntimeError("Missing 'Linux version 6.1.138...' banner in Image!")
    banner = m.group(0).decode("utf-8", "replace")
    print("Kernel banner:", banner)
    if banner != EXPECTED_BANNER:
        raise RuntimeError(
            f"Banner mismatch!\n  Expected: {EXPECTED_BANNER}\n  Actual:   {banner}"
        )
    print("Verified exact OS3.0.307.0 kernel banner & UTS_RELEASE!")

    for needle in (
        b"6.1.138-android14-11-g0c3d559bcd85-ab14529422 SMP preempt mod_unload modversions aarch64",
        b"susfs is initialized! version: v2.4.0",
        b"CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER",
        b"KernelSU",
        EXPECTED_MODULE_LAYOUT_CRC_LE,
    ):
        if needle not in data:
            raise RuntimeError(f"Expected binary marker {needle!r} not found in Image!")
    print("Verified vermagic, SUSFS v2.4.0, PRIORITY_UID_FILTER, KernelSU, and module_layout CRC (0xea759d7f) in Image.")

    if os.path.isfile(symvers_path):
        with open(symvers_path, "r", encoding="utf-8") as sf:
            for line in sf:
                if "\tmodule_layout\t" in line:
                    print("vmlinux.symvers module_layout:", line.strip())
                    if not line.startswith("0xea759d7f\t"):
                        raise RuntimeError(f"Unexpected module_layout CRC in vmlinux.symvers: {line.strip()}")


def collect_dotconfig(ws: str, out_dir: str) -> None:
    candidates = glob.glob(
        os.path.join(ws, "bazel-bin", "common", "kernel_aarch64_config", "**", ".config"),
        recursive=True,
    )
    if not candidates:
        candidates = glob.glob(
            os.path.join(ws, "out", "**", ".config"),
            recursive=True,
        )
    if candidates:
        dst = os.path.join(out_dir, "kernel_aarch64.config")
        shutil.copy2(candidates[0], dst)
        print(f"Copied .config from {candidates[0]} -> {dst}")
        with open(dst, "r", encoding="utf-8") as f:
            txt = f.read()
        for key in (
            "CONFIG_ARM64_4K_PAGES",
            "CONFIG_LTO_NONE",
            "CONFIG_LTO_CLANG_THIN",
            "CONFIG_CFI_CLANG",
            "CONFIG_TRIM_UNUSED_KSYMS",
            "CONFIG_MODVERSIONS",
            "CONFIG_KSU",
            "CONFIG_KPM",
            "CONFIG_KSU_DEBUG",
            "CONFIG_KSU_SUSFS",
            "CONFIG_KSU_SUSFS_PRIORITY_UID_FILTER",
            "CONFIG_OVERLAY_FS",
        ):
            for line in txt.splitlines():
                if line.startswith(f"{key}=") or line == f"# {key} is not set":
                    print(" ", line)
    else:
        print("Warning: .config not found in bazel-bin/ or out/")


def build_susfs_module(ws: str, out_dir: str) -> None:
    susfs_dir = os.path.join(ws, "upstream", "susfs4ksu")
    ndk_candidates = glob.glob(os.path.join(ws, "prebuilts", "ndk-*", "**", "ndk-build"), recursive=True)
    ndk_build = next((p for p in ndk_candidates if os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if not ndk_build:
        ndk_build = shutil.which("ndk-build")
    if ndk_build:
        print(f"Building ksu_susfs using {ndk_build}")
        jni_parent = os.path.join(susfs_dir, "ksu_susfs")
        for sub in ("libs", "obj"):
            shutil.rmtree(os.path.join(jni_parent, sub), ignore_errors=True)
        run([ndk_build], cwd=jni_parent)
        built_bin = os.path.join(jni_parent, "libs", "arm64-v8a", "ksu_susfs")
        if os.path.isfile(built_bin):
            shutil.copy2(built_bin, os.path.join(susfs_dir, "ksu_module_susfs", "tools", "ksu_susfs_arm64"))
            shutil.copy2(built_bin, os.path.join(out_dir, "ksu_susfs_arm64"))
    else:
        print("Warning: ndk-build not found; keeping upstream prebuilt ksu_susfs_arm64 binary.")
        prebuilt_bin = os.path.join(susfs_dir, "ksu_module_susfs", "tools", "ksu_susfs_arm64")
        if os.path.isfile(prebuilt_bin):
            shutil.copy2(prebuilt_bin, os.path.join(out_dir, "ksu_susfs_arm64"))

    mod_dir = os.path.join(susfs_dir, "ksu_module_susfs")
    zip_out = os.path.join(out_dir, "ksu_module_susfs-v2.4.0.zip")
    with zipfile.ZipFile(zip_out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(mod_dir):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, mod_dir)
                zf.write(full, arc)
    print(f"Created {zip_out}")


def package_anykernel3(ws: str, image_path: str, out_dir: str, kernel_string: str) -> None:
    ak3_src = os.path.join(ws, "upstream", "AnyKernel3")
    ak3_stage = os.path.join(out_dir, "ak3_stage")
    if os.path.exists(ak3_stage):
        shutil.rmtree(ak3_stage)
    shutil.copytree(ak3_src, ak3_stage, ignore=shutil.ignore_patterns(".git", ".github", "ramdisk", "patch", "modules"))

    with open(os.path.join(ak3_stage, "anykernel.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(ANYKERNEL_SH.replace("__KERNEL_STRING__", kernel_string))

    shutil.copy2(image_path, os.path.join(ak3_stage, "Image"))
    ak3_zip = os.path.join(out_dir, "AnyKernel3-houji-OS3.0.307.0-6.1.138-SukiSU-SuSFS-v2.4.0.zip")
    with zipfile.ZipFile(ak3_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(ak3_stage):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.relpath(full, ak3_stage)
                zf.write(full, arc)
    shutil.rmtree(ak3_stage)
    print(f"Created {ak3_zip}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--dist-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--kernel-string", default="houji OS3.0.307.0 6.1.138 SukiSU + SuSFS v2.4.0")
    args = parser.parse_args()

    ws = os.path.abspath(args.workspace)
    dist_dir = os.path.abspath(args.dist_dir)
    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    image_path = os.path.join(dist_dir, "Image")
    symvers_path = os.path.join(dist_dir, "vmlinux.symvers")
    verify_image(image_path, symvers_path)

    for artifact in ("Image", "vmlinux.symvers", "System.map", "modules.builtin", "modules.builtin.modinfo"):
        src = os.path.join(dist_dir, artifact)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(out_dir, artifact))

    collect_dotconfig(ws, out_dir)
    package_anykernel3(ws, image_path, out_dir, args.kernel_string)
    build_susfs_module(ws, out_dir)


if __name__ == "__main__":
    main()
