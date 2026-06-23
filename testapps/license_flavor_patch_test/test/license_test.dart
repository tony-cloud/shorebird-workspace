import 'package:flutter_test/flutter_test.dart';
import 'package:license_flavor_patch_test/license.dart';
import 'package:license_flavor_patch_test/main.dart';

void main() {
  test('defaults to free license flavor', () {
    expect(currentLicenseType(), LicenseType.free);
    expect(proFeatureEnabled, isFalse);
  });

  testWidgets('renders free license status labels', (tester) async {
    await tester.pumpWidget(const LicenseFlavorPatchApp());

    expect(find.text('license:free'), findsOneWidget);
    expect(find.text('pro-feature:off'), findsOneWidget);
  });
}
