import socket
import zipfile

import pytest

from creality_agent.config import Policy
from creality_agent.ingestion import extract_models, import_file, public_target, validate_archive
from creality_agent.models import ServiceError
from creality_agent.network import pin_lan


def test_public_download_rejects_any_private_dns_answer(monkeypatch):
    def records(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in ["93.184.216.34", "192.168.1.8"]]
    monkeypatch.setattr(socket, "getaddrinfo", records)
    with pytest.raises(ServiceError, match="public"):
        public_target("https://models.example/part.stl")
    with pytest.raises(ServiceError, match="credentials"):
        public_target("https://user:secret@models.example/part.stl")


def test_lan_pin_rejects_public_and_keeps_original_tls_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 7125))])
    pinned, host, sni = pin_lan("https://printer.home:7125/printer/info")
    assert pinned == "https://192.168.1.9:7125/printer/info"
    assert host == "printer.home:7125" and sni == "printer.home"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    with pytest.raises(ServiceError, match="LAN"):
        pin_lan("https://printer.home/printer/info")


def test_import_symlink_cannot_escape_operator_roots(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.stl"
    outside.write_text("outside")
    (root / "model.stl").symlink_to(outside)
    with pytest.raises(ServiceError, match="outside"):
        import_file(str(root / "model.stl"), root / "copy.stl", [str(root)], Policy())
    assert not (root / "copy.stl").exists()


@pytest.mark.parametrize("entry", ["../escape.stl", "/absolute.stl", "models/../../escape.stl", "models\\escape.stl"])
def test_archive_paths_cannot_escape(tmp_path, entry):
    source = tmp_path / "models.zip"
    with zipfile.ZipFile(source, "w") as z:
        z.writestr(entry, b"model")
    with pytest.raises(ServiceError, match="Unsafe"):
        extract_models(source, tmp_path / "models", Policy())
    assert not (tmp_path / "models").exists()


def test_archive_expansion_is_bounded_and_selects_only_models(tmp_path):
    source = tmp_path / "models.zip"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("part.stl", b"x" * 100)
        z.writestr("instruction.gcode", "G28")
    with pytest.raises(ServiceError, match="expanded"):
        validate_archive(source, Policy(max_expanded_bytes=50))
    models = extract_models(source, tmp_path / "models", Policy())
    assert [p.name for p in models] == ["part.stl"]
    assert not (tmp_path / "models/instruction.gcode").exists()
