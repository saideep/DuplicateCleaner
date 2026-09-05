"""G7: bootable APFS clones at ``/Volumes/<VOL>/`` must be exclusion-safe.

``EXCLUDED_ROOTS`` matches exact string prefixes and does not know about
external drives. A bootable clone at ``/Volumes/BackupBoot/System``,
``/Volumes/BackupBoot/Library``, etc must be rejected by regex-based
matching in :func:`duplicate_cleaner.paths.validate_not_excluded`.

These are string-only checks — the paths do not exist on the test host.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from duplicate_cleaner.paths import (
    is_excluded_root_path,
    validate_not_excluded,
)


@pytest.mark.parametrize(
    "path",
    [
        "/Volumes/BackupBoot/System/Library/Kernels/kernel",
        "/Volumes/BackupBoot/Library/LaunchDaemons/foo.plist",
        "/Volumes/BackupBoot/Applications/Safari.app/Contents/Info.plist",
        "/Volumes/BackupBoot/usr/bin/env",
        "/Volumes/BackupBoot/opt/local/bin/mtr",
        "/Volumes/BackupBoot/private/etc/hosts",
        "/Volumes/BackupBoot/private/var/db/dslocal/nodes/Default/users",
        "/Volumes/BackupBoot/private/var/log/system.log",
        "/Volumes/BackupBoot/private/var/folders/xy/tmpfile",
        "/Volumes/BackupBoot/Users/alice/Library/Preferences/com.apple.foo",
    ],
)
def test_volumes_system_paths_are_excluded(path: str) -> None:
    assert is_excluded_root_path(Path(path)), (
        f"path {path} should be blocked on a bootable clone at /Volumes/BackupBoot"
    )
    with pytest.raises(ValueError):
        validate_not_excluded(Path(path))


@pytest.mark.parametrize(
    "path",
    [
        # Normal user data on external drives is NOT excluded — that is
        # the whole point of the tool.
        "/Volumes/BackupBoot/Users/alice/Documents/report.pdf",
        "/Volumes/BackupBoot/Users/alice/Desktop/foo.jpg",
        "/Volumes/MyPhotos/2024/vacation.jpg",
    ],
)
def test_volumes_user_paths_are_allowed(path: str) -> None:
    assert not is_excluded_root_path(Path(path)), (
        f"path {path} is legitimate user data on an external drive"
    )
    # Should not raise.
    validate_not_excluded(Path(path))
