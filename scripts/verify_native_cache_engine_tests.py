#!/usr/bin/env python3
"""Require executed, successful Linux native-cache engine controls in gtest XML.

The Shorebird report must cover its complete --gtest_list_tests inventory. The
embedder report must contain exactly the four release/AOT controls below. This
validator does not execute native code; --self-test uses synthetic XML only.
"""

import argparse
from pathlib import Path
import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


EMBEDDER_TESTS = frozenset({
    "EmbedderTest.PreparedFDOwnershipAndImageSurviveMultipleEngines",
    "EmbedderTest.InvalidAOTDataSourcesMustReturnError",
    "EmbedderTest.CanCreateAndCollectAValidElfSource",
    "EmbedderTest.CanLaunchAndShutdownWithAValidElfSource",
})
REQUIRED_SHOREBIRD_TESTS = frozenset({
    "PreparedUpdaterTest.BeginsBeforeTheLoaderAndDoesNotStartLegacyBoot",
    "PreparedUpdaterTest.LaterEngineFailureCannotSwitchRunningPatchToBase",
    "PreparedUpdaterTest.LaterBaseSelectionCannotReplaceRunningPatch",
    "PreparedUpdaterTest.ExplicitBaseIsBegunWithoutLoadingTheCandidate",
    "PreparedUpdaterTest.ReturnedLoaderFailureRetriesBaseWithoutRetiringPatch",
    "PreparedUpdaterTest.StaleSelectionNeverReachesTheCandidateLoader",
    "PreparedUpdaterTest.FailedRequiredConfigurationCannotStartImplicitly",
    "PreparedUpdaterTest.PreparationFailureCannotFallThroughToLegacySelection",
    "PreparedUpdaterTest.FailedAbortCannotReportCandidateSuccessOrStartUpdates",
    "PreparedBootSourceTest.ClosesOwnedFDAndTransfersDetachedFD",
    "Shorebird.NativePatchBudgetUsesReleaseDefaultOrExplicitOverride",
    "Shorebird.NativePatchBudgetRejectsMalformedAmbiguousAndOverflowValues",
})


def listed_tests(path: Path) -> set[str]:
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
    if not REQUIRED_SHOREBIRD_TESTS <= names:
        raise ValueError(f"Missing Shorebird controls in binary: {sorted(REQUIRED_SHOREBIRD_TESTS - names)}")
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

    def test_exact_controls_pass_and_full_shorebird_inventory_includes_legacy(self):
        self.write(self.report())
        self.assertEqual(validate_report(self.path, EMBEDDER_TESTS), 4)
        names = REQUIRED_SHOREBIRD_TESTS | {"UpdaterTest.ReportLaunchStartOnlyCallsOnce"}
        listing = self.path.with_suffix(".txt")
        listing.write_text("".join(f"{suite}.\n" + "".join(f"  {name.split('.', 1)[1]}\n" for name in sorted(names) if name.startswith(suite + ".")) for suite in sorted({name.split('.', 1)[0] for name in names})), encoding="utf-8")
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
        for mutation in mutations:
            root = self.report()
            mutation(root)
            self.write(root)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_report(self.path, EMBEDDER_TESTS)
        self.path.write_text("<testsuites", encoding="utf-8")
        with self.assertRaises(ET.ParseError):
            validate_report(self.path, EMBEDDER_TESTS)

    def test_incomplete_or_disabled_list_fails(self):
        for listing in ("", "PreparedUpdaterTest.\n  DISABLED_Example\n", "not a gtest inventory\n"):
            self.path.write_text(listing, encoding="utf-8")
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                listed_tests(self.path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--shorebird-list", type=Path)
    parser.add_argument("--shorebird-xml", type=Path)
    parser.add_argument("--embedder-xml", type=Path)
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ValidatorTests))
        return 0 if result.wasSuccessful() else 1
    if not all((args.shorebird_list, args.shorebird_xml, args.embedder_xml)):
        parser.error("--shorebird-list, --shorebird-xml, and --embedder-xml are required")
    try:
        shorebird = validate_report(args.shorebird_xml, listed_tests(args.shorebird_list))
        embedder = validate_report(args.embedder_xml, EMBEDDER_TESTS)
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"Native cache engine test contract failed: {error}", file=sys.stderr)
        return 1
    print(f"Native cache engine test contract passed: {shorebird} Shorebird tests and {embedder} AOT embedder controls executed; no failures/skips")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
