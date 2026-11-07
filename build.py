#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SPK 自动构建脚本。

读取同目录下的 app.json，跟随上游仓库最新 Release，
把上游产物封装成群晖 DSM 7 可安装的 .spk，并输出构建清单。
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from spk_builder import create_spk

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
TEMPLATE = ROOT / "template"
API = "https://api.github.com"


def token():
    return os.environ.get("GITHUB_TOKEN", "").strip()


def get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "spk-builder",
        "Accept": "application/vnd.github+json",
    })
    if token():
        req.add_header("Authorization", "Bearer " + token())
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "spk-builder"})
    if token():
        req.add_header("Authorization", "Bearer " + token())
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=900) as resp, open(dest, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)
    return Path(dest)


def md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def syno_version(tag):
    """上游 tag 转成群晖可比较版本号，例如 1.0.0-rc.48 -> 1.0.0-48。"""
    return (tag or "").strip().lstrip("v").replace("-rc.", "-")


def match_asset(assets, pattern):
    rx = re.compile(pattern)
    for asset in assets:
        if rx.search(asset.get("name", "")):
            return asset
    return None


def read_member(path, wanted):
    """从 tar / zip 归档中取出指定文件名，裸文件则直接读取。"""
    try:
        with tarfile.open(path, "r:*") as tar:
            for member in tar.getmembers():
                if Path(member.name).name == wanted and member.isfile():
                    return tar.extractfile(member).read()
    except tarfile.TarError:
        pass
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if Path(name).name == wanted:
                    return zf.read(name)
    except zipfile.BadZipFile:
        pass
    return Path(path).read_bytes()


def unpack_extra(archive, wanted_dir, workdir):
    """把归档完整解开，返回其中指定的子目录。"""
    target = Path(workdir) / "extract"
    target.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as tar:
            try:
                tar.extractall(target, filter="data")
            except TypeError:
                tar.extractall(target)
    except tarfile.TarError:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(target)
    for path in sorted(target.rglob(wanted_dir)):
        if path.is_dir():
            return path
    raise FileNotFoundError("归档中未找到目录: " + wanted_dir)


def spk_name(cfg, version, family):
    return "{0}_{1}_{2}.spk".format(cfg["package_id"], version, "armv8" if family == "arm" else "x86_64")


def already_published(cfg, version):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return False
    tag = "{0}-v{1}".format(cfg["package_id"], version)
    try:
        rel = get_json("{0}/repos/{1}/releases/tags/{2}".format(API, repo, tag))
    except Exception:
        return False
    names = {a.get("name") for a in rel.get("assets", [])}
    return all(spk_name(cfg, version, a["family"]) in names for a in cfg["architectures"])


def build_one(cfg, arch_cfg, version, display_version, tag, rel_assets, workdir):
    family = arch_cfg["family"]
    info_arch = "armv8" if family == "arm" else "x86_64"
    out_path = DIST / spk_name(cfg, version, family)
    changelog = cfg["changelog"].format(version=version, tag=tag)

    if cfg["type"] == "dsh_patch":
        from dsh_patcher import patch_and_repack_dsh_spk

        asset = match_asset(rel_assets, arch_cfg["asset_pattern"])
        if not asset:
            raise RuntimeError("未找到匹配的 Release 资产: " + arch_cfg["asset_pattern"])
        src = download(asset["browser_download_url"], Path(workdir) / asset["name"])
        return patch_and_repack_dsh_spk(
            original_spk_path=str(src),
            output_spk_path=str(out_path),
            distributor=cfg["distributor"],
            distributor_url=cfg["distributor_url"],
            version_override=version,
            display_label=display_version,
        )

    if cfg["type"] == "vaultwarden":
        import vaultwarden_artifacts as va

        upstream_version = (tag or "").lstrip("v")
        binary = va.fetch_binary_from_image(upstream_version, arch_cfg["docker_arch"])
        web_dir = va.fetch_web_vault(upstream_version, arch_cfg["docker_arch"], Path(workdir))
        return create_spk(
            output_spk_path=str(out_path),
            package_id=cfg["package_id"],
            version=version,
            display_name=cfg["display_name"],
            description=cfg["description"],
            description_chs=cfg["description_chs"],
            maintainer=cfg["maintainer"],
            maintainer_url=cfg["maintainer_url"],
            distributor=cfg["distributor"],
            distributor_url=cfg["distributor_url"],
            arch=info_arch,
            binary_content=binary,
            binary_filename="vaultwarden",
            dsm_ver=cfg["os_min_ver"],
            changelog=changelog,
            template_dir=str(TEMPLATE),
            binary_dest="vaultwarden",
            extra_items=[("web-vault", str(web_dir))],
        )

    asset = match_asset(rel_assets, arch_cfg["asset_pattern"])
    if not asset:
        raise RuntimeError("未找到匹配的 Release 资产: " + arch_cfg["asset_pattern"])
    src = download(asset["browser_download_url"], Path(workdir) / asset["name"])
    binary = read_member(src, arch_cfg["binary_path"])
    if not binary:
        raise RuntimeError("归档中未找到二进制: " + arch_cfg["binary_path"])

    extra_items = []
    for extra in arch_cfg.get("extra_dirs", []):
        extra_items.append((extra, str(unpack_extra(src, extra, workdir))))

    return create_spk(
        output_spk_path=str(out_path),
        package_id=cfg["package_id"],
        version=version,
        display_name=cfg["display_name"],
        description=cfg["description"],
        description_chs=cfg["description_chs"],
        maintainer=cfg["maintainer"],
        maintainer_url=cfg["maintainer_url"],
        distributor=cfg["distributor"],
        distributor_url=cfg["distributor_url"],
        arch=info_arch,
        binary_content=binary,
        binary_filename=arch_cfg["binary_path"],
        dsm_ver=cfg["os_min_ver"],
        changelog=changelog,
        template_dir=str(TEMPLATE),
        binary_dest=arch_cfg.get("binary_dest"),
        extra_items=extra_items,
    )


def main():
    cfg = json.loads((ROOT / "app.json").read_text(encoding="utf-8"))
    rel = get_json("{0}/repos/{1}/releases/latest".format(API, cfg["upstream_repo"]))
    tag = rel.get("tag_name", "")

    if cfg["type"] == "dsh_patch":
        from dsh_patcher import dsh_syno_version

        version, display_version = dsh_syno_version(tag)
    else:
        version, display_version = syno_version(tag), syno_version(tag)

    print("上游 {0} 最新: {1}  =>  群晖版本 {2}".format(cfg["upstream_repo"], tag, version))

    if not os.environ.get("FORCE_REBUILD") and already_published(cfg, version):
        print("版本 {0} 已发布，跳过构建。".format(version))
        return 0

    DIST.mkdir(exist_ok=True)
    for stale in DIST.glob("*.spk"):
        stale.unlink()

    assets = []
    workdir = tempfile.mkdtemp(prefix="spk-build-")
    try:
        for arch_cfg in cfg["architectures"]:
            print("构建 " + spk_name(cfg, version, arch_cfg["family"]))
            md5 = build_one(cfg, arch_cfg, version, display_version, tag, rel.get("assets", []), workdir)
            assets.append({
                "file": spk_name(cfg, version, arch_cfg["family"]),
                "md5": md5,
                "family": arch_cfg["family"],
            })
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    manifest = {
        "package": cfg["package_id"],
        "version": version,
        "display_version": display_version,
        "upstream_repo": cfg["upstream_repo"],
        "upstream_tag": tag,
        "os_min_ver": cfg["os_min_ver"],
        "assets": assets,
    }
    (DIST / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "上游版本：" + tag,
        "打包版本：" + version,
        "最低 DSM 版本：" + cfg["os_min_ver"],
        "",
        "| 文件 | 架构 | MD5 |",
        "| --- | --- | --- |",
    ]
    for item in assets:
        lines.append("| `{0}` | {1} | `{2}` |".format(
            item["file"], "ARM" if item["family"] == "arm" else "x86_64", item["md5"]))
    (DIST / "notes.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("构建完成，产物:")
    for item in assets:
        print("  {0}  {1}".format(item["file"], item["md5"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
