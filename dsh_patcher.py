#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeekHarness-NAS 升级配置持久化修复与打包模块
核心修复：
1. 废除原作者在 var/<version>/.dsh 中按版本号隔离数据的反直觉设计。
2. 固化数据目录至持久根目录 var/.dsh，实现跨版本升级永久无缝保留配置与聊天记录。
3. 注入向前无损兼容迁移机制：覆盖安装时自动扫描旧版历史目录 (var/*/.dsh) 并迁移数据。
4. 端口文件统一收敛至 var/ports，避免升级后端口配置丢失。
5. 修正 ports 读取顺序，确保 start-stop-status 优先命中持久化端口文件。
"""

import os
import re
import io
import shutil
import tarfile
import hashlib
import tempfile
import urllib.request
from pathlib import Path
from spk_builder import build_outer_spk_tar

# ---------------------------------------------------------------------------
# 版本号映射：把 semver 预发布标签映射为群晖可比较的「纯数字版本」
# ---------------------------------------------------------------------------
# 背景：群晖 synopkg 的版本比较器按 . 与 - 切段后**逐段数值比较**，遇到字母段会退化
#   为字符串比较，导致 0.2.2-alpha.2 这类标签排序不可控；更致命的是「正式版无后缀」
#   会被判定为「段数更少 = 更小」，于是 rc 版装上后再也收不到正式版更新。
# 方案：固定输出 4 段 major.minor.patch-build，用 build 段编码发布阶段权重，
#   保证「alpha < beta < rc < 正式版」严格单调递增：
#       alpha.X → 100 + X     (0.2.2-alpha.2  → 0.2.2-0102)
#       beta.X  → 200 + X     (0.2.2-beta.1   → 0.2.2-0201)
#       rc.X    → 300 + X     (0.2.1-rc.5     → 0.2.1-0305)
#       正式版  → 9000        (0.2.2           → 0.2.2-9000)
# 兼容性：作者现存安装的 0.2.2（3 段）与我们的 0.2.2-9000（4 段）比较时，
#   前三段相同、我方多一段 → 判定为「更新」，因此可直接覆盖升级、不会提示降级。
_DSH_STAGE_WEIGHT = {"alpha": 100, "beta": 200, "rc": 300}
_DSH_STABLE_WEIGHT = 9000

def dsh_syno_version(release_tag: str, fallback: str = "") -> tuple:
    """
    解析上游 Release tag，返回 (群晖可比较版本, 原始版本展示串)。
    例：'dsh-v0.2.2-alpha.2' -> ('0.2.2-0102', '0.2.2-alpha.2')
        'dsh-v0.2.1-rc.5'   -> ('0.2.1-0305', '0.2.1-rc.5')
        'dsh-v0.2.2'        -> ('0.2.2-9000', '0.2.2')
    解析失败时返回 (fallback, fallback)。
    """
    raw = (release_tag or "").strip()

    m = re.search(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.\-]+))?", raw)
    if not m:
        fb = (fallback or "").strip()
        return (fb, fb)

    major, minor, patch = m.group(1), m.group(2), m.group(3)
    pre = (m.group(4) or "").strip()
    base = f"{major}.{minor}.{patch}"
    display = f"{base}-{pre}" if pre else base

    if not pre:
        return (f"{base}-{_DSH_STABLE_WEIGHT}", display)

    pm = re.match(r"^(alpha|beta|rc)[.\-_]?(\d*)$", pre.lower())
    if pm:
        stage, num = pm.group(1), (pm.group(2) or "1")
        try:
            seq = int(num)
        except ValueError:
            seq = 1
        build = _DSH_STAGE_WEIGHT[stage] + seq
        return (f"{base}-{build:04d}", display)

    digits = re.sub(r"\D", "", pre) or "1"
    return (f"{base}-{int(digits):04d}", display)

# ---------------------------------------------------------------------------
# scripts/installer 修复
# ---------------------------------------------------------------------------

# 迁移函数：把历史版本隔离目录中的数据一次性搬进持久根目录
_MIGRATION_FUNC = r"""
# ── [Fix] 历史版本数据自动迁移 ──────────────────────────────────────────────
# 原作者按 <var>/<version>/.dsh 隔离数据，升级即换目录 → 用户每次升级都要重新配置。
# 本函数在安装/升级时扫描历史版本目录，把最新的一份数据平移到持久根目录 <var>/.dsh。
#
# 三个约束（都是踩过的坑）：
#   1. 迁移完成后必须清理历史目录，否则每升一次级就永久多留一份旧配置；
#   2. 选择来源必须按「版本号」排序，不能按字典序 —— "0.2.2-9" 字典序大于
#      "0.2.2-10"，会从更旧的版本迁移；
#   3. 落一个迁移完成标记，之后即使 .dsh 为空也不再回头翻历史目录，
#      避免把很旧的配置"复活"。
migrate_legacy_data() {
  local TARGET_DSH="${PKG_VAR_DIR}/.dsh"
  local MARKER="${PKG_VAR_DIR}/.dsh-migrated"

  # 已迁移过：直接复用持久目录，不再扫描历史
  if [ -f "$MARKER" ]; then
    return 0
  fi

  # 持久目录已有数据：认作已迁移，打标记后退出
  if [ -d "$TARGET_DSH" ] && [ -n "$(ls -A "$TARGET_DSH" 2>/dev/null)" ]; then
    echo "[installer] 持久目录 $TARGET_DSH 已有数据，保留现有配置（不再扫描历史目录）" >&2
    # 已经迁移过的旧版残留也在这里清理：数据以持久目录为准，
    # 历史目录不再有任何用途，留着只会占空间并让人误以为要重新迁移。
    if [ "${DSH_KEEP_LEGACY:-0}" = "1" ]; then
      echo "[installer] DSH_KEEP_LEGACY=1，按配置保留历史目录" >&2
    else
      local _had=0
      for d in "${PKG_VAR_DIR}"/*/.dsh; do
        [ -d "$d" ] || continue
        _had=1
        rm -rf "$d" 2>/dev/null || true
        rmdir "$(dirname "$d")" 2>/dev/null || true
      done
      [ "$_had" = "1" ] && echo "[installer] 已清理迁移前的历史目录" >&2
    fi
    touch "$MARKER" 2>/dev/null || true
    return 0
  fi

  # 收集所有非空历史 .dsh，按版本号排序取最新
  local _list="" _src=""
  for d in "${PKG_VAR_DIR}"/*/.dsh; do
    [ -d "$d" ] || continue
    [ -n "$(ls -A "$d" 2>/dev/null)" ] || continue
    _list="${_list}$(basename "$(dirname "$d")")	$d
"
  done

  if [ -n "$_list" ]; then
    # 优先用 sort -V（版本感知）；老 busybox 不支持时退回普通 sort
    _src="$(printf '%s' "$_list" | sort -V 2>/dev/null | tail -n 1 | cut -f2)"
    if [ -z "$_src" ]; then
      _src="$(printf '%s' "$_list" | sort 2>/dev/null | tail -n 1 | cut -f2)"
    fi
  fi

  if [ -n "$_src" ] && [ -d "$_src" ]; then
    echo "[installer] 检测到历史版本数据: $_src，正在迁移至持久目录 $TARGET_DSH ..." >&2
    mkdir -p "$TARGET_DSH"
    cp -a "$_src/." "$TARGET_DSH/" 2>/dev/null || true

    # 先校验再清理：复制不完整就保留历史目录，绝不删数据
    local _src_n _dst_n
    _src_n="$(find "$_src" -type f 2>/dev/null | wc -l | tr -d ' ')"
    _dst_n="$(find "$TARGET_DSH" -type f 2>/dev/null | wc -l | tr -d ' ')"
    if [ "${_dst_n:-0}" -lt "${_src_n:-0}" ]; then
      echo "[installer] 迁移校验未通过（源 ${_src_n} / 目标 ${_dst_n} 个文件），保留历史目录" >&2
    else
      echo "[installer] 迁移完成（${_dst_n} 个文件），开始清理历史目录" >&2
      if [ "${DSH_KEEP_LEGACY:-0}" = "1" ]; then
        echo "[installer] DSH_KEEP_LEGACY=1，按配置保留历史目录" >&2
      else
        for d in "${PKG_VAR_DIR}"/*/.dsh; do
          [ -d "$d" ] || continue
          rm -rf "$d" 2>/dev/null || true
          rmdir "$(dirname "$d")" 2>/dev/null || true
        done
      fi
    fi
  fi

  # 端口文件同样收敛到根目录（优先采用历史版本中已有的端口设置）
  if [ ! -f "${PKG_VAR_DIR}/ports" ]; then
    for p in "${PKG_VAR_DIR}"/*/ports; do
      if [ -f "$p" ]; then
        cp -a "$p" "${PKG_VAR_DIR}/ports" 2>/dev/null || true
        echo "[installer] 已迁移历史端口文件: $p -> ${PKG_VAR_DIR}/ports" >&2
        break
      fi
    done
  fi

  touch "$MARKER" 2>/dev/null || true
}
"""

# 原 setup_pkg_env 原样替换（整段精确匹配，不使用正则）
_OLD_SETUP_ENV = '''setup_pkg_env() {
  local VER="$(pkg_version_resolved)"
  mkdir -p "${PKG_VAR_DIR}/${VER}"
  chown -R "${PACKAGE_NAME}:system" "${PKG_VAR_DIR}/${VER}" 2>/dev/null || true
  fix_ownership
  link_dsh_cmd
}'''

# 修复后：不再按版本创建隔离目录，统一使用持久根目录
_NEW_SETUP_ENV = '''setup_pkg_env() {
  # [Fix] 不再按版本号隔离数据目录；统一使用持久根目录 ${PKG_VAR_DIR}/.dsh
  mkdir -p "${PKG_VAR_DIR}/.dsh"
  chown -R "${PACKAGE_NAME}:system" "${PKG_VAR_DIR}" 2>/dev/null || true
  migrate_legacy_data
  fix_ownership
  link_dsh_cmd
}'''

# 卸载清理：改为清理持久根目录
_OLD_CLEANUP = '''    if [ -n "$VER" ] && [ -d "${PKG_VAR_DIR}/${VER}" ]; then
      rm -rf "${PKG_VAR_DIR}/${VER}" 2>/dev/null
    fi'''

_NEW_CLEANUP = '''    if [ -d "${PKG_VAR_DIR}/.dsh" ]; then
      rm -rf "${PKG_VAR_DIR}/.dsh" 2>/dev/null
    fi
    rm -f "${PKG_VAR_DIR}/.dsh-migrated" 2>/dev/null'''

def patch_installer_script(installer_content: str) -> str:
    """修复 scripts/installer：数据目录持久化 + 历史数据迁移 + 端口文件收敛"""
    content = installer_content

    # 1) 注入迁移函数（放在 pkg_version_resolved 之后）
    anchor = '''  echo "0.2.2-alpha.2"
}
'''
    if anchor in content:
        content = content.replace(anchor, anchor + _MIGRATION_FUNC, 1)
    else:
        # 回退：插到文件头部的 PKG_VAR_DIR 定义之后
        marker = 'PKG_VAR_DIR="/var/packages/${PACKAGE_NAME}/var"\n'
        if marker in content:
            content = content.replace(marker, marker + _MIGRATION_FUNC, 1)
        else:
            content = _MIGRATION_FUNC + "\n" + content

    # 2) setup_pkg_env 精确替换
    if _OLD_SETUP_ENV in content:
        content = content.replace(_OLD_SETUP_ENV, _NEW_SETUP_ENV, 1)
    else:
        print("    [WARN] 未匹配到 setup_pkg_env 原段，数据目录持久化可能未生效")

    # 3) 端口文件收敛到根目录
    old_ports = 'local PORTS_FILE="${PKG_VAR_DIR}/${VER}/ports"'
    if old_ports in content:
        content = content.replace(old_ports, 'local PORTS_FILE="${PKG_VAR_DIR}/ports"')

    old_mkdir = 'mkdir -p "${PKG_VAR_DIR}/${VER}"'
    if old_mkdir in content:
        content = content.replace(old_mkdir, 'mkdir -p "${PKG_VAR_DIR}"')

    # 4) 卸载清理改为清理持久根目录
    if _OLD_CLEANUP in content:
        content = content.replace(_OLD_CLEANUP, _NEW_CLEANUP, 1)

    # 5) 卸载保留数据的日志也指向持久目录（否则提示的是旧版隔离路径）
    content = content.replace(
        'echo "[uninstall] 保留数据（wizard_delete_data=${wizard_delete_data:-false}）: ${PKG_VAR_DIR}/${VER}" >&2',
        'echo "[uninstall] 保留数据（wizard_delete_data=${wizard_delete_data:-false}）: ${PKG_VAR_DIR}/.dsh" >&2'
    )

    return content

# ---------------------------------------------------------------------------
# start.sh 修复
# ---------------------------------------------------------------------------

def patch_start_script(start_content: str) -> str:
    """修复 start.sh：将 DSH_HOME 绑定至持久根目录，移除 /$PKG_VERSION 分隔"""
    content = start_content
    # 替换各种 PKGVAR 分支中的 /$PKG_VERSION
    content = content.replace('DSH_HOME_PARENT="$TRIM_PKGVAR/$PKG_VERSION"', 'DSH_HOME_PARENT="$TRIM_PKGVAR"')
    content = content.replace('DSH_HOME_PARENT="$SYNOPKG_PKGVAR/$PKG_VERSION"', 'DSH_HOME_PARENT="$SYNOPKG_PKGVAR"')
    content = content.replace('DSH_HOME_PARENT="$PKG_VAR/$PKG_VERSION"', 'DSH_HOME_PARENT="$PKG_VAR"')
    content = content.replace('DSH_HOME_PARENT="$SYNODATA/$PKG_VERSION"', 'DSH_HOME_PARENT="$SYNODATA"')

    # 无外部变量时按版本目录探测数据根的分支：改为直接探测根目录
    content = content.replace(
        'for _d in "/var/packages/${_APP_NAME}/var" /volume*/@appdata/${_APP_NAME}; do\n    [ -d "$_d/$PKG_VERSION" ] && { SYNODATA="$_d"; break; }\n  done',
        'for _d in "/var/packages/${_APP_NAME}/var" /volume*/@appdata/${_APP_NAME}; do\n    [ -d "$_d" ] && { SYNODATA="$_d"; break; }\n  done'
    )

    # 探测到数据根后不再追加版本号
    content = content.replace('DSH_HOME_PARENT="$SYNODATA/$PKG_VERSION"', 'DSH_HOME_PARENT="$SYNODATA"')
    return content

def patch_start_stop_status(sss_content: str) -> str:
    """修复 start-stop-status：端口文件优先读持久根目录，DSH_HOME 与 installer 保持一致"""
    content = sss_content

    # 端口文件：优先 <var>/ports，其次回退打包默认
    old = 'for _pf in "${SYNOPKG_PKGVAR}"/*/ports "${SYNOPKG_PKGVAR}/ports"; do'
    new = 'for _pf in "${SYNOPKG_PKGVAR}/ports" "${SYNOPKG_PKGVAR}"/*/ports; do'
    if old in content:
        content = content.replace(old, new, 1)

    # DSH_HOME 必须与 installer 的持久目录一致（否则新装的目录读不到旧配置）
    old_home = 'export DSH_HOME="${PACKAGE_BASE}/.dsh-home/.dsh"'
    new_home = 'export DSH_HOME="${SYNOPKG_PKGVAR}/.dsh"'
    if old_home in content:
        content = content.replace(old_home, new_home, 1)

    return content

def patch_and_repack_dsh_spk(
    original_spk_path: str,
    output_spk_path: str,
    distributor: str = "onelxzy",
    distributor_url: str = "https://github.com/onelxzy",
    version_override: str = None,
    display_label: str = None
) -> str:
    """
    解包原版 SPK -> 修复配置持久化与历史迁移逻辑 -> 重打包生成兼容升级的 SPK
    version_override: 写入 INFO 的群晖可比较版本（4 段纯数字版），保证小版本可连续升级
    display_label:    追加到 displayname 的展示标签（如 0.2.2-alpha.2），便于商店辨识
    返回新 SPK 的 MD5 哈希
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_spk_path)), exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dsh_spk_") as work_dir:
        spk_extract_dir = Path(work_dir) / "spk_root"
        pkg_extract_dir = Path(work_dir) / "pkg_root"
        spk_extract_dir.mkdir(parents=True, exist_ok=True)
        pkg_extract_dir.mkdir(parents=True, exist_ok=True)

        print("[+] 正在解压原版 DeepSeekHarness-NAS SPK 容器...")
        with tarfile.open(original_spk_path, "r:*") as spk_tar:
            spk_tar.extractall(spk_extract_dir)

        orig_pkg_tgz = spk_extract_dir / "package.tgz"
        if not orig_pkg_tgz.exists():
            raise FileNotFoundError("原版 SPK 中未找到 package.tgz 文件！")

        # 1. 修复 scripts/installer
        installer_file = spk_extract_dir / "scripts" / "installer"
        if installer_file.exists():
            print("[+] 正在修复 scripts/installer (数据目录持久化 + 历史数据迁移)...")
            with open(installer_file, "r", encoding="utf-8", errors="ignore") as f:
                raw_installer = f.read()
            patched_installer = patch_installer_script(raw_installer)
            with open(installer_file, "w", encoding="utf-8") as f:
                f.write(patched_installer)
            installer_file.chmod(0o755)

        # 2. 修复 scripts/start-stop-status
        sss_file = spk_extract_dir / "scripts" / "start-stop-status"
        if sss_file.exists():
            print("[+] 正在修复 scripts/start-stop-status (端口与 DSH_HOME 对齐持久目录)...")
            with open(sss_file, "r", encoding="utf-8", errors="ignore") as f:
                raw_sss = f.read()
            patched_sss = patch_start_stop_status(raw_sss)
            with open(sss_file, "w", encoding="utf-8") as f:
                f.write(patched_sss)
            sss_file.chmod(0o755)

        # 3. 解包 package.tgz 并修复 start.sh
        print("[+] 正在解压 package.tgz 以修复核心启动脚本 start.sh ...")
        with tarfile.open(orig_pkg_tgz, "r:*") as pkg_tar:
            pkg_tar.extractall(pkg_extract_dir)

        start_sh_file = pkg_extract_dir / "start.sh"
        if start_sh_file.exists():
            print("[+] 正在修复 start.sh (解除 DSH_HOME 版本隔离，永久绑定 var/.dsh)...")
            with open(start_sh_file, "r", encoding="utf-8", errors="ignore") as f:
                raw_start = f.read()
            patched_start = patch_start_script(raw_start)
            with open(start_sh_file, "w", encoding="utf-8") as f:
                f.write(patched_start)
            start_sh_file.chmod(0o755)

        # 4. 重新打包 package.tgz (保持 gzip 格式与权限)
        print("[+] 正在重新压缩封装 package.tgz ...")
        orig_pkg_tgz.unlink()

        def pkg_filter(tarinfo):
            return tarinfo

        with tarfile.open(orig_pkg_tgz, "w:gz", format=tarfile.PAX_FORMAT) as pkg_tar:
            for item in sorted(os.listdir(pkg_extract_dir)):
                pkg_tar.add(pkg_extract_dir / item, arcname=item, filter=pkg_filter)

        pkg_md5 = hashlib.md5()
        with open(orig_pkg_tgz, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                pkg_md5.update(chunk)
        new_checksum = pkg_md5.hexdigest().lower()

        # 5. 更新 INFO 元数据
        info_path = spk_extract_dir / "INFO"
        if info_path.exists():
            with open(info_path, "r", encoding="utf-8", errors="ignore") as f:
                info_lines = f.readlines()

            new_lines = []
            has_checksum = False
            for line in info_lines:
                if line.startswith("checksum="):
                    new_lines.append(f'checksum="{new_checksum}"\n')
                    has_checksum = True
                elif line.startswith("version=") and version_override:
                    # 用 4 段纯数字版本覆盖作者原始的 3 段版本，保证群晖可识别小版本升级
                    new_lines.append(f'version="{version_override}"\n')
                elif line.startswith("displayname=") and display_label:
                    new_lines.append(f'displayname="DeepSeek Harness ({display_label})"\n')
                elif line.startswith("distributor="):
                    new_lines.append(f'distributor="{distributor}"\n')
                elif line.startswith("distributor_url="):
                    new_lines.append(f'distributor_url="{distributor_url}"\n')
                elif line.startswith("description="):
                    new_lines.append('description="DeepSeek Harness (DSH) 原生群晖打包，已修复跨版本升级配置持久化保留。"\n')
                else:
                    new_lines.append(line)

            if not has_checksum:
                new_lines.append(f'checksum="{new_checksum}"\n')

            with open(info_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

        # 6. 重新打包为最终 SPK
        #
        # ★ 必须是 GNU/USTAR 普通 tar，且第一个成员是 INFO。
        #   用 PAX 格式时 tar 开头会写出 "././@PaxHeader" 扩展头，
        #   群晖安装器读到首条不是 INFO，直接报「套件格式不正确」。
        #   PAX 头只在成员元数据放不进 ustar 时才生成（例如 mtime 带小数秒），
        #   所以同一个打包器有时正常、有时报错，非常难查。
        print(f"[+] 正在组装并构建最终 SPK: {os.path.basename(output_spk_path)} ...")
        build_outer_spk_tar(spk_extract_dir, output_spk_path)

    spk_md5 = hashlib.md5()
    with open(output_spk_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            spk_md5.update(chunk)

    final_md5 = spk_md5.hexdigest().lower()
    print(f"[√] DeepSeekHarness-NAS 修复版 SPK 封包完成! 最终 MD5: {final_md5}")
    return final_md5
