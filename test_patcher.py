#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh_patcher 的关键不变量测试。

这些断言对应几类真实踩过的事故，改动 dsh_patcher.py 后必须仍然通过：

1. 版本号序：alpha < beta < rc < 正式版。build 段固定 4 位，由上游阶段序号决定。
   曾经为支持"同版本重打包"把 build 段放大 100 倍（变成 6 位，如 0.2.3-010200），
   版本号体系失控、Release 命名混乱，已回退为 4 位。
2. 旧包可升级性：已有安装的 0.2.3-0102 必须能被更高版本覆盖。
   注意 alpha.2 与其自身同号，故重打包同一上游版本不会产生新版本号 ——
   这是刻意取舍：改了打包脚本要等上游发新版本才能让用户收到。
3. 补丁锚点：installer / start-stop-status / start.sh 的每处修改都必须命中。
   锚点静默失配会让修复"看起来打了、实际没生效"（端口补丁就这么失效过）。

用法：python3 test_patcher.py
"""

import re
import sys

from dsh_patcher import PatchError, dsh_syno_version, patch_installer_script, patch_start_script

failures = []


def check(name, cond, detail=""):
    if cond:
        print("  [OK] " + name)
    else:
        print("  [FAIL] " + name + (("  -> " + detail) if detail else ""))
        failures.append(name)


def vkey(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)-(\d+)$", v)
    if not m:
        raise AssertionError("无法解析版本: " + v)
    return tuple(int(x) for x in m.groups())


print("[1] build 段固定 4 位，且阶段有序")
seq = [
    "dsh-v0.2.3-alpha.2",
    "dsh-v0.2.3-beta.1",
    "dsh-v0.2.3-rc.1",
    "dsh-v0.2.3",
]
vs = [dsh_syno_version(t)[0] for t in seq]
for t, v in zip(seq, vs):
    print("      {0:22} -> {1}".format(t, v))
    check("  {0} build 段为 4 位".format(t), bool(re.match(r"^\d+\.\d+\.\d+-\d{4}$", v)), v)
ks = [vkey(v) for v in vs]
check("alpha < beta < rc < 正式版", all(ks[i] < ks[i + 1] for i in range(len(ks) - 1)))

print("\n[2] 不得再产出 6 位 build 段")
for t in seq:
    v6 = dsh_syno_version(t, revision=1)[0]
    check("{0} 不受 revision 影响（仍 4 位）".format(t),
          v6 == dsh_syno_version(t)[0] and bool(re.match(r"^\d+\.\d+\.\d+-\d{4}$", v6)), v6)

print("\n[3] 升级方向")
check("0.2.3-0102 < 0.2.3-beta.1（能升级）",
      vkey("0.2.3-0102") < vkey(dsh_syno_version("dsh-v0.2.3-beta.1")[0]))
check("alpha 不会越过正式版",
      vkey(dsh_syno_version("dsh-v0.2.3-alpha.99")[0]) < vkey(dsh_syno_version("dsh-v0.2.3")[0]))

print("\n[4] 补丁锚点必须命中")
# 用真实上游脚本片段做冒烟：锚点缺失时必须抛 PatchError，绝不能静默返回原文
MIN_INSTALLER = '''#!/bin/sh
PACKAGE_NAME="$SYNOPKG_PKGNAME"
PKG_VAR_DIR="/var/packages/${PACKAGE_NAME}/var"
pkg_version_resolved() {
  local s v
  echo "0.2.3-alpha.2"
}
setup_pkg_env() {
  local VER="$(pkg_version_resolved)"
  mkdir -p "${PKG_VAR_DIR}/${VER}"
  chown -R "${PACKAGE_NAME}:system" "${PKG_VAR_DIR}/${VER}" 2>/dev/null || true
  fix_ownership
  link_dsh_cmd
}
apply_wizard_ports() {
  local VER="$(pkg_version_resolved)"
  local PORTS_FILE="${PKG_VAR_DIR}/${VER}/ports"
  mkdir -p "${PKG_VAR_DIR}/${VER}"
}
cleanup_uninstall() {
  local VER="$(pkg_version_resolved)"
  if [ "${wizard_delete_data:-false}" = "true" ]; then
    if [ -n "$VER" ] && [ -d "${PKG_VAR_DIR}/${VER}" ]; then
      rm -rf "${PKG_VAR_DIR}/${VER}" 2>/dev/null
    fi
  else
    echo "[uninstall] 保留数据（wizard_delete_data=${wizard_delete_data:-false}）: ${PKG_VAR_DIR}/${VER}" >&2
  fi
}
preuninst() {
  # ★ 卸载时置标记 → start.sh stop 输出"已卸载"而不是"DSH 未运行"（避免无谓弹窗 ✗）
  DSH_UNINSTALLING=1 "${PACKAGE_SSS}" stop
  cleanup_uninstall
  exit 0
}

postuninst() {
  cleanup_uninstall
  exit 0
}
'''
out = patch_installer_script(MIN_INSTALLER)
check("注入 is_uninstalling()", "is_uninstalling()" in out)
check("注入 migrate_legacy_data()", "migrate_legacy_data()" in out)
check("preuninst 区分升级/卸载", "覆盖升级：跳过卸载清理" in out)
check("cleanup_uninstall 纵深防御",
      "if ! is_uninstalling; then" in out)
check("端口指向持久根目录", 'PORTS_FILE="${PKG_VAR_DIR}/ports"' in out)
check("不再创建版本隔离目录", 'mkdir -p "${PKG_VAR_DIR}/${VER}"' not in out)

# 锚点缺失 → 必须报错
broken = MIN_INSTALLER.replace("preuninst() {", "preuninst_broken() {")
try:
    patch_installer_script(broken)
    check("锚点缺失时抛 PatchError", False, "竟然静默通过了")
except PatchError:
    check("锚点缺失时抛 PatchError", True)

print("\n[5] start.sh 补丁锚点")
MIN_START = '''#!/bin/bash
is_running() {
  [ -f "$PID_FILE" ] || return 1
  local pid
  pid=$(cat "$PID_FILE" 2>/dev/null)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
ensure_tmpfile_writable() {
  local f="$1"
  if : > "$f" 2>/dev/null; then
    return 0
  fi
  echo "[!] 无法写入 $f（请检查该路径及其父目录的权限/属主）" >&2
  return 1
}
cmd_start() {
  ensure_tmpfile_writable "$LOG_FILE" || return 1
  echo "! 端口未就绪，请查看日志: $LOG_FILE"
}
'''
out2 = patch_start_script(MIN_START)
check("PID 身份校验注入", "_pid_is_ours()" in out2)
check("日志轮转函数注入", "ensure_logf_append()" in out2)
check("不再截断日志", 'ensure_tmpfile_writable "$LOG_FILE"' not in out2)
check("失败留痕写入崩溃日志", '>> "${CRASH_FILE}"' in out2)
check("身份校验不依赖 APP_NAME 兜底",
      '"${APP_NAME:-DeepSeekHarness-NAS}"' not in out2)

try:
    patch_start_script(MIN_START.replace("is_running() {", "is_running_x() {"))
    check("start.sh 锚点缺失时抛 PatchError", False, "竟然静默通过了")
except PatchError:
    check("start.sh 锚点缺失时抛 PatchError", True)

print()
if failures:
    print("✗ 失败 {0} 项: {1}".format(len(failures), ", ".join(failures)))
    sys.exit(1)
print("✓ 全部通过")
