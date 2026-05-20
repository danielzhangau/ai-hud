"""HMAC-SHA256 sidecar signing for speed databases.

Used by both the build pipeline (sign) and the device (verify), so the
two stay in lock-step on file format and key handling. stdlib only --
nothing to install on the RV1106's tiny Python rootfs.

Threat model
============
Without signing, an attacker on the OTA path can replace `speed_zones.db`
with a forged file that claims the local Hume Highway is a 200 km/h
zone, then push it via the same channel the launcher uses. The driver
would see a wrong (high) limit and the existing "宁可不报" pipeline
would happily display it -- there is no integrity gate today.

What we sign
============
Per-file HMAC-SHA256 over the raw `.db` bytes, written to a sidecar
file `<name>.sig` containing exactly 32 binary bytes. We chose a
sidecar over an embedded header because:
  * Existing v3 .db files don't need a format bump -- signing is
    transport-layer, not on-disk layout.
  * Tools like `sha256sum`/diff still work on the .db unchanged.
  * Sidecar can be regenerated locally without re-writing the DB
    after a key rotation.

Rollback protection
===================
The signature alone doesn't prevent replay of a *previously valid*
old DB (e.g. one signed before a state lowered a road's speed). We
additionally track the highest build_epoch the device has accepted
in `/etc/ai_hud_db_min_epoch` -- any new DB whose build_epoch is
strictly less is rejected as a downgrade. The file lives in /etc
so a factory-reset wipes it; the device boots with min_epoch=0 and
trusts whatever signed DB ships in the firmware image.

Key handling
============
- Build side: AI_HUD_DB_SECRET env var (64-char hex or 32-char hex
  string -- we accept either by SHA-256-hashing whatever's provided
  into a 32-byte key. This makes "ai-hud-dev" a usable dev key
  without requiring developers to generate proper entropy).
- Device side: /etc/ai_hud_db_secret, 0600, owned by root. Same
  normalization applies.
- Local dev: missing env var falls back to the literal string
  "ai-hud-dev-only" -- a key any attacker reading public source can
  derive. That's intentional: production CI MUST set the real secret
  via GitHub Secrets; dev mode is for local roundtrip only.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct
from pathlib import Path

# Used when no real secret is configured. Anyone reading the repo can
# regenerate signatures with this key -- which is the whole point of
# calling it out as "dev only".
_DEV_FALLBACK_KEY_MATERIAL = b"ai-hud-dev-only"

# Standard sidecar suffix.
SIG_SUFFIX = ".sig"

# Production secret path on the device. Rootfs-owned, 0600 root.
DEVICE_SECRET_PATH = "/etc/ai_hud_db_secret"

# Rollback-protection: highest build_epoch we've already accepted.
# Wiped by factory reset.
MIN_EPOCH_PATH = "/etc/ai_hud_db_min_epoch"

# Escape hatch for recovery / dev. Presence of this file lets the
# device load .db without verifying a .sig. Should not exist in
# production firmware images.
UNSIGNED_OVERRIDE_PATH = "/etc/ai_hud_db_allow_unsigned"


def _normalize_key(material: bytes) -> bytes:
    """Hash any key-material into a 32-byte key.

    Lets the operator drop a phrase, a hex string, or raw bytes in
    AI_HUD_DB_SECRET without worrying about length. Output is always
    SHA-256(input) so weak inputs still get a 256-bit "key" -- but the
    underlying entropy obviously cannot exceed the input's.
    """
    return hashlib.sha256(material).digest()


def load_key(env_var: str = "AI_HUD_DB_SECRET",
             device_path: str | None = None) -> bytes:
    """Resolve the signing key from env var, then device path, then dev fallback.

    Args:
        env_var:     name of the env var to look at first.
        device_path: optional file to read when env_var is unset. Defaults
                     to DEVICE_SECRET_PATH on the device, None elsewhere.

    Returns:
        32-byte normalized key.
    """
    material = os.environ.get(env_var, "").encode("utf-8")
    if not material and device_path:
        try:
            with open(device_path, "rb") as f:
                material = f.read().strip()
        except OSError:
            material = b""
    if not material:
        material = _DEV_FALLBACK_KEY_MATERIAL
    return _normalize_key(material)


def sign_file(db_path: Path, key: bytes,
              sig_path: Path | None = None) -> Path:
    """Write HMAC-SHA256(db_path) to <db_path>.sig (or `sig_path`).

    Returns the path that was written.
    """
    db_path = Path(db_path)
    sig_path = sig_path or db_path.with_suffix(db_path.suffix + SIG_SUFFIX)
    h = hmac.new(key, digestmod=hashlib.sha256)
    with db_path.open("rb") as f:
        while True:
            chunk = f.read(1 << 20)   # 1 MiB
            if not chunk:
                break
            h.update(chunk)
    sig_path.write_bytes(h.digest())
    return sig_path


def verify_file(db_path: Path, key: bytes,
                sig_path: Path | None = None) -> bool:
    """Return True iff `<db_path>.sig` matches HMAC-SHA256(db_path).

    Uses hmac.compare_digest to avoid timing side-channels. A missing
    sidecar returns False (fail-closed); the caller decides whether
    the absence is acceptable.
    """
    db_path = Path(db_path)
    sig_path = sig_path or db_path.with_suffix(db_path.suffix + SIG_SUFFIX)
    if not sig_path.is_file():
        return False
    try:
        expected = sig_path.read_bytes()
    except OSError:
        return False
    if len(expected) != hashlib.sha256().digest_size:
        return False
    h = hmac.new(key, digestmod=hashlib.sha256)
    try:
        with db_path.open("rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return False
    return hmac.compare_digest(h.digest(), expected)


def read_min_epoch(path: str | None = None) -> int:
    """Highest build_epoch the device has ever accepted (0 if absent).

    Stored as a literal decimal string so a human can `cat` it during
    field debugging. We refuse to load a DB whose own build_epoch is
    less than this value -- that's our anti-rollback gate.

    `path` defaults to the module-level constant resolved at CALL time
    (not definition time) so tests can monkeypatch MIN_EPOCH_PATH on
    the module to redirect IO into a temp directory.
    """
    if path is None:
        path = MIN_EPOCH_PATH
    try:
        with open(path, "r") as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def write_min_epoch(epoch: int, path: str | None = None) -> None:
    """Persist a new high-water mark. Atomic rename so a power loss
    can't leave an empty file behind (which would forget our history
    of accepted DBs and re-enable rollback)."""
    if path is None:
        path = MIN_EPOCH_PATH
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(f"{int(epoch)}\n")
        os.replace(tmp, path)
    except OSError:
        # Best-effort -- /etc may be read-only in some recovery modes.
        try:
            os.unlink(tmp)
        except OSError:
            pass


def allow_unsigned(path: str | None = None) -> bool:
    """True iff the unsigned-DB escape hatch file exists on disk.

    Resolves UNSIGNED_OVERRIDE_PATH at call time (not at def time)
    so tests that swap the module constant get the honoured value.
    """
    if path is None:
        path = UNSIGNED_OVERRIDE_PATH
    return os.path.isfile(path)


# Header layout shared by every DB version: 16-byte v1 prefix
# (magic[4] + version[2] + count[4] + rec_size[2] + flags[4]). The
# build-time tools (db_health) and device-time loader (speed_db) both
# parse this, so it lives in db_signer to give them a single source
# of truth.
_HEADER_FMT_V1 = "<4sHIHI"
_HEADER_SIZE_V1 = 16


def _read_header(db_path: Path):
    """Return (magic, version, count, rec_size, flags) or None on failure."""
    try:
        with open(db_path, "rb") as f:
            head = f.read(_HEADER_SIZE_V1)
            if len(head) < _HEADER_SIZE_V1:
                return None
            return struct.unpack(_HEADER_FMT_V1, head)
    except OSError:
        return None


def read_build_epoch_from_header(db_path: Path) -> int:
    """Peek at the DB header to extract build_epoch without loading records.

    Returns 0 for v1 files (no build_epoch field), for unreadable
    files, and for non-v1/v2/v3 versions. The caller uses this for
    rollback comparison BEFORE running HMAC verification, because if
    the file is rolled back we'd rather reject early than spend MB of
    HMAC work just to throw the result away.
    """
    hdr = _read_header(db_path)
    if hdr is None:
        return 0
    _magic, version, _count, _rec_size, _flags = hdr
    if version == 1:
        return 0
    try:
        with open(db_path, "rb") as f:
            f.seek(_HEADER_SIZE_V1)
            extra = f.read(4)
            if len(extra) < 4:
                return 0
            (epoch,) = struct.unpack("<I", extra)
            return int(epoch)
    except OSError:
        return 0


def read_record_count_from_header(db_path: Path) -> int | None:
    """Return record count, or None if file unreadable / header truncated."""
    hdr = _read_header(db_path)
    if hdr is None:
        return None
    return int(hdr[2])
