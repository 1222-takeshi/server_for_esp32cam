"""Hash-guarded ESP32-only fix for the pinned WebSockets 2.7.3 build copy."""

import hashlib
from pathlib import Path

ORIGINAL = "6fa7b92afb9ca87d0eb6e6cc8c2d777076a36776d888738152ad7a0e66cfae9f"
PATCHED = "47606cf81ee8079648e4b0136cfba2c486fc94a985f7d98bb0e22a6f25a013e8"


def patched_source(raw):
    if hashlib.sha256(raw).hexdigest() != ORIGINAL:
        raise ValueError("unexpected WebSockets 2.7.3 source")
    source = raw.decode().replace("\r\n", "\n")
    source = source.replace("#include <esp_system.h>", "#include <esp_system.h>\n#include <esp_random.h>")
    source = source.replace("length < 0xFFFF", "length <= 0xFFFF")
    start = source.index("bool WebSockets::sendFrameHeader(")
    pos = source.index("    uint8_t maskKey", start)
    source = source[:pos] + (
        "    // Header-only client writes cannot mask the subsequent payload.\n"
        "    if(!client || client->cIsClient) return false;\n"
    ) + source[pos:]
    start = source.index("bool WebSockets::sendFrame(")
    pos = source.index("    if(client->tcp", start)
    source = source[:pos] + (
        "    if(!client || !client->tcp || (length && !payload) ||\n"
        "       (headerToPayload && !payload) || length > UINT32_MAX - WEBSOCKETS_MAX_HEADER_SIZE) return false;\n"
    ) + source[pos:]
    source = source.replace(
        "if(!headerToPayload && ((length > 0) && (length < 1400)) && (GET_FREE_HEAP > 6000)) {",
        "if(!headerToPayload && length > 0 &&\n"
        "       (client->cIsClient || (length < 1400 && GET_FREE_HEAP > 6000))) {",
    )
    source = source.replace(
        "        if(dataPtr) {\n            memcpy",
        "        if(!dataPtr && client->cIsClient) return false;\n        if(dataPtr) {\n            memcpy",
    )
    begin = source.index("    if(client->cIsClient && useInternBuffer)", start)
    end = source.index("\n    createHeader", begin)
    source = source[:begin] + (
        "    if(client->cIsClient) {\n"
        "        // Wi-Fi is active: ESP-IDF hardware RNG has RF entropy.\n"
        "        esp_fill_random(maskKey, sizeof(maskKey));\n    }\n"
    ) + source[end:]
    source = source.replace("if(client->cIsClient && useInternBuffer)", "if(client->cIsClient)")
    pos = source.index("    return ret;", start)
    source = source[:pos] + (
        "    // A partial frame poisons this TCP stream; never append a new frame.\n"
        "    if(!ret && client->cIsClient) clientDisconnect(client);\n"
    ) + source[pos:]
    return source.encode()


def patch_library(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() == PATCHED:
        return
    result = patched_source(raw)
    if hashlib.sha256(result).hexdigest() != PATCHED:
        raise ValueError("WebSockets patch digest mismatch")
    path.write_bytes(result)


if "Import" in globals():  # PlatformIO post script: dependencies are installed.
    globals()["Import"]("env")
    build = globals()["env"]
    root = Path(build.subst("$PROJECT_LIBDEPS_DIR")) / build.subst("$PIOENV")
    path = root / "WebSockets/src/WebSockets.cpp"
    if (not root.resolve().is_relative_to(Path(build.subst("$PROJECT_DIR")).resolve())
            or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError("refusing to patch a shared dependency")
    patch_library(path)
