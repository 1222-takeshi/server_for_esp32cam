"""Prepare a pinned static JPEG library in a NEW private build directory."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

VERSION = "3.1.4.1"  # Same upstream codec version as the frozen Pi Pillow baseline.
ARCHIVES = {
    "amd64": "c0b8cf74ba68a083008800804c2146a2eb7740d3e7506d6a3fd93a169248ce21",
    "arm64": "d3f9d7ddd998758e1850b37153a7904bdcdeb8cf54a782c772780b0bc50dd553",
}
DOWNLOAD_LIMIT = 8 * 1024**2
FILE_LIMIT = 16 * 1024**2


def download(url):
    with urlopen(url, timeout=30) as response:
        return response.read(DOWNLOAD_LIMIT + 1)


def payload(deb):
    if not deb.startswith(b"!<arch>\n"):
        raise ValueError("Invalid Debian archive")
    offset = 8
    for _ in range(64):
        header = deb[offset:offset + 60]
        if len(header) != 60 or header[-2:] != b"`\n":
            break
        size = int(header[48:58])
        if size < 0 or offset + 60 + size > len(deb):
            break
        name = header[:16].strip().rstrip(b"/")
        if name in {b"data.tar.gz", b"data.tar.xz"}:
            return deb[offset + 60:offset + 60 + size]
        offset += 60 + size + size % 2
    raise ValueError("Missing bounded Debian data archive")


def selected_files(deb):
    result, total = {}, 0
    # Never extract paths, links, modes or owners supplied by the archive.
    with tarfile.open(fileobj=io.BytesIO(payload(deb))) as archive:
        for count, member in enumerate(archive):
            total += member.size
            if count >= 1024 or member.size < 0 or member.size > FILE_LIMIT or total > 64 * 1024**2:
                raise ValueError("Native package exceeds extraction budget")
            name = Path(member.name).name
            if name not in {"libturbojpeg.a", "LICENSE.md"}:
                continue
            if not member.isfile() or name in result:
                raise ValueError("Ambiguous native package file")
            result[name] = archive.extractfile(member).read(FILE_LIMIT + 1)
            if len(result[name]) != member.size:
                raise ValueError("Truncated native package file")
    if set(result) != {"libturbojpeg.a", "LICENSE.md"}:
        raise ValueError("Native archive or license missing")
    return result


def prepare(arch: str, output: Path, *, fetch=download):
    if arch not in ARCHIVES:
        raise ValueError("Unsupported build architecture")
    if output.exists() or output.is_symlink():
        raise FileExistsError("Build directory must be new")
    url = (f"https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/{VERSION}/"
           f"libjpeg-turbo-official_{VERSION}_{arch}.deb")
    deb = fetch(url)
    if len(deb) > DOWNLOAD_LIMIT or hashlib.sha256(deb).hexdigest() != ARCHIVES[arch]:
        raise ValueError("Native package hash/size mismatch")
    files = selected_files(deb)
    output.mkdir(mode=0o700)
    for name, content in files.items():
        with (output / name).open("xb") as handle:
            handle.write(content)
    manifest = {"version": VERSION, "arch": arch, "url": url, "archive_sha256": ARCHIVES[arch],
                "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    # This final manifest is the completion marker; a partial directory is never reused.
    with (output / "manifest.json").open("x") as handle:
        json.dump(manifest, handle, indent=2)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=ARCHIVES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.arch, args.output)))
    except (OSError, ValueError, tarfile.TarError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
