#!/usr/bin/env python3
"""Require executed macOS interpreter routing/configuration unit controls.

This validates native gtest evidence, not a signed Flutter application's runtime.
Each XML report must execute its entire corresponding binary/filter inventory.
"""

import argparse
from pathlib import Path

from verify_native_cache_engine_tests import listed_tests, validate_report


REQUIRED = {
    "shorebird": frozenset({
        "DesktopInterpreterConfigTest.PreservesDesktopScopeAndWholeFramework",
        "DesktopInterpreterConfigTest.UpdaterFailureRetainsSignedBaseWithoutKey",
        "DesktopInterpreterConfigTest.InvalidBaseRetainedAndStartupRejected",
        "DesktopInterpreterConfigTest.SettingsOwnSelectionAfterArgumentsGoAway",
        "DesktopInterpreterConfigTest.BaseOnlyChoiceIsSealedAcrossLaterEngines",
        "InterpreterPatchTest.MacOSUsesTrustedPlatformInSdkOptions",
        "InterpreterPatchTest.MacOSRejectsAuthenticatedIosArtifactWithoutReload",
        "InterpreterPatchTest.InvalidTrustedTargetNeverCallsSdk",
        "InterpreterPatchTest.UpdateFailureRequiresFreshGroupBeforeBaseInstall",
        "InterpreterPatchTest.OnlyTypedKeyRefusalsAreRetryable",
        "InterpreterPatchTest.ValidationRejectionClearsPriorRetryableStatus",
        "InterpreterPatchTest.BaseSelectionWaitsForKeyDeferralBookkeeping",
        "InterpreterPatchTest.PermanentRejectionIsReportedBeforeBaseSelection",
        "InterpreterPatchTest.CompletedMainWinsBeforeConcurrentKeyRejection",
        "InterpreterPatchTest.CompletedMainWinsBeforeConcurrentEntrypointFailure",
        "UpdaterTest.KeyDeferralPreventsFallbackFailureAndPatchSuccessReports",
        "UpdaterTest.FailedKeyDeferralSuppressesPatchSuccessAndBackgroundUpdates",
        "UpdaterTest.LaterKeyDeferralCannotChangeAcceptedPatch",
        "InterpreterPatchTest.BrokenBaseOrCompiledProfileCannotFallBackToNativeApp",
    }),
    "embedder": frozenset({
        "ShorebirdInterpreterEmbedderTest.MissingPrivateConfigurationIsRejected",
        "ShorebirdInterpreterEmbedderTest.ExternalSnapshotOverridesAreRejected",
        "EmbedderTest.InvalidAOTDataSourcesMustReturnError",
        "EmbedderTest.CanCreateAndCollectAValidElfSource",
        "EmbedderTest.CanLaunchAndShutdownWithAValidElfSource",
    }),
    "cocoa": frozenset({
        "FlutterEngineTest.FullInterpreterNeverUsesNativeOrElfRoute",
        "FlutterEngineTest.LegacyProfileKeepsPublicInitializerAndElfRoute",
        "FlutterDartProjectShorebirdTest.ActualLoadedCustomBundleProvidesHostPath",
        "FlutterDartProjectShorebirdTest.ProviderIsRetainedAndCustomAssetsSealRegistration",
    }),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    for suite, required in REQUIRED.items():
        inventory = listed_tests(args.evidence / f"{suite}-list.txt",
                                 platform="macos", required=required)
        count = validate_report(args.evidence / f"{suite}.xml", inventory)
        print(f"macOS {suite}: {count} executed, no failures or skips")


if __name__ == "__main__":
    main()
