#!/usr/bin/env python3
"""Require executed, successful native-cache engine controls in gtest XML.

The Shorebird report must cover its complete --gtest_list_tests inventory. The
embedder report must contain exactly the platform's release/AOT controls.
Windows additionally requires its four native adapter controls. This validator
does not execute native code; --self-test uses synthetic XML only.
"""

import argparse
import contextlib
import io
from pathlib import Path
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


COMMON_EMBEDDER_TESTS = frozenset({
    "EmbedderTest.PreparedMemoryImageSurvivesInputAndMultipleEngines",
    "EmbedderTest.InvalidAOTDataSourcesMustReturnError",
    "EmbedderTest.CanCreateAndCollectAValidElfSource",
    "EmbedderTest.CanLaunchAndShutdownWithAValidElfSource",
})
FD_EMBEDDER_TEST = "EmbedderTest.PreparedFDOwnershipAndImageSurviveMultipleEngines"
COMMON_SHOREBIRD_TESTS = frozenset({
    "PreparedUpdaterTest.BeginsBeforeTheLoaderAndDoesNotStartLegacyBoot",
    "PreparedUpdaterTest.LaterEngineFailureCannotSwitchRunningPatchToBase",
    "PreparedUpdaterTest.LaterBaseSelectionCannotReplaceRunningPatch",
    "PreparedUpdaterTest.ExplicitBaseIsBegunWithoutLoadingTheCandidate",
    "PreparedUpdaterTest.ReturnedLoaderFailureRetriesBaseWithoutRetiringPatch",
    "PreparedUpdaterTest.StaleSelectionNeverReachesTheCandidateLoader",
    "PreparedUpdaterTest.FailedRequiredConfigurationCannotStartImplicitly",
    "PreparedUpdaterTest.PreparationFailureCannotFallThroughToLegacySelection",
    "PreparedUpdaterTest.FailedAbortCannotReportCandidateSuccessOrStartUpdates",
    "PreparedBootSourceTest.MemoryLeaseOutlivesCallerAndEndsAfterLoader",
    "Shorebird.NativePatchBudgetUsesReleaseDefaultOrExplicitOverride",
    "Shorebird.NativePatchBudgetRejectsMalformedAmbiguousAndOverflowValues",
})
FD_SHOREBIRD_TEST = "PreparedBootSourceTest.ClosesOwnedFDAndTransfersDetachedFD"
WINDOWS_ENGINE_TESTS = frozenset({
    "ShorebirdFlutterWindowsEngineTest.PreparedMemoryRunsAndLaterEnginesCannotMixBase",
    "ShorebirdFlutterWindowsEngineTest.RefusedMemoryBeginsBaseBeforeLoadingIt",
    "ShorebirdFlutterWindowsEngineTest.FailedPreparationNeverLoadsBundledOrLegacyCode",
    "ShorebirdFlutterWindowsEngineTest.FailedPreparedConfigurationNeverPreparesOrLoadsCode",
})


def embedder_tests(platform: str) -> frozenset[str]:
    if platform == "windows":
        return COMMON_EMBEDDER_TESTS
    if platform == "linux":
        return COMMON_EMBEDDER_TESTS | {FD_EMBEDDER_TEST}
    raise ValueError(f"Unsupported native-cache test platform: {platform}")


def required_shorebird_tests(platform: str) -> frozenset[str]:
    if platform == "windows":
        return COMMON_SHOREBIRD_TESTS
    if platform == "linux":
        return COMMON_SHOREBIRD_TESTS | {FD_SHOREBIRD_TEST}
    raise ValueError(f"Unsupported native-cache test platform: {platform}")


EMBEDDER_TESTS = embedder_tests("linux")
REQUIRED_SHOREBIRD_TESTS = required_shorebird_tests("linux")


def listed_tests(path: Path, platform: str = "linux",
                 required: frozenset[str] | None = None) -> set[str]:
    """Parse gtest inventory, including parameterized names and inline comments."""
    suite = None
    names = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line[0].isspace() and line.endswith("."):
            suite = line[:-1]
            if not suite or any(char.isspace() for char in suite):
                raise ValueError(f"Invalid gtest suite inventory line: {raw!r}")
            continue
        name = line.strip()
        if suite is None or not line.startswith("  ") or not re.fullmatch(r"[\w/]+", name):
            raise ValueError(f"Invalid gtest inventory line: {raw!r}")
        full_name = f"{suite}.{name}"
        if full_name in names or any(part.startswith("DISABLED_") for part in (suite, name)):
            raise ValueError(f"Duplicate or disabled listed test: {full_name}")
        names.add(full_name)
    if required is None:
        required = required_shorebird_tests(platform)
    if not required <= names:
        raise ValueError(f"Missing {platform} Shorebird controls in binary: {sorted(required - names)}")
    return names


def counter(element: ET.Element, attribute: str, expected: int) -> None:
    value = element.get(attribute, "")
    if not value.isdecimal() or int(value) != expected:
        raise ValueError(f"{element.tag}.{attribute} must equal {expected}, got {value!r}")


def validate_report(path: Path, expected: set[str] | frozenset[str]) -> int:
    root = ET.parse(path).getroot()
    if root.tag != "testsuites" or not expected:
        raise ValueError("Expected a nonempty executed gtest testsuites report")
    for tag in ("failure", "error", "skipped"):
        if root.find(f".//{tag}") is not None:
            raise ValueError(f"Report contains {tag}: {path}")
    seen = set()
    suite_names = set()
    for suite in root.findall("testsuite"):
        name = suite.get("name", "")
        if not name or name in suite_names:
            raise ValueError(f"Missing or duplicate suite name: {name!r}")
        suite_names.add(name)
        cases = suite.findall("testcase")
        counter(suite, "tests", len(cases))
        for attribute in ("failures", "errors", "disabled", "skipped"):
            counter(suite, attribute, 0)
        for case in cases:
            full_name = f"{name}.{case.get('name', '')}"
            if case.get("classname") != name or not case.get("name") or full_name in seen:
                raise ValueError(f"Invalid or duplicate test identity: {full_name}")
            if case.get("status") != "run" or case.get("result") != "completed":
                raise ValueError(f"Test did not execute successfully: {full_name}")
            seen.add(full_name)
    counter(root, "tests", len(seen))
    for attribute in ("failures", "errors", "disabled"):
        counter(root, attribute, 0)
    if root.get("skipped") is not None:
        counter(root, "skipped", 0)
    if len(root.findall(".//testcase")) != len(seen) or seen != expected:
        raise ValueError(f"Wrong executed test inventory: missing={sorted(expected - seen)}, unexpected={sorted(seen - expected)}")
    return len(seen)


class ValidatorTests(unittest.TestCase):
    """Synthetic fixtures exercise the acceptance oracle, not native execution."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="native-cache-gtest-validator-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "report.xml"

    def report(self, names=EMBEDDER_TESTS):
        root = ET.Element("testsuites", tests=str(len(names)), failures="0", errors="0", disabled="0")
        for suite_name in sorted({name.split(".", 1)[0] for name in names}):
            tests = sorted(name.split(".", 1)[1] for name in names if name.split(".", 1)[0] == suite_name)
            suite = ET.SubElement(root, "testsuite", name=suite_name, tests=str(len(tests)), failures="0", errors="0", disabled="0", skipped="0")
            for name in tests:
                ET.SubElement(suite, "testcase", name=name, classname=suite_name, status="run", result="completed")
        return root

    def write(self, root):
        ET.ElementTree(root).write(self.path, encoding="utf-8", xml_declaration=True)

    def listing(self, names):
        listing = self.path.with_suffix(".txt")
        listing.write_text("".join(f"{suite}.\n" + "".join(f"  {name.split('.', 1)[1]}\n" for name in sorted(names) if name.startswith(suite + ".")) for suite in sorted({name.split('.', 1)[0] for name in names})), encoding="utf-8")
        return listing

    def test_exact_controls_pass_and_full_shorebird_inventory_includes_legacy(self):
        self.write(self.report())
        self.assertEqual(validate_report(self.path, EMBEDDER_TESTS), 5)
        names = REQUIRED_SHOREBIRD_TESTS | {"UpdaterTest.ReportLaunchStartOnlyCallsOnce"}
        listing = self.listing(names)
        self.write(self.report(names))
        self.assertEqual(validate_report(self.path, listed_tests(listing)), len(names))
        root = self.report(REQUIRED_SHOREBIRD_TESTS)
        self.write(root)
        with self.assertRaises(ValueError):
            validate_report(self.path, listed_tests(listing))

    def test_missing_duplicate_unexpected_and_zero_tests_fail(self):
        for names in (set(), set(EMBEDDER_TESTS) - {min(EMBEDDER_TESTS)}, set(EMBEDDER_TESTS) | {"EmbedderTest.Unrequested"}):
            with self.subTest(names=names):
                self.write(self.report(names))
                with self.assertRaises(ValueError):
                    validate_report(self.path, EMBEDDER_TESTS)
        root = self.report()
        root.find("testsuite").append(ET.fromstring(ET.tostring(root.find(".//testcase"))))
        self.write(root)
        with self.assertRaises(ValueError):
            validate_report(self.path, EMBEDDER_TESTS)

    def test_skipped_failed_unexecuted_listing_only_and_forged_counts_fail(self):
        mutations = [
            lambda root: root.find(".//testcase").set("result", "skipped"),
            lambda root: root.find(".//testcase").set("status", "notrun"),
            lambda root: root.find(".//testcase").attrib.pop("result"),
            lambda root: ET.SubElement(root.find(".//testcase"), "failure"),
            lambda root: ET.SubElement(root.find(".//testcase"), "skipped"),
            lambda root: root.set("failures", "1"),
            lambda root: root.set("tests", "0"),
            lambda root: root.find("testsuite").set("skipped", "1"),
            lambda root: root.find("testsuite").set("disabled", "1"),
        ]
        for expected in (embedder_tests("linux"), embedder_tests("windows"), WINDOWS_ENGINE_TESTS):
            for mutation in mutations:
                root = self.report(expected)
                mutation(root)
                self.write(root)
                with self.subTest(expected=expected, mutation=mutation), self.assertRaises(ValueError):
                    validate_report(self.path, expected)
        self.path.write_text("<testsuites", encoding="utf-8")
        with self.assertRaises(ET.ParseError):
            validate_report(self.path, EMBEDDER_TESTS)

    def test_incomplete_or_disabled_list_fails(self):
        for listing in ("", "PreparedUpdaterTest.\n  DISABLED_Example\n", "not a gtest inventory\n"):
            self.path.write_text(listing, encoding="utf-8")
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                listed_tests(self.path)

    def test_platform_specific_controls_and_full_inventory(self):
        windows_names = required_shorebird_tests("windows") | {"UpdaterTest.ReportLaunchStartOnlyCallsOnce"}
        listing = self.listing(windows_names)
        self.write(self.report(windows_names))
        self.assertEqual(validate_report(self.path, listed_tests(listing, "windows")), len(windows_names))
        with self.assertRaises(ValueError):
            listed_tests(listing, "linux")
        self.write(self.report(embedder_tests("windows")))
        self.assertEqual(validate_report(self.path, embedder_tests("windows")), 4)
        with self.assertRaises(ValueError):
            validate_report(self.path, embedder_tests("linux"))
        self.write(self.report(embedder_tests("linux")))
        with self.assertRaises(ValueError):
            validate_report(self.path, embedder_tests("windows"))

    def test_memory_controls_cannot_be_replaced_by_legacy_controls(self):
        memory_shorebird = "PreparedBootSourceTest.MemoryLeaseOutlivesCallerAndEndsAfterLoader"
        memory_embedder = "EmbedderTest.PreparedMemoryImageSurvivesInputAndMultipleEngines"
        for platform in ("linux", "windows"):
            with self.subTest(platform=platform):
                listing = self.listing(required_shorebird_tests(platform) - {memory_shorebird})
                with self.assertRaises(ValueError):
                    listed_tests(listing, platform)
                self.write(self.report(embedder_tests(platform) - {memory_embedder}))
                with self.assertRaises(ValueError):
                    validate_report(self.path, embedder_tests(platform))
                # A stale legacy-only XML cannot satisfy the new inventory.
                listing = self.listing(required_shorebird_tests(platform))
                self.write(self.report(required_shorebird_tests(platform) - {memory_shorebird}))
                with self.assertRaises(ValueError):
                    validate_report(self.path, listed_tests(listing, platform))

    def test_invalid_platform_fails(self):
        for controls in (required_shorebird_tests, embedder_tests):
            with self.assertRaises(ValueError):
                controls("macos")

    def test_windows_adapter_requires_exact_executed_controls(self):
        self.write(self.report(WINDOWS_ENGINE_TESTS))
        self.assertEqual(validate_report(self.path, WINDOWS_ENGINE_TESTS), 4)
        for names in (set(), WINDOWS_ENGINE_TESTS - {min(WINDOWS_ENGINE_TESTS)}, WINDOWS_ENGINE_TESTS | {"ShorebirdFlutterWindowsEngineTest.Unrequested"}, embedder_tests("windows")):
            self.write(self.report(names))
            with self.subTest(names=names), self.assertRaises(ValueError):
                validate_report(self.path, WINDOWS_ENGINE_TESTS)

    def test_platform_cli_requires_the_matching_complete_reports(self):
        for platform in ("linux", "windows"):
            shorebird = self.listing(required_shorebird_tests(platform))
            paths = {}
            inventories = {"shorebird": required_shorebird_tests(platform), "embedder": embedder_tests(platform)}
            if platform == "windows":
                inventories["windows"] = WINDOWS_ENGINE_TESTS
            for name, inventory in inventories.items():
                paths[name] = Path(self.directory.name) / f"{name}.xml"
                ET.ElementTree(self.report(inventory)).write(paths[name], encoding="utf-8")
            arguments = ["--shorebird-list", str(shorebird), "--shorebird-xml", str(paths["shorebird"]), "--embedder-xml", str(paths["embedder"])]
            with self.subTest(platform=platform), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if platform == "linux":
                    self.assertEqual(main(arguments), 0)  # Linux remains the default.
                    with self.assertRaises(SystemExit):
                        main(arguments + ["--windows-xml", str(paths["shorebird"])])
                else:
                    arguments += ["--platform", "windows"]
                    with self.assertRaises(SystemExit):
                        main(arguments)
                    self.assertEqual(main(arguments + ["--windows-xml", str(paths["windows"])]), 0)
                    self.assertEqual(main(arguments + ["--windows-xml", str(paths["embedder"])]), 1)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--platform", choices=("linux", "windows"), default="linux")
    parser.add_argument("--shorebird-list", type=Path)
    parser.add_argument("--shorebird-xml", type=Path)
    parser.add_argument("--embedder-xml", type=Path)
    parser.add_argument("--windows-xml", type=Path,
                        help="Required native Windows adapter report for --platform windows")
    args = parser.parse_args(argv)
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ValidatorTests))
        return 0 if result.wasSuccessful() else 1
    if not all((args.shorebird_list, args.shorebird_xml, args.embedder_xml)):
        parser.error("--shorebird-list, --shorebird-xml, and --embedder-xml are required")
    if (args.platform == "windows") != bool(args.windows_xml):
        parser.error("--windows-xml is required exactly when --platform windows")
    try:
        shorebird = validate_report(args.shorebird_xml, listed_tests(args.shorebird_list, args.platform))
        embedder = validate_report(args.embedder_xml, embedder_tests(args.platform))
        windows = validate_report(args.windows_xml, WINDOWS_ENGINE_TESTS) if args.windows_xml else 0
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"Native cache engine test contract failed: {error}", file=sys.stderr)
        return 1
    adapter = f", {windows} Windows adapter controls" if args.windows_xml else ""
    print(f"Native cache engine test contract passed ({args.platform}): {shorebird} Shorebird tests, {embedder} AOT embedder controls{adapter} executed; no failures/skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
