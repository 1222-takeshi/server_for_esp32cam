import hashlib
import io
from pathlib import Path
import tarfile

import pytest

from scripts import prepare_rust_jpeg as prep


def package(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        for name, content in entries:
            member = tarfile.TarInfo(name)
            if content is None:
                member.type = tarfile.SYMTYPE
                member.linkname = "/outside"
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    body = data.getvalue()
    header = f"{'data.tar.gz/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(body):<10}`\n".encode()
    return b"!<arch>\n" + header + body + (b"\n" if len(body) % 2 else b"")


def test_new_private_prefix_keeps_archive_paths_and_links_out(tmp_path, monkeypatch):
    raw = package([("../../libturbojpeg.a", b"static"), ("/docs/LICENSE.md", b"notice"),
                   ("doc", None)])
    monkeypatch.setitem(prep.ARCHIVES, "amd64", hashlib.sha256(raw).hexdigest())
    output = tmp_path / "new"
    result = prep.prepare("amd64", output, fetch=lambda _: raw)
    assert {p.name for p in output.iterdir()} == {"libturbojpeg.a", "LICENSE.md", "manifest.json"}
    assert (output / "libturbojpeg.a").read_bytes() == b"static"
    assert result["files"]["LICENSE.md"] == hashlib.sha256(b"notice").hexdigest()
    assert output.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        prep.prepare("amd64", output, fetch=lambda _: pytest.fail("Must not download again"))


@pytest.mark.parametrize("entries", [
    [("libturbojpeg.a", None), ("LICENSE.md", b"license")],
    [("libturbojpeg.a", b"a"), ("libturbojpeg.a", b"b"), ("LICENSE.md", b"l")],
    [("libturbojpeg.a", b"a")],
])
def test_missing_ambiguous_and_linked_artifact_rejected(entries):
    with pytest.raises(ValueError):
        prep.selected_files(package(entries))


def test_invalid_archive_hash_size_and_native_file_budget(tmp_path, monkeypatch):
    for raw in [b"", b"!<arch>\n", b"not a package"]:
        with pytest.raises(ValueError):
            prep.payload(raw)
    with pytest.raises(ValueError, match="hash/size"):
        prep.prepare("arm64", tmp_path / "bad", fetch=lambda _: b"bad")
    assert not (tmp_path / "bad").exists()
    monkeypatch.setattr(prep, "FILE_LIMIT", 4)
    with pytest.raises(ValueError, match="budget"):
        prep.selected_files(package([("libturbojpeg.a", b"12345"), ("LICENSE.md", b"l")]))
    with pytest.raises(ValueError):
        prep.prepare("unknown", Path("unused"))
