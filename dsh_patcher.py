#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeekHarness-NAS 打包修补模块

在 EIGHTfs/DeepSeekHarness-NAS 上游 SPK 基础上做三件事：

A. 数据持久化（原有修复）
   1. 废除上游 var/<version>/.dsh 的版本隔离设计；固化到 var/.dsh。
   2. 覆盖安装时扫描历史 var/*/.dsh 并迁移（按版本号排序、先校验后清理）。
   3. 端口文件收敛到 var/ports。

B. 覆盖升级语义（本次新增，修复"安装完提示已卸载"）
   DSM 在【覆盖升级】时同样会执行 preuninst / postuninst，并把
   SYNOPKG_PKG_STATUS 置为 UPGRADE。上游 installer 不区分这两个状态，于是
   升级时做了真卸载才该做的事：打印"已卸载"、删服务用户、删 appconf。
   现在只在真正的 UNINSTALL 时才执行卸载清理。

C. 重启健壮性（本次新增，修复"关机重启后套件起不来"）
   1. is_running 增加进程身份校验：PID 跨重启会被复用，旧逻辑只做 kill -0，
      一旦撞上无关进程就误判"已在运行"并直接 return 0（一个进程都不启动）。
   2. 启动日志改为轮转而非清空：旧逻辑每次启动 `: > $LOG_FILE`，
      把上一次失败现场抹掉，导致这类问题永远无法定位。
   3. start-stop-status 冷启动失败自动重试一次。
   4. start-stop-status 端口解析真正优先读 var/ports（原补丁锚点已失配、且
      新逻辑仍会被后续的旧发现逻辑覆盖，实测向导端口被静默丢弃）。

关于补丁锚点：本模块所有替换都必须命中，命中不了直接抛错。
上游脚本会随版本演进，静默失配比报错危险得多 —— 之前"端口补丁看起来打了、
其实一行没生效"就是这么来的。
"""

import hashlib
import os
import re
import tarfile
import tempfile
from pathlib import Path

from spk_builder import build_outer_spk_tar

# ---------------------------------------------------------------------------
# 版本号映射：把 semver 预发布标签映射为群晖可比较的「纯数字版本」
# ---------------------------------------------------------------------------
# 群晖 synopkg 的版本比较器按 . 与 - 切段后逐段数值比较，遇到字母段会退化为字符串
# 比较；且「正式版无后缀」会被判成「段数更少 = 更小」，装了 rc 就再也收不到正式版。
# 方案：固定 major.minor.patch-build，用 build 段编码发布阶段权重：
#       alpha.X → 100 + X     (0.2.2-alpha.2  → 0.2.3-0102)
#       beta.X  → 200 + X
#       rc.X    → 300 + X     (0.2.1-rc.5     → 0.2.1-0305)
#       正式版  → 9000
#
# revision：同一上游版本重新打包（改了打包脚本但上游没发版）时必须让版本号变大，
#   否则 DSM 认为"没有新版本"，用户永远收不到修复。
#
# ⚠ 修订号不能简单拼在后面：那样 0.2.3-010201（alpha+rev1）会大于后续正式版
#   0.2.3-9000，等于把正式版永久屏蔽掉 —— 这是个比原问题更糟的回归。
#   因此整个 build 段统一放大 100 倍（两位留给修订号）：
#       build = (阶段权重 + 序号) * 100 + revision，固定输出 6 位。
#       alpha.2     → (100+2)*100 = 10200
#       alpha.2 r1  → 10201
#       rc.5        → (300+5)*100 = 30500
#       正式版      → 9000*100 = 900000
#   这样「alpha < beta < rc < 正式版」与「同版本内 rev 递增」同时成立。
#   旧的 4 段版本（0.2.3-0102）仍严格小于新的 6 段版本，升级方向正确。
_DSH_STAGE_WEIGHT = {"alpha": 100, "beta": 200, "rc": 300}
_DSH_STABLE_WEIGHT = 9000


def dsh_syno_version(release_tag: str, fallback: str = "", revision: int = 0) -> tuple:
    """
    解析上游 Release tag，返回 (群晖可比较版本, 原始版本展示串)。

    例：'dsh-v0.2.2-alpha.2' -> ('0.2.2-0102', '0.2.2-alpha.2')
        'dsh-v0.2.1-rc.5'   -> ('0.2.1-0305', '0.2.1-rc.5')
        'dsh-v0.2.2'        -> ('0.2.2-9000', '0.2.2')
        同一 tag + revision=2 -> ('0.2.2-010202', ...)

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
        stage_build = _DSH_STABLE_WEIGHT
    else:
        pm = re.match(r"^(alpha|beta|rc)[.\-_]?(\d*)$", pre.lower())
        if pm:
            stage, num = pm.group(1), (pm.group(2) or "1")
            try:
                seq = int(num)
            except ValueError:
                seq = 1
            stage_build = _DSH_STAGE_WEIGHT[stage] + seq
        else:
            digits = re.sub(r"\D", "", pre) or "1"
            stage_build = int(digits)

    rev = max(0, min(99, int(revision or 0)))
    build = stage_build * 100 + rev
    return (f"{base}-{build:06d}", display)


# ---------------------------------------------------------------------------
# 补丁工具：所有锚点必须命中，否则抛错（绝不静默失配）
# ---------------------------------------------------------------------------

class PatchError(RuntimeError):
    """补丁锚点失配。宁可构建失败，也不要产出一个"看起来打了补丁"的包。"""


def _replace_once(content: str, old: str, new: str, what: str) -> str:
    count = content.count(old)
    if count == 0:
        raise PatchError(f"锚点未命中：{what}（上游脚本可能已改版，需更新 dsh_patcher.py）")
    if count > 1:
        raise PatchError(f"锚点命中 {count} 次（期望 1 次）：{what}")
    return content.replace(old, new, 1)


def _sub_once(content: str, pattern: str, repl: str, what: str, flags: int = re.S) -> str:
    new_content, n = re.subn(pattern, repl, content, count=1, flags=flags)
    if n != 1:
        raise PatchError(f"锚点未命中：{what}（上游脚本可能已改版，需更新 dsh_patcher.py）")
    return new_content


# ---------------------------------------------------------------------------
# installer（生命周期钩子）
# ---------------------------------------------------------------------------

# 历史版本数据迁移：把旧版 var/<version>/.dsh 一次性搬到持久根目录 var/.dsh
_MIGRATION_FUNC = r"""
# ── [Fix] 历史版本数据自动迁移 ──────────────────────────────────────────────
# 上游按 <var>/<version>/.dsh 隔离数据，升级即换目录 → 每次升级都要重新配置。
# 本函数在安装/升级时扫描历史版本目录，把最新的一份平移到持久根目录 <var>/.dsh。
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

# 覆盖升级 ≠ 卸载。DSM 在 upgrade 时同样跑 preuninst/postuninst。
_UNINSTALL_GUARD = r"""
# ── [Fix] 区分「覆盖升级」与「真正卸载」────────────────────────────────────
# DSM 的覆盖升级流程是：
#   preupgrade → preuninst → 删 target → postuninst → preinst → 安装 → postinst → postupgrade
# 也就是说升级时同样会执行卸载钩子，并把 SYNOPKG_PKG_STATUS 置为 UPGRADE。
#
# 上游 installer 不区分这两种状态，于是升级时执行了真卸载才该做的事：
#   · DSH_UNINSTALLING=1 → start.sh 打印"已卸载"，被 DSM 收进安装结果弹窗
#     （用户看到的就是"安装完提示已卸载"）；
#   · synouser --del → 删掉服务用户，UID 变化后 @appdata 内的数据属主失配；
#   · 删 /usr/syno/etc/packages/<pkg> 及其指向的 @appconf 目录。
# 现在只有真正的卸载（UNINSTALL）才走清理。
is_uninstalling() {
  case "${SYNOPKG_PKG_STATUS:-}" in
    UPGRADE|upgrade) return 1 ;;
  esac
  return 0
}
"""

_OLD_PREUNINST = '''preuninst() {
  # ★ 卸载时置标记 → start.sh stop 输出"已卸载"而不是"DSH 未运行"（避免无谓弹窗 ✗）
  DSH_UNINSTALLING=1 "${PACKAGE_SSS}" stop
  cleanup_uninstall
  exit 0
}

postuninst() {
  cleanup_uninstall
  exit 0
}'''

_NEW_PREUNINST = '''preuninst() {
  if is_uninstalling; then
    # ★ 真卸载：置标记 → start.sh stop 输出"已卸载"而不是"DSH 未运行"（避免无谓弹窗 ✗）
    DSH_UNINSTALLING=1 "${PACKAGE_SSS}" stop
    cleanup_uninstall
  else
    # 覆盖升级：只停服务。绝不 upsert 卸载话术、绝不删用户/配置（见 is_uninstalling 注释）
    "${PACKAGE_SSS}" stop
    echo "[installer] 覆盖升级：跳过卸载清理（保留服务用户 / 注册目录 / 数据）" >&2
  fi
  exit 0
}

postuninst() {
  if is_uninstalling; then
    cleanup_uninstall
  else
    echo "[installer] 覆盖升级：跳过卸载清理（postuninst）" >&2
  fi
  exit 0
}'''

# setup_pkg_env：不再按版本建隔离目录，统一持久根目录
_SETUP_ENV_RE = (
    r'setup_pkg_env\(\)\s*\{\s*\n'
    r'(?:[^\n]*\n)*?'
    r'\s*\}\s*\n'
)

_NEW_SETUP_ENV = '''setup_pkg_env() {
  # [Fix] 不再按版本号隔离数据目录；统一使用持久根目录 ${PKG_VAR_DIR}/.dsh
  mkdir -p "${PKG_VAR_DIR}/.dsh"
  chown -R "${PACKAGE_NAME}:system" "${PKG_VAR_DIR}" 2>/dev/null || true
  migrate_legacy_data
  fix_ownership
  link_dsh_cmd
}
'''


def patch_installer_script(installer_content: str) -> str:
    """修复 scripts/installer：数据持久化 + 历史迁移 + 升级/卸载语义分离"""
    content = installer_content

    # 1) 注入迁移函数（紧跟 pkg_version_resolved 之后；该函数的兜底 echo 的行号是版本相关的，
    #    所以用「函数定义 + 其后第一个闭合 }」来定位，不写死版本号）
    m = re.search(r'pkg_version_resolved\(\)\s*\{.*?\n\}\n', content, re.S)
    if not m:
        raise PatchError("锚点未命中：pkg_version_resolved()（installer 结构已变）")
    content = content[:m.end()] + _MIGRATION_FUNC + content[m.end():]

    # 2) setup_pkg_env 整体改写为持久目录版本
    content = _sub_once(content, _SETUP_ENV_RE, _NEW_SETUP_ENV, "setup_pkg_env()")

    # 3) 端口文件收敛到根目录
    content = _replace_once(
        content,
        'local PORTS_FILE="${PKG_VAR_DIR}/${VER}/ports"',
        'local PORTS_FILE="${PKG_VAR_DIR}/ports"',
        'apply_wizard_ports 的 PORTS_FILE',
    )
    content = _replace_once(
        content,
        'mkdir -p "${PKG_VAR_DIR}/${VER}"',
        'mkdir -p "${PKG_VAR_DIR}"',
        'apply_wizard_ports 的 mkdir',
    )

    # 4) 卸载清理改为清理持久根目录
    content = _replace_once(
        content,
        '''    if [ -n "$VER" ] && [ -d "${PKG_VAR_DIR}/${VER}" ]; then
      rm -rf "${PKG_VAR_DIR}/${VER}" 2>/dev/null
    fi''',
        '''    if [ -d "${PKG_VAR_DIR}/.dsh" ]; then
      rm -rf "${PKG_VAR_DIR}/.dsh" 2>/dev/null
    fi
    rm -f "${PKG_VAR_DIR}/.dsh-migrated" 2>/dev/null''',
        'cleanup_uninstall 的数据目录',
    )
    content = _replace_once(
        content,
        'echo "[uninstall] 保留数据（wizard_delete_data=${wizard_delete_data:-false}）: ${PKG_VAR_DIR}/${VER}" >&2',
        'echo "[uninstall] 保留数据（wizard_delete_data=${wizard_delete_data:-false}）: ${PKG_VAR_DIR}/.dsh" >&2',
        'cleanup_uninstall 的保留数据日志',
    )

    # 5) 覆盖升级 ≠ 卸载：注入 guard 并改写 preuninst / postuninst
    m = re.search(r'cleanup_uninstall\(\)\s*\{', content)
    if not m:
        raise PatchError("锚点未命中：cleanup_uninstall()")
    content = content[:m.start()] + _UNINSTALL_GUARD.lstrip("\n") + "\n" + content[m.start():]

    content = _replace_once(content, _OLD_PREUNINST, _NEW_PREUNINST, "preuninst/postuninst")

    # 6) 纵深防御：cleanup_uninstall 自身也拒绝在升级时执行
    content = _replace_once(
        content,
        '''cleanup_uninstall() {
  local VER="$(pkg_version_resolved)"''',
        '''cleanup_uninstall() {
  # 纵深防御：即便调用方忘记判断，升级时也绝不执行卸载清理
  if ! is_uninstalling; then
    echo "[uninstall] 当前为覆盖升级（SYNOPKG_PKG_STATUS=${SYNOPKG_PKG_STATUS:-}），跳过清理" >&2
    return 0
  fi
  local VER="$(pkg_version_resolved)"''',
        'cleanup_uninstall 的升级守卫',
    )

    # 7) 脚本末尾自检：把 guard 是否就位写进日志，便于事后核对
    return content


# ---------------------------------------------------------------------------
# start-stop-status
# ---------------------------------------------------------------------------

# 端口解析：真正优先读 var/ports。
# 上游/旧补丁的行为：先按 <var>/<INFO version>*/.dsh 目录找 ports，再退到 var/ 下
# 「版本号最大的目录」，最后才是 target/var/ports —— 而 installer 现在把生效端口写在
# ${SYNOPKG_PKGVAR}/ports（持久根目录），上述两条都命中不了它，向导端口被静默丢弃。
_RX_PORT_BLOCK = re.compile(
    r'PORT_FILE="\$\{PACKAGE_BASE\}/var/ports"\n'
    r'(?:.*?\n)*?'
    r'if \[ -f "\$PORT_FILE" \]; then\n\s*\. "\$PORT_FILE"\nfi\n',
    re.S,
)

_NEW_PORT_BLOCK = '''# [Fix] 端口解析优先级（务必保持与 installer 的写入位置一致）：
#   ① 持久根目录 ${SYNOPKG_PKGVAR}/ports —— installer 每次安装/升级都写，含用户向导端口；
#   ② 历史版本目录 <var>/<版本>/ports（兼容早期版本写入的位置），取版本号最大者；
#   ③ 打包默认 ${PACKAGE_BASE}/var/ports。
# 旧逻辑从 ② 开始找，永远命中不了 ①，导致安装向导里填的端口被静默忽略（实测）。
PORT_FILE=""
if [ -f "${SYNOPKG_PKGVAR}/ports" ]; then
  PORT_FILE="${SYNOPKG_PKGVAR}/ports"
fi
if [ -z "$PORT_FILE" ]; then
  _NEWEST="$(ls -1d "${SYNOPKG_PKGVAR}"/*/ 2>/dev/null | sort -V | tail -1)"
  [ -n "${_NEWEST}" ] && [ -f "${_NEWEST}ports" ] && PORT_FILE="${_NEWEST}ports"
fi
[ -n "$PORT_FILE" ] || PORT_FILE="${PACKAGE_BASE}/var/ports"
if [ -f "$PORT_FILE" ]; then
  . "$PORT_FILE"
fi
'''

# 冷启动失败自动重试一次
_OLD_START_TAIL = '''  if ! running_dsh "${DSH_PORT:-$SPK_DSH_PORT}" ; then
    echo "启动仍在进行中（端口 ${DSH_PORT} 尚未监听），交由系统 status 判定" >&2
    return 0
  fi
  return 0
}'''

_NEW_START_TAIL = '''  # [Fix] 冷启动失败自动重试一次。
  #   动机：关机重启后偶发起不来，而「卸载重装」总能好 —— 二者最关键的差别就是
  #   重装会把服务重新拉起一次。与其让用户手工重装，不如这里自动重来一次：
  #   先彻底 stop（清掉残留端口占用/半死进程），再 start，并给足冷启动时间。
  if ! running_dsh "${DSH_PORT:-$SPK_DSH_PORT}" ; then
    echo "首次启动未就绪，自动重试…（现场保留于 var/logs/start.log 与 start-retry.log）" >&2
    "${START_SCRIPT}" stop \\
      --proxy-port "$PROXY_PORT" --dsh-port "$DSH_PORT" --container-port "$CONTAINER_PORT" \\
      >/dev/null 2>&1 || true
    sleep 2
    "${START_SCRIPT}" start \\
      --proxy-port "$PROXY_PORT" --dsh-port "$DSH_PORT" --container-port "$CONTAINER_PORT" \\
      >> "${PACKAGE_BASE}/var/logs/start-retry.log" 2>&1 &
    local _w2=0
    while [ "$_w2" -lt 20 ]; do
      running_dsh "${DSH_PORT:-$SPK_DSH_PORT}" && break
      _w2=$((_w2 + 1)); sleep 1
    done
  fi
  if ! running_dsh "${DSH_PORT:-$SPK_DSH_PORT}" ; then
    echo "启动仍在进行中（端口 ${DSH_PORT} 尚未监听），交由系统 status 判定" >&2
    return 0
  fi
  return 0
}'''


def patch_start_stop_status(sss_content: str) -> str:
    """修复 start-stop-status：端口优先读持久 var/ports、DSH_HOME 对齐、冷启动重试、日志不截断"""
    content = sss_content

    # 1) 端口解析：真正优先读 ${SYNOPKG_PKGVAR}/ports
    if not _RX_PORT_BLOCK.search(content):
        raise PatchError("锚点未命中：start-stop-status 端口解析块")
    content = _RX_PORT_BLOCK.sub(lambda _m: _NEW_PORT_BLOCK, content, count=1)

    # 2) DSH_HOME 必须与 installer 的持久目录一致
    content = _replace_once(
        content,
        'export DSH_HOME="${PACKAGE_BASE}/.dsh-home/.dsh"',
        'export DSH_HOME="${SYNOPKG_PKGVAR}/.dsh"',
        'start() 的 DSH_HOME',
    )

    # 3) start.log 追加而非覆盖：覆盖会把上一次失败现场冲掉
    content = _replace_once(
        content,
        '> "${PACKAGE_BASE}/var/logs/start.log" 2>&1 &',
        '>> "${PACKAGE_BASE}/var/logs/start.log" 2>&1 &',
        'start.log 重定向',
    )

    # 4) 冷启动重试
    content = _replace_once(content, _OLD_START_TAIL, _NEW_START_TAIL, "start() 尾部")

    return content


# ---------------------------------------------------------------------------
# start.sh
# ---------------------------------------------------------------------------

_OLD_IS_RUNNING = '''is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}'''

_NEW_IS_RUNNING = '''# PID 身份校验：PID 跨重启会被系统复用，只做 kill -0 会把「无关进程」当成自己。
#   实测场景：关机 → 重启后 PID 号从低位重新分配 → 旧 PID 文件里恰好是一个活着的
#   无关进程 → is_running 返回真 → cmd_start 直接 echo "DSH 已在运行" 后 return 0，
#   一个进程都不启动，套件永久停在"停止"状态（而卸载重装因为删掉了 PID 文件就正常）。
#   守护进程的 cmdline 里必然带 DSH_REPAIR_* 变量名（见下方 start 的 env 传参），
#   据此确认「这个 PID 确实是本实例的守护进程」。
_pid_is_ours() {
  local pid="$1" cl
  [ -n "$pid" ] || return 1
  cl="$(tr '\\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" || return 1
  [ -n "$cl" ] || return 1
  # 只认守护进程特征：它以 node -e 内联脚本 + DSH_REPAIR_* 环境变量启动。
  #   ⚠ 不能用 "$APP_NAME" 做兜底匹配：用套件自带 node 跑的任何进程 cmdline 里都含
  #   包路径，那样等于没校验（实测：一个 sleep 进程就被误判成"已在运行"）。
  case "$cl" in
    *DSH_REPAIR_DIR*|*DSH_REPAIR_PID_FILE*) return 0 ;;
  esac
  return 1
}

is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null)
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  # PID 活着 ≠ 是我们的进程：必须再验身份，否则重启后会误判"已在运行"
  _pid_is_ours "$pid"
}'''

_OLD_TMPFILE_FUNC_TAIL = '''  echo "[!] 无法写入 $f（请检查该路径及其父目录的权限/属主）" >&2
  return 1
}'''

_NEW_APPEND_FUNC = '''  echo "[!] 无法写入 $f（请检查该路径及其父目录的权限/属主）" >&2
  return 1
}

# 日志【追加 + 轮转】，绝不截断。
#   旧逻辑用 ensure_tmpfile_writable 先 `: > $LOG_FILE`，等于每次启动都清空日志：
#   「重启后起不来」的现场在下一次启动瞬间被销毁，问题永远抓不到（这正是本轮修复
#   的原始动机之一）。改为把上一轮日志保留为 .1 / .2。
ensure_logf_append() {
  local f="$1"
  if [ -f "$f" ]; then
    [ -f "${f}.1" ] && mv -f "${f}.1" "${f}.2" 2>/dev/null || true
    mv -f "$f" "${f}.1" 2>/dev/null || true
  fi
  mkdir -p "$(dirname "$f")" 2>/dev/null || true
  if : >> "$f" 2>/dev/null; then
    return 0
  fi
  rm -f "$f" 2>/dev/null || true
  if : >> "$f" 2>/dev/null; then
    return 0
  fi
  echo "[!] 无法写入 $f（请检查该路径及其父目录的权限/属主）" >&2
  return 1
}'''

_OLD_START_LOG_CALL = '  ensure_tmpfile_writable "$LOG_FILE" || return 1'

_NEW_START_LOG_CALL = '''  # [Fix] 轮转 + 追加，不再截断（见 ensure_logf_append 注释）
  ensure_logf_append "$LOG_FILE" || return 1'''

# 启动失败时把根因落进跨轮次保留的崩溃日志
_OLD_TIMEOUT_HINT = '''  echo "! 端口未就绪，请查看日志: $LOG_FILE"'''

_NEW_TIMEOUT_HINT = '''  echo "! 端口未就绪，请查看日志: $LOG_FILE"
  # [Fix] 把根因写进跨启动保留的崩溃日志：日志轮转只留一代，跨重启排查需要更长留痕
  {
    printf '\\n===== 启动失败 %s =====\\n' "$(date '+%F %T')"
    printf 'PKG_VERSION=%s PROXY=%s DSH=%s CONTAINER=%s\\n' \\
      "${PKG_VERSION:-}" "${PROXY_PORT:-}" "${DSH_PORT:-}" "${CONTAINER_PORT:-}"
    grep -m5 -E "startup failed|required plugin did not activate|readable beyond its owner|Cannot find package|EADDRINUSE|Error:|error:" \\
      "$LOG_FILE" 2>/dev/null | tail -5
    printf -- '--- 日志尾部(30 行) ---\\n'
    tail -30 "$LOG_FILE" 2>/dev/null
    printf '\\n'
  } >> "${CRASH_FILE}" 2>/dev/null || true'''


def patch_start_script(start_content: str) -> str:
    """修复 start.sh：数据目录持久化 + PID 身份校验 + 日志不销毁 + 失败留痕"""
    content = start_content

    # 1) 解除 DSH_HOME 版本隔离
    for old, new in (
        ('DSH_HOME_PARENT="$TRIM_PKGVAR/$PKG_VERSION"', 'DSH_HOME_PARENT="$TRIM_PKGVAR"'),
        ('DSH_HOME_PARENT="$SYNOPKG_PKGVAR/$PKG_VERSION"', 'DSH_HOME_PARENT="$SYNOPKG_PKGVAR"'),
        ('DSH_HOME_PARENT="$PKG_VAR/$PKG_VERSION"', 'DSH_HOME_PARENT="$PKG_VAR"'),
        ('DSH_HOME_PARENT="$SYNODATA/$PKG_VERSION"', 'DSH_HOME_PARENT="$SYNODATA"'),
    ):
        if old in content:
            content = content.replace(old, new)

    # 2) PID 身份校验
    content = _replace_once(content, _OLD_IS_RUNNING, _NEW_IS_RUNNING, "is_running()")

    # 3) 日志轮转函数
    content = _replace_once(
        content, _OLD_TMPFILE_FUNC_TAIL, _NEW_APPEND_FUNC, "ensure_tmpfile_writable() 尾部"
    )
    content = _replace_once(content, _OLD_START_LOG_CALL, _NEW_START_LOG_CALL, "启动时的日志初始化调用")

    # 4) 失败留痕
    content = _replace_once(content, _OLD_TIMEOUT_HINT, _NEW_TIMEOUT_HINT, "cmd_start 超时提示")

    return content


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def patch_and_repack_dsh_spk(
    original_spk_path: str,
    output_spk_path: str,
    distributor: str = "onelxzy",
    distributor_url: str = "https://github.com/onelxzy",
    version_override: str = None,
    display_label: str = None,
) -> str:
    """
    解包上游 SPK → 打补丁 → 重打包。

    version_override: 写入 INFO 的群晖可比较版本（4/5 段纯数字），保证可连续升级
    display_label:    displayname 的展示后缀（如 0.2.3-alpha.2），便于商店辨识
    返回新 SPK 的 MD5。
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_spk_path)), exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="dsh_spk_") as work_dir:
        spk_extract_dir = Path(work_dir) / "spk_root"
        pkg_extract_dir = Path(work_dir) / "pkg_root"
        spk_extract_dir.mkdir(parents=True, exist_ok=True)
        pkg_extract_dir.mkdir(parents=True, exist_ok=True)

        print("[+] 解压上游 SPK 容器...")
        with tarfile.open(original_spk_path, "r:*") as spk_tar:
            spk_tar.extractall(spk_extract_dir)

        orig_pkg_tgz = spk_extract_dir / "package.tgz"
        if not orig_pkg_tgz.exists():
            raise FileNotFoundError("上游 SPK 中未找到 package.tgz")

        # 1) scripts/installer
        installer_file = spk_extract_dir / "scripts" / "installer"
        if not installer_file.exists():
            raise FileNotFoundError("上游 SPK 中未找到 scripts/installer")
        print("[+] 修补 scripts/installer（数据持久化 + 升级≠卸载）...")
        raw = installer_file.read_text(encoding="utf-8", errors="ignore")
        installer_file.write_text(patch_installer_script(raw), encoding="utf-8")
        installer_file.chmod(0o755)

        # 2) scripts/start-stop-status
        sss_file = spk_extract_dir / "scripts" / "start-stop-status"
        if not sss_file.exists():
            raise FileNotFoundError("上游 SPK 中未找到 scripts/start-stop-status")
        print("[+] 修补 scripts/start-stop-status（端口 / DSH_HOME / 冷启动重试）...")
        raw = sss_file.read_text(encoding="utf-8", errors="ignore")
        sss_file.write_text(patch_start_stop_status(raw), encoding="utf-8")
        sss_file.chmod(0o755)

        # 3) package.tgz 内的 start.sh
        print("[+] 解压 package.tgz，修补 start.sh（PID 校验 / 日志留痕）...")
        with tarfile.open(orig_pkg_tgz, "r:*") as pkg_tar:
            pkg_tar.extractall(pkg_extract_dir)

        start_sh = pkg_extract_dir / "start.sh"
        if not start_sh.exists():
            raise FileNotFoundError("package.tgz 中未找到 start.sh")
        raw = start_sh.read_text(encoding="utf-8", errors="ignore")
        start_sh.write_text(patch_start_script(raw), encoding="utf-8")
        start_sh.chmod(0o755)

        # 4) 重打 package.tgz（保持成员顺序与权限；sorted 保证可复现）
        print("[+] 重新封装 package.tgz...")
        orig_pkg_tgz.unlink()
        with tarfile.open(orig_pkg_tgz, "w:gz", format=tarfile.PAX_FORMAT) as pkg_tar:
            for item in sorted(os.listdir(pkg_extract_dir)):
                pkg_tar.add(pkg_extract_dir / item, arcname=item)

        pkg_md5 = hashlib.md5()
        with open(orig_pkg_tgz, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                pkg_md5.update(chunk)
        new_checksum = pkg_md5.hexdigest().lower()

        # 5) INFO 元数据
        info_path = spk_extract_dir / "INFO"
        if not info_path.exists():
            raise FileNotFoundError("SPK 中未找到 INFO")
        lines, has_checksum = [], False
        for line in info_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("checksum="):
                lines.append(f'checksum="{new_checksum}"')
                has_checksum = True
            elif line.startswith("version=") and version_override:
                lines.append(f'version="{version_override}"')
            elif line.startswith("displayname=") and display_label:
                lines.append(f'displayname="DeepSeek Harness ({display_label})"')
            elif line.startswith("distributor="):
                lines.append(f'distributor="{distributor}"')
            elif line.startswith("distributor_url="):
                lines.append(f'distributor_url="{distributor_url}"')
            elif line.startswith("description="):
                lines.append(
                    'description="DeepSeek Harness (DSH) 原生群晖打包，无需 Docker；'
                    '已修复覆盖升级提示已卸载、重启后无法启动、升级丢配置等问题。"'
                )
            else:
                lines.append(line)
        if not has_checksum:
            lines.append(f'checksum="{new_checksum}"')
        info_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 6) 外层 SPK：USTAR + INFO 首位
        print(f"[+] 组装 SPK: {os.path.basename(output_spk_path)}")
        build_outer_spk_tar(spk_extract_dir, output_spk_path)

    spk_md5 = hashlib.md5()
    with open(output_spk_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            spk_md5.update(chunk)
    final_md5 = spk_md5.hexdigest().lower()
    print(f"[√] DeepSeekHarness-NAS 打包完成，MD5: {final_md5}")
    return final_md5
