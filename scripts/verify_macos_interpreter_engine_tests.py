#!/usr/bin/env python3
"""Require executed macOS interpreter routing/configuration unit controls.

This validates native gtest evidence, not a signed Flutter application's runtime.
Each XML report must execute its entire corresponding binary/filter inventory.
"""

import argparse
from pathlib import Path
import platform

from verify_native_cache_engine_tests import listed_tests, validate_report


REQUIRED = {
    "shorebird": frozenset({
        "Shorebird.V2ScopeNeverDowngradesWhenMissingOrAmbiguous",
        "Shorebird.V2OriginalBytecodeStreamIsIndependentOwnedAndFreshOnEveryOpen",
        "InterpreterSelectionV2Test.BeginsSameTokenBeforeSelectionAwareKeyRelease",
        "InterpreterSelectionV2Test.V2NeverFallsBackToLegacyProvider",
        "InterpreterSelectionV2Test.StaleTokenDoesNotReleaseKeyOrTouchVm",
        "InterpreterSelectionV2Test.MissingContextNeverBeginsOrCallsKeyProvider",
        "InterpreterSelectionV2Test.ArtifactMutationCannotReuseVerifiedAuthority",
        "InterpreterSelectionV2Test.RejectsReplayProfileLeaseAndAmbiguousContext",
        "InterpreterSelectionV2Test.WireAarch64NeverAcceptsX64EnvelopeMetadata",
        "InterpreterSelectionV2Test.BegunAuthorityKeepsLaterEnginesCoherent",
        "InterpreterSelectionV2Test.ConcurrentBaseWinDiscardsInstalledPatchBeforeMain",
        "InterpreterSelectionV2Test.ConcurrentPatchCommitPreventsBaseBeforeMainCompletes",
        "InterpreterSelectionV2Test.UnreadableOwnedArtifactIsRetryableWithoutForgedKeyStatus",
        "InterpreterSelectionV2Test.WrongProvisionedKeyRetainsTypedDeferral",
        "InterpreterPatchTest.FailedCommittedMainRefusesFreshGroupsUntilColdRestart",
        "InterpreterPatchTest.CommittedBaseFailureCannotRetryAnotherSource",
        "DesktopInterpreterConfigTest.CompiledV2MissingScopeRefusesStartup",
        "DesktopInterpreterConfigTest.V2CheckedInitFailureRefusesUnpreparedBase",
        "DesktopInterpreterConfigTest.ValidV2NoGrantBeginsPreparedFreeBase",
        "UpdaterV2Marker.DirectConfigurationCannotIgnoreCompiledProtocol",

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
    "runtime": frozenset({
        "DartIsolateTest.UncommittedInterpreterGroupSuppressesApplicationShutdownCallback",
        "DartIsolateTest.CommittedInterpreterGroupRetainsApplicationShutdownCallback",
    }),
    "cocoa": frozenset({
        "FlutterDartProjectShorebirdTest.V2ProviderRetainsContextBlockAndSealsAtStartup",
        "FlutterEngineTest.FullInterpreterNeverUsesNativeOrElfRoute",
        "FlutterEngineTest.LegacyProfileKeepsPublicInitializerAndElfRoute",
        "FlutterDartProjectShorebirdTest.ActualLoadedCustomBundleProvidesHostPath",
        "FlutterDartProjectShorebirdTest.ProviderIsRetainedAndCustomAssetsSealRegistration",
    }),
}

# Both native Mac architectures must execute the full source-commitment and
# architecture-binding controls; every listed parameter is mandatory.
REQUIRED["shorebird"] |= {
    "Shorebird.MacFullScopeAcceptsBothWireArchitecturesAndRejectsReplay",
    "DesktopInterpreterConfigTest.V2WrongScopeOsOrArchRefusesBeforeUpdaterInit",
    "DesktopInterpreterConfigTest.MacBodyAndStructuralProfilesRefuseStartup",
} | {
    f"MacArchitectures/MacInterpreterSelectionV2Test.{case}/{arch}"
    for arch in ("Arm64", "X64")
    for case in (
        "FullProfileBeginsBeforeMatchingKeyRelease",
        "WrongScopeOsOrArchNeverBeginsOrReleasesKey",
        "WrongEnvelopeOsOrArchNeverBeginsOrReleasesKey",
        "UnsupportedProfilesNeverBeginOrReleaseKey",
    )
}


# P6: shorebird_unittests of the arm64 engine configured as a hardened
# toolchain. Every legacy control still runs, plus the integrity and
# security-profile controls, and the log must show the binary's compiled
# profile is hardened (the profile-mismatch refusal names it).
REQUIRED["shorebird-hardened"] = REQUIRED["shorebird"] | {
    "UpdaterSecurityProfile.EngineProfileIsOneOfTheContractProfiles",
    "UpdaterSecurityProfile.LinkedRustUpdaterMatchesTheEngineProfile",
    "UpdaterSecurityProfile.MismatchedUpdaterLibraryIsNeverInitialized",
    "UpdaterIntegrity.HardenedInitFailureRecordsTheTypedReason",
    "UpdaterIntegrity.SuccessfulInitRecordsNoFailure",
}
SUITE_ARCHES = {"shorebird-hardened": ("arm64",)}
PROFILE_MARKERS = {"shorebird-hardened": "differs from the engine's hardened;"}


def required_tests(suite, arch):
    if arch not in ("arm64", "x64"):
        raise ValueError(f"Unsupported macOS artifact architecture: {arch}")
    if arch not in SUITE_ARCHES.get(suite, ("arm64", "x64")):
        raise ValueError(f"macOS suite {suite} does not run on {arch}")
    required = REQUIRED[suite]
    return required


def check_profile_marker(evidence, suite):
    marker = PROFILE_MARKERS.get(suite)
    if marker is None:
        return
    log = evidence / f"{suite}-tests.log"
    if marker not in log.read_text(errors="replace"):
        raise SystemExit(f"{log}: no {marker!r}; the engine was not built with the hardened profile")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--arch", choices=("arm64", "x64"), default={
        "arm64": "arm64", "aarch64": "arm64", "x86_64": "x64",
        "amd64": "x64",
    }.get(platform.machine().lower()))
    parser.add_argument("--suite", action="append", choices=sorted(REQUIRED),
                        help="validate only this suite (repeatable); default validates every suite")
    args = parser.parse_args()
    if args.arch is None:
        parser.error("--arch is required when validating on an unknown host")
    suites = args.suite or [suite for suite in REQUIRED
                            if args.arch in SUITE_ARCHES.get(suite, ("arm64", "x64"))]
    for suite in sorted(suites):
        check_profile_marker(args.evidence, suite)
        inventory = listed_tests(args.evidence / f"{suite}-list.txt",
                                 platform="macos", required=required_tests(suite, args.arch))
        count = validate_report(args.evidence / f"{suite}.xml", inventory)
        print(f"macOS {args.arch} {suite}: {count} executed, no failures or skips")


if __name__ == "__main__":
    main()
