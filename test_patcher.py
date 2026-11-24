#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh_patcher 的关键不变量测试。

这些断言对应三类真实踩过的事故，改动 dsh_patcher.py 后必须仍然通过：

1. 版本号序：alpha < beta < rc < 正式版，且同版本内 revision 递增。
   曾经把 revision 直接拼在 4 段版本后面，导致 0.2.3-010201 > 0.2.3-9000，
   正式版被永久屏蔽（比原问题更糟）。
2. 旧包可升级性：已有安装的 0.2.3-0102 必须严格小于新产物，否则用户收不到修复。
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
        print("  ✓ " + name)
    else:
        print("  ✗ " + name + (("  → " + detail) if detail else ""))
        failures.append(name)


def vkey(v):
    m = re.match(r"(\d+)\.(\d+)\.(\d+)-(\d+)", v)
    if not m:
        raise AssertionError("无法解析版本: " + v)
    return tuple(int(x) for x in m.groups())


print("[1] 版本号序")
seq = [
    ("dsh-v0.2.3-alpha.2", 0),
    ("dsh-v0.2.3-alpha.2", 1),
    ("dsh-v0.2.3-alpha.2", 2),
    ("dsh-v0.2.3-beta.1", 0),
    ("dsh-v0.2.3-rc.1", 0),
    ("dsh-v0.2.3", 0),
    ("dsh-v0.2.3", 1),
]
vs = [dsh_syno_version(t, revision=r)[0] for t, r in seq]
for (t, r), v in zip(seq, vs):
    print("      {0:22} rev={1} -> {2}".format(t, r, v))
ks = [vkey(v) for v in vs]
check("alpha < beta < rc < 正式版，且 revision 递增", all(ks[i] < ks[i + 1] for i in range(len(ks) - 1)))

print("\n[2] 旧包可升级性")
check("0.2.3-0102（已发布旧版）< 新产物", vkey("0.2.3-0102") < vkey(dsh_syno_version("dsh-v0.2.3-alpha.2")[0]))
check("正式版不被 alpha+rev 越过",
      vkey(dsh_syno_version("dsh-v0.2.3-alpha.2", revision=99)[0]) < vkey(dsh_syno_version("dsh-v0.2.3")[0]))

print("\n[3] revision 越界被夹紧")
check("revision=100 夹到 99", dsh_syno_version("dsh-v0.2.3-alpha.2", revision=100)[0]
      == dsh_syno_version("dsh-v0.2.3-alpha.2", revision=99)[0])

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
