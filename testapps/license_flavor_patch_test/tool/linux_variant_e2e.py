#!/usr/bin/env python3
"""Opt-in native Linux v2 variants; V1 encrypted-cache driver remains unchanged."""
import linux_native_cache_e2e as native
from native_variant_desktop import NativeDesktopVariant, run_driver

class Lifecycle(NativeDesktopVariant,native.Lifecycle):
    module = native
    target_os = 'linux'

if __name__ == '__main__':
    raise SystemExit(run_driver(native,Lifecycle))
