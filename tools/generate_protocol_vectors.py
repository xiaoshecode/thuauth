#!/usr/bin/env python3
"""Generate synthetic fixtures with the pinned, unmodified Go reference algorithms.

Reference: z4yx/GoAuthing, GPL-3.0, commit 3730e252ca11e6abbd055d7850c417c5751c1efa.
Only the Python standard library and an already installed local Go toolchain are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "3730e252ca11e6abbd055d7850c417c5751c1efa"
SOURCE_SHA256 = {
    "libauth/coding.go": "5c9946fac17f9fb566314e5304a48268c67f07d9744f7945820149f3b2e68346",
    "libauth/requests.go": "bbca4e8c4ff262ae9503db8db7e6c7f38ac07c6448b6c1c73b47a703083a088d",
}

# Imports belong to this temporary wrapper, not to the original requests.go.
# No HTTP function or third-party import is extracted.
REQUEST_IMPORTS = """package main

import (
    "crypto/md5"
    "encoding/json"
    "errors"
    "fmt"
    "net/url"
)

"""

GO_RUNNER = r"""package main

import (
    "encoding/hex"
    "encoding/json"
    "errors"
    "fmt"
    "net/url"
    "os"
)

type codingVector struct {
    Name       string `json:"name"`
    InputHex   string `json:"input_hex"`
    KeyHex     string `json:"key_hex"`
    XEncodeHex string `json:"xencode_hex"`
    SRBX1      string `json:"srbx1"`
    Base64     string `json:"base64"`
}

type requestVector struct {
    Name            string            `json:"name"`
    Username        string            `json:"username"`
    Password        string            `json:"password"`
    Token           string            `json:"token"`
    IP              string            `json:"ip"`
    AcID            string            `json:"ac_id"`
    Logout          bool              `json:"logout"`
    RawInfoJSON     string            `json:"raw_info_json"`
    Params          map[string]string `json:"params"`
    Query           string            `json:"query"`
    ChallengeParams map[string]string `json:"challenge_params"`
}

type fixture struct {
    SchemaVersion int               `json:"schema_version"`
    SourceCommit  string            `json:"source_commit"`
    Synthetic     bool              `json:"synthetic"`
    SourceSHA256  map[string]string `json:"source_sha256"`
    Coding        []codingVector    `json:"coding"`
    Requests      []requestVector   `json:"requests"`
}

func scalarParams(values url.Values) (map[string]string, error) {
    result := make(map[string]string, len(values))
    for key, items := range values {
        if len(items) != 1 {
            return nil, errors.New("expected one value per parameter")
        }
        result[key] = items[0]
    }
    return result, nil
}

// This observability helper uses Go's own JSON encoder. The extracted original
// buildLoginParams is unchanged; agreement with its info field is checked below.
func rawInfoJSON(v requestVector) (string, error) {
    raw := map[string]string{
        "username": v.Username,
        "password": v.Password,
        "ip": v.IP,
        "acid": v.AcID,
        "enc_ver": "srun_bx1",
    }
    if v.Logout {
        delete(raw, "password")
    }
    data, err := json.Marshal(raw)
    return string(data), err
}

func run() error {
    var result fixture
    if err := json.NewDecoder(os.Stdin).Decode(&result); err != nil {
        return errors.New("cannot decode synthetic cases")
    }
    for i := range result.Coding {
        v := &result.Coding[i]
        data, err := hex.DecodeString(v.InputHex)
        if err != nil {
            return errors.New("invalid synthetic input hex")
        }
        key, err := hex.DecodeString(v.KeyHex)
        if err != nil || len(key) < 13 {
            return errors.New("invalid synthetic key")
        }
        encoded := XEncode(string(data), string(key))
        if encoded == nil {
            return errors.New("reference XEncode returned nil")
        }
        v.XEncodeHex = hex.EncodeToString([]byte(*encoded))
        v.SRBX1 = QuirkBase64Encode(*encoded)
        v.Base64 = QuirkBase64Encode(string(data))
    }
    for i := range result.Requests {
        v := &result.Requests[i]
        if len(v.Token) < 13 {
            return errors.New("invalid synthetic token")
        }
        params, err := buildLoginParams(v.Username, v.Password, v.Token, v.Logout, v.IP, v.AcID)
        if err != nil {
            return errors.New("reference buildLoginParams failed")
        }
        v.RawInfoJSON, err = rawInfoJSON(*v)
        if err != nil {
            return errors.New("cannot encode raw info JSON")
        }
        encoded := XEncode(v.RawInfoJSON, v.Token)
        if encoded == nil || params.Get("info") != "{SRBX1}"+QuirkBase64Encode(*encoded) {
            return errors.New("raw info JSON disagrees with reference request")
        }
        v.Params, err = scalarParams(params)
        if err != nil {
            return err
        }
        v.Query = params.Encode()
        v.ChallengeParams, err = scalarParams(buildChallengeParams(v.Username, v.IP))
        if err != nil {
            return err
        }
    }
    encoder := json.NewEncoder(os.Stdout)
    encoder.SetIndent("", "  ")
    encoder.SetEscapeHTML(false)
    return encoder.Encode(result)
}

func main() {
    if err := run(); err != nil {
        // Errors are deliberately static; never print input or output vectors.
        fmt.Fprintln(os.Stderr, err)
        os.Exit(1)
    }
}
"""


class GenerationError(Exception):
    """A generation failure safe to report without fixture contents."""


def verified_sources() -> dict[str, bytes]:
    """Pin both source blobs independently of the repository's manifest."""
    sources = {}
    for relative, expected in SOURCE_SHA256.items():
        data = (ROOT / "references" / "GoAuthing" / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise GenerationError(f"reference SHA-256 mismatch: {relative}")
        sources[relative] = data
    return sources


def extract_function(source: bytes, name: str) -> bytes:
    """Extract the exact top-level function span from the hash-verified source.

    This is intentionally limited to the pinned snapshot: nested functions are
    indented, and every top-level closing brace starts in column zero.
    """
    starts = list(re.finditer(rb"(?m)^func " + re.escape(name.encode("ascii")) + rb"\(", source))
    if len(starts) != 1:
        raise GenerationError(f"expected exactly one reference function: {name}")
    start = starts[0].start()
    next_func = re.search(rb"(?m)^func ", source[starts[0].end() :])
    stop = starts[0].end() + next_func.start() if next_func else len(source)
    region = source[start:stop]
    closing = list(re.finditer(rb"(?m)^\}\r?$", region))
    if len(closing) != 1:
        raise GenerationError(f"unexpected reference function layout: {name}")
    return region[: closing[0].end()]


def deterministic_bytes(label: str, length: int) -> bytes:
    """SHA-256 counter stream: fixed inputs, independent of random module versions."""
    prefix = ("thuauth-synthetic-v1:" + label).encode("ascii")
    blocks = (
        hashlib.sha256(prefix + i.to_bytes(4, "big")).digest() for i in range((length + 31) // 32)
    )
    return b"".join(blocks)[:length]


def synthetic_cases() -> dict[str, object]:
    lengths = (0, 1, 2, 3, 4, 5, 15, 16, 17, 255, 256, 257)
    inputs = [(f"bytes-{n}", bytes(i % 256 for i in range(n))) for n in lengths]
    inputs.extend(
        [
            ("pseudorandom-257", deterministic_bytes("data", 257)),
            ("binary", b"\x00\xff\x80\x7f\x00\xfe\x01\x00"),
            ("chinese", "合成编码测试".encode()),
            ("emoji", "😀🔑👩\u200d💻".encode()),
            ("all-ff-17", b"\xff" * 17),
        ]
    )
    key = deterministic_bytes("key", 64)
    coding = [
        {"name": f"{name}-key-{n}", "input_hex": data.hex(), "key_hex": key[:n].hex()}
        for name, data in inputs
        for n in (13, 14, 15, 16, 64)
    ]
    special_password = 'synthetic-口令🔑<>&\u2028\u2029\n"\\\r\t'
    scenarios = [
        ("empty-ip", "synthetic-user", "synthetic-password", "", "1", 13),
        ("ipv4-escapes", "synthetic-user-2", special_password, "192.0.2.42", "00042", 14),
        (
            "ipv6-expanded-unicode",
            "synthetic-测试😀",
            special_password,
            "2001:0DB8:0000:0000:0000:0000:0000:0042",
            "00003",
            15,
        ),
        ("ipv6-compressed-empty-password", "synthetic-user-4", "", "2001:db8::42", "7", 16),
        (
            "ipv4-unicode-controls",
            "synthetic-用戶👩\u200d💻",
            "\x00合成\b\f e\u0301",
            "198.51.100.7",
            "01",
            64,
        ),
    ]
    requests = [
        {
            "name": f"{'logout' if logout else 'login'}-{name}",
            "username": username,
            "password": password,
            "token": deterministic_bytes(f"token-{name}", 32).hex()[:token_length],
            "ip": ip,
            "ac_id": ac_id,
            "logout": logout,
        }
        for name, username, password, ip, ac_id, token_length in scenarios
        for logout in (False, True)
    ]
    return {
        "schema_version": 1,
        "source_commit": SOURCE_COMMIT,
        "synthetic": True,
        "source_sha256": SOURCE_SHA256,
        "coding": coding,
        "requests": requests,
    }


def find_go(argument: str | None) -> Path:
    if argument:
        candidate = Path(argument).expanduser().resolve()
    elif (local := ROOT / ".tools" / "go" / "bin" / "go.exe").is_file():
        candidate = local
    elif executable := shutil.which("go"):
        candidate = Path(executable).resolve()
    else:
        raise GenerationError("local Go not found; pass --go with an installed go.exe path")
    if not candidate.is_file():
        raise GenerationError("Go executable not found; check --go")
    return candidate


def generate(go: Path, sources: dict[str, bytes]) -> bytes:
    scratch_root = ROOT / ".tools"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="thuauth-go-vectors-", dir=scratch_root) as directory:
        work = Path(directory)
        coding, count = re.subn(
            rb"(?m)^package libauth$", b"package main", sources["libauth/coding.go"]
        )
        if count != 1:
            raise GenerationError("unexpected reference coding.go package declaration")
        (work / "coding.go").write_bytes(coding)
        functions = [
            extract_function(sources["libauth/requests.go"], name)
            for name in ("buildChallengeParams", "buildLoginParams")
        ]
        (work / "requests.go").write_bytes(
            REQUEST_IMPORTS.encode() + b"\n\n".join(functions) + b"\n"
        )
        (work / "main.go").write_bytes(GO_RUNNER.encode())
        env = os.environ.copy()
        # Ignore persisted Go settings and host build overrides. All writable Go
        # state belongs to this disposable directory, including telemetry state.
        for name in list(env):
            if name.upper().startswith(("GO", "CGO_")):
                del env[name]
        env.update(
            {
                "GO111MODULE": "off",
                "GOTOOLCHAIN": "local",
                "GOPROXY": "off",
                "GOSUMDB": "off",
                "GOWORK": "off",
                "GOENV": "off",
                "GOTELEMETRY": "off",
                "GOTELEMETRYDIR": str(work / "telemetry"),
                "GOCACHE": str(work / "build-cache"),
                "GOPATH": str(work / "gopath"),
                "GOMODCACHE": str(work / "module-cache"),
                "GOTMPDIR": str(work),
                "CGO_ENABLED": "0",
                "TMP": str(work),
                "TEMP": str(work),
            }
        )
        result = subprocess.run(
            [str(go), "run", "coding.go", "requests.go", "main.go"],
            cwd=work,
            env=env,
            input=json.dumps(synthetic_cases(), ensure_ascii=True).encode("ascii"),
            capture_output=True,
            timeout=300,
            check=False,
        )
        if result.returncode:
            # Do not echo subprocess buffers, even when generation fails.
            raise GenerationError(f"local go run failed (exit {result.returncode})")
        try:
            document = json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise GenerationError("local go run did not produce a JSON fixture") from exc
        if (
            not isinstance(document, dict)
            or document.get("synthetic") is not True
            or document.get("schema_version") != 1
            or document.get("source_commit") != SOURCE_COMMIT
            or document.get("source_sha256") != SOURCE_SHA256
        ):
            raise GenerationError("local go run produced unexpected fixture metadata")
        return result.stdout


def write_fixture(output: Path, data: bytes) -> None:
    """Replace only after successful generation, without Windows open-file locks."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=".go-vectors-", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(data)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--go", help="path to a local go.exe (default: .tools/go/bin/go.exe, then PATH)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "go_protocol_vectors.json",
        help="fixture path (default is relative to this repository; explicit paths use cwd)",
    )
    parser.add_argument(
        "--check", action="store_true", help="compare bytes; never write the fixture"
    )
    args = parser.parse_args()
    try:
        output = args.output.expanduser().resolve()
        if output.is_relative_to((ROOT / "references").resolve()):
            raise GenerationError("output must not be inside the read-only references directory")
        if args.check and not output.is_file():
            raise GenerationError("fixture is missing; generate it before using --check")
        data = generate(find_go(args.go), verified_sources())
        if args.check:
            if output.read_bytes() != data:
                print("Fixture differs; no files written.", file=sys.stderr)
                return 1
            status = "Fixture matches"
        else:
            write_fixture(output, data)
            status = "Fixture generated"
        document = json.loads(data)
        print(f"{status}: {len(document['coding'])} coding, {len(document['requests'])} requests.")
        return 0
    except GenerationError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print("Error: local go run timed out; fixture not written.", file=sys.stderr)
        return 2
    except OSError:
        print("Error: local file access or Go execution failed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
