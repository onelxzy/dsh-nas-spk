#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synology SPK 打包器 (纯 Python 标准库实现)
严格遵循群晖 DSM 6 / DSM 7 官方 SPK 打包与目录树规范：
1. 保证 package.tgz 与顶层 SPK 均显式包含完整的目录项 (DIRTYPE, mode=0o755)
2. 规范控制脚本与配置文件权限 (0o755 / 0o644)
3. 严格清洗 INFO 字段（单行化 changelog，杜绝折行与双引号破坏解析器）
4. 精确对齐官方群晖规范成员编排顺序与 checksum 计算
"""

import os
import io
import shutil
import tarfile
import hashlib
import tempfile
import time
from pathlib import Path

TRANSPARENT_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

# 群晖 SPK 外层归档规范：
#   1. 必须是 POSIX ustar（magic "ustar\0 00"），GNU 格式（"ustar  \0"）会被判
#      「平台不兼容」；
#   2. 第一个成员必须是 INFO；
#   3. mtime 用整数，小数秒会引入 PAX 扩展头。
SPK_TAR_EPOCH = int(os.environ.get("SOURCE_DATE_EPOCH", "1767225600"))


def _normalize_spk_member(ti):
    """把成员元数据压到 ustar 能容纳的范围，避免产生 PAX 扩展头。"""
    ti.uid = 0
    ti.gid = 0
    ti.uname = "root"
    ti.gname = "root"
    ti.mtime = int(SPK_TAR_EPOCH)   # 取整：小数秒会触发 PAX 头
    ti.pax_headers = {}             # 清空显式扩展属性
    return ti


def build_outer_spk_tar(src_dir, output_spk_path):
    """
    把解包目录组装成符合群晖规范的外层 SPK tar。
    - USTAR_FORMAT：与群晖官方生态一致，不使用 PAX / GNU 扩展
    - INFO 强制排在第一个成员
    """
    src = Path(src_dir)
    items = sorted(os.listdir(src))
    # INFO 必须在首位；其余按名字排序保证可重现
    ordered = (["INFO"] if "INFO" in items else []) + [i for i in items if i != "INFO"]
    with tarfile.open(str(output_spk_path), "w", format=tarfile.USTAR_FORMAT) as tar:
        for item in ordered:
            tar.add(str(src / item), arcname=item, filter=_normalize_spk_member)


def create_spk(
    output_spk_path: str,
    package_id: str,
    version: str,
    display_name: str,
    description: str,
    description_chs: str,
    maintainer: str,
    maintainer_url: str,
    distributor: str,
    distributor_url: str,
    arch: str,
    binary_content: bytes,
    binary_filename: str,
    dsm_ver: str = "7.1-42661",
    changelog: str = "",
    template_dir: str = None,
    binary_dest: str = None,
    extra_items: list = None
) -> str:
    """
    生成 .spk 包并返回最终文件的 MD5 校验和
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_spk_path)), exist_ok=True)
    # ★ 可重现构建：时间戳必须固定，不能取当前时间。
    #   否则同一版本每次打包 MD5 都不同：
    #   1) 每天定时任务会把它误判为「有新内容」，反复重打包、反复覆盖同名资产；
    #   2) 线上资产被覆盖后，已提交的 packages.json 仍留着旧哈希，
    #      DSM 下载完成校验 MD5 失败 -> 安装失败。
    #   固定为 SOURCE_DATE_EPOCH（符合可重现构建惯例）或常量 2026-01-01。
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH", "1767225600"))

    safe_version = version.lstrip("v")
    if "-rc." in safe_version:
        safe_version = safe_version.replace("-rc.", "-")

    # 规范化单行 changelog，严禁 raw newlines 导致 DSM 解析器报 4528 平台不兼容
    clean_changelog = (changelog or f"更新至最新版本 {version}").replace("\r", "").replace("\n", " ").replace('"', "'").strip()

    # -------------------------------------------------------------
    # 1. 构建 package.tgz
    # -------------------------------------------------------------
    pkg_tgz_buffer = io.BytesIO()
    with tarfile.open(fileobj=pkg_tgz_buffer, mode="w:gz") as pkg_tar:
        added_dirs = set()

        def add_dir_entry(dir_name: str):
            if not dir_name or dir_name == "." or dir_name in added_dirs:
                return
            # 递归确保父目录先行创建
            parent = os.path.dirname(dir_name)
            if parent and parent != "." and parent not in added_dirs:
                add_dir_entry(parent)
            d_info = tarfile.TarInfo(name=dir_name)
            d_info.type = tarfile.DIRTYPE
            d_info.mode = 0o755
            d_info.mtime = mtime
            pkg_tar.addfile(d_info)
            added_dirs.add(dir_name)

        # 如果有模板目录且存在 target 文件夹，加入 target 下的内容 (保持完整目录树与权限)
        if template_dir:
            tmpl_target = Path(template_dir) / "target"
            if tmpl_target.exists():
                for root, dirs, files in os.walk(tmpl_target):
                    rel_dir = Path(root).relative_to(tmpl_target).as_posix()
                    if rel_dir != ".":
                        add_dir_entry(rel_dir)
                    for d in dirs:
                        add_dir_entry((Path(rel_dir) / d).as_posix() if rel_dir != "." else d)
                    for f in files:
                        full_p = Path(root) / f
                        rel_p = full_p.relative_to(tmpl_target).as_posix()
                        t_info = tarfile.TarInfo(name=rel_p)
                        t_info.size = full_p.stat().st_size
                        # 遵循群晖套件习惯，可执行脚本与主程序保持 0o755
                        t_info.mode = 0o755
                        t_info.mtime = mtime
                        with open(full_p, "rb") as fp:
                            pkg_tar.addfile(t_info, fp)

        # 添加额外目录/文件到 target (如 web-vault/)
        if extra_items:
            for item_name, item_path in extra_items:
                item_path = Path(item_path)
                if item_path.is_file():
                    parent_dir = os.path.dirname(item_name)
                    if parent_dir:
                        add_dir_entry(parent_dir)
                    t_info = tarfile.TarInfo(name=item_name)
                    t_info.size = item_path.stat().st_size
                    t_info.mode = 0o755
                    t_info.mtime = mtime
                    with open(item_path, "rb") as fp:
                        pkg_tar.addfile(t_info, fp)
                elif item_path.is_dir():
                    add_dir_entry(item_name)
                    for root, dirs, files in os.walk(item_path):
                        cur_rel = Path(root).relative_to(item_path).as_posix()
                        base_d = item_name if cur_rel == "." else f"{item_name}/{cur_rel}"
                        add_dir_entry(base_d)
                        for d in dirs:
                            add_dir_entry(f"{base_d}/{d}")
                        for f in files:
                            full_p = Path(root) / f
                            rel_p = f"{base_d}/{f}"
                            t_info = tarfile.TarInfo(name=rel_p)
                            t_info.size = full_p.stat().st_size
                            t_info.mode = 0o755 if (full_p.stat().st_mode & 0o111) else 0o644
                            t_info.mtime = mtime
                            with open(full_p, "rb") as fp:
                                pkg_tar.addfile(t_info, fp)

        # 添加目标主二进制到 target
        target_bin_rel = binary_dest or f"bin/{binary_filename}"
        bin_parent = os.path.dirname(target_bin_rel)
        if bin_parent:
            add_dir_entry(bin_parent)
        bin_info = tarfile.TarInfo(name=target_bin_rel)
        bin_info.size = len(binary_content)
        bin_info.mode = 0o755
        bin_info.mtime = mtime
        pkg_tar.addfile(bin_info, io.BytesIO(binary_content))

    pkg_tgz_bytes = pkg_tgz_buffer.getvalue()
    pkg_checksum = hashlib.md5(pkg_tgz_bytes).hexdigest().lower()

    # -------------------------------------------------------------
    # 2. 构建 INFO 文件
    # -------------------------------------------------------------
    info_rendered = None
    if template_dir:
        tmpl_info_file = Path(template_dir) / "INFO.template"
        if tmpl_info_file.exists():
            with open(tmpl_info_file, "r", encoding="utf-8") as f:
                info_raw = f.read()
            info_rendered = info_raw.format(
                version=safe_version,
                description=description,
                description_chs=description_chs or description,
                arch=arch,
                maintainer=maintainer,
                maintainer_url=maintainer_url,
                distributor=distributor,
                distributor_url=distributor_url,
                os_min_ver=dsm_ver,
                displayname=display_name,
                changelog=clean_changelog,
                checksum=pkg_checksum
            )

    if not info_rendered:
        info_rendered = f"""package="{package_id}"
version="{safe_version}"
os_min_ver="{dsm_ver}"
description="{description}"
description_chs="{description_chs or description}"
arch="{arch}"
maintainer="{maintainer}"
maintainer_url="{maintainer_url}"
distributor="{distributor}"
distributor_url="{distributor_url}"
displayname="{display_name}"
silent_install="yes"
silent_uninstall="yes"
silent_upgrade="yes"
startable="yes"
changelog="{clean_changelog}"
checksum="{pkg_checksum}"
""".strip()

    info_bytes = (info_rendered.strip() + "\n").encode("utf-8")

    # -------------------------------------------------------------
    # 3. 组装顶层 SPK 归档 (严格对齐群晖官方标准排列与权限)
    # -------------------------------------------------------------
    with tarfile.open(output_spk_path, mode="w", format=tarfile.USTAR_FORMAT) as spk_tar:
        def add_spk_dir(dir_name: str):
            d_info = tarfile.TarInfo(name=dir_name)
            d_info.type = tarfile.DIRTYPE
            d_info.mode = 0o755
            d_info.mtime = mtime
            spk_tar.addfile(d_info)

        if template_dir:
            t_dir = Path(template_dir)

            # 1. INFO 必须排在首位，权限必须为 0o644 (DSM 官方规范要求极高)
            t_info = tarfile.TarInfo(name="INFO")
            t_info.size = len(info_bytes)
            t_info.mode = 0o644
            t_info.mtime = mtime
            spk_tar.addfile(t_info, io.BytesIO(info_bytes))

            # 2. package.tgz 排在第二位
            t_pkg = tarfile.TarInfo(name="package.tgz")
            t_pkg.size = len(pkg_tgz_bytes)
            t_pkg.mode = 0o644
            t_pkg.mtime = mtime
            spk_tar.addfile(t_pkg, io.BytesIO(pkg_tgz_bytes))

            # 3. conf/ (配置文件应为 0o644)
            if (t_dir / "conf").exists():
                add_spk_dir("conf")
                for f in sorted((t_dir / "conf").glob("*")):
                    if f.is_file():
                        t_item = tarfile.TarInfo(name=f"conf/{f.name}")
                        t_item.size = f.stat().st_size
                        t_item.mode = 0o644
                        t_item.mtime = mtime
                        with open(f, "rb") as fp:
                            spk_tar.addfile(t_item, fp)

            # 4. scripts/ (脚本必须为 0o755)
            if (t_dir / "scripts").exists():
                add_spk_dir("scripts")
                for f in sorted((t_dir / "scripts").glob("*")):
                    if f.is_file():
                        t_item = tarfile.TarInfo(name=f"scripts/{f.name}")
                        t_item.size = f.stat().st_size
                        t_item.mode = 0o755
                        t_item.mtime = mtime
                        with open(f, "rb") as fp:
                            spk_tar.addfile(t_item, fp)

            # 5. WIZARD_UIFILES/
            if (t_dir / "WIZARD_UIFILES").exists():
                add_spk_dir("WIZARD_UIFILES")
                for f in sorted((t_dir / "WIZARD_UIFILES").glob("*")):
                    if f.is_file():
                        t_item = tarfile.TarInfo(name=f"WIZARD_UIFILES/{f.name}")
                        t_item.size = f.stat().st_size
                        t_item.mode = 0o644
                        t_item.mtime = mtime
                        with open(f, "rb") as fp:
                            spk_tar.addfile(t_item, fp)

            # 6. 图标
            for icon_name in ["PACKAGE_ICON.PNG", "PACKAGE_ICON_256.PNG"]:
                icon_path = t_dir / icon_name
                if icon_path.exists():
                    t_icon = tarfile.TarInfo(name=icon_name)
                    t_icon.size = icon_path.stat().st_size
                    t_icon.mode = 0o644
                    t_icon.mtime = mtime
                    with open(icon_path, "rb") as fp:
                        spk_tar.addfile(t_icon, fp)

            # 7. 屏幕截图资源 (如有)
            for sc in sorted(t_dir.glob("*_screen_*.png")):
                t_sc = tarfile.TarInfo(name=sc.name)
                t_sc.size = sc.stat().st_size
                t_sc.mode = 0o644
                t_sc.mtime = mtime
                with open(sc, "rb") as fp:
                    spk_tar.addfile(t_sc, fp)

        else:
            # 默认最小骨架
            #
            # ★ INFO 必须是外层 tar 的第一个成员。群晖安装器从头顺序读取，
            #   首条不是 INFO 会直接报「套件格式不正确」。
            t_info = tarfile.TarInfo(name="INFO")
            t_info.size = len(info_bytes)
            t_info.mode = 0o644
            t_info.mtime = mtime
            spk_tar.addfile(t_info, io.BytesIO(info_bytes))

            add_spk_dir("conf")
            privilege_json = f'{{"defaults":{{"run-as":"package"}},"username":"{package_id}","groupname":"{package_id}"}}'.encode("utf-8")
            t_priv = tarfile.TarInfo(name="conf/privilege")
            t_priv.size = len(privilege_json)
            t_priv.mode = 0o755
            t_priv.mtime = mtime
            spk_tar.addfile(t_priv, io.BytesIO(privilege_json))

            add_spk_dir("scripts")
            start_stop_script = f"""#!/bin/sh
DAEMON="/var/packages/{package_id}/target/bin/{binary_filename}"
PIDFILE="/var/packages/{package_id}/target/var/{package_id}.pid"
mkdir -p /var/packages/{package_id}/target/var
case "$1" in
    start)
        nohup "$DAEMON" >/dev/null 2>&1 &
        echo $! > "$PIDFILE"
        exit 0
        ;;
    stop)
        [ -f "$PIDFILE" ] && kill $(cat "$PIDFILE") 2>/dev/null && rm -f "$PIDFILE"
        exit 0
        ;;
    status)
        [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null && exit 0
        exit 1
        ;;
    *) exit 1 ;;
esac""".encode("utf-8")
            t_script = tarfile.TarInfo(name="scripts/start-stop-status")
            t_script.size = len(start_stop_script)
            t_script.mode = 0o755
            t_script.mtime = mtime
            spk_tar.addfile(t_script, io.BytesIO(start_stop_script))

            for icon_name in ["PACKAGE_ICON_256.PNG", "PACKAGE_ICON.PNG"]:
                t_icon = tarfile.TarInfo(name=icon_name)
                t_icon.size = len(TRANSPARENT_PNG_BYTES)
                t_icon.mode = 0o755
                t_icon.mtime = mtime
                spk_tar.addfile(t_icon, io.BytesIO(TRANSPARENT_PNG_BYTES))

            t_pkg = tarfile.TarInfo(name="package.tgz")
            t_pkg.size = len(pkg_tgz_bytes)
            t_pkg.mode = 0o644
            t_pkg.mtime = mtime
            spk_tar.addfile(t_pkg, io.BytesIO(pkg_tgz_bytes))

    # 计算最终生成的 SPK 文件的 MD5 哈希
    md5_hash = hashlib.md5()
    with open(output_spk_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            md5_hash.update(chunk)

    return md5_hash.hexdigest().lower()
