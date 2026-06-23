import 'package:flutter_test/flutter_test.dart';
import 'package:license_flavor_patch_test/license.dart';
import 'package:license_flavor_patch_test/main.dart';

void main() {
  final isProFlavor = currentLicenseType() == LicenseType.pro;

  test('pro flavor enables pro feature gate', () {
    expect(currentLicenseType(), LicenseType.pro);
    expect(proFeatureEnabled, isTrue);
  }, skip: !isProFlavor);

  testWidgets('renders pro license status labels', (tester) async {
    await tester.pumpWidget(const LicenseFlavorPatchApp());

    expect(find.text('license:pro'), findsOneWidget);
    expect(find.text('pro-feature:enabled'), findsOneWidget);
  }, skip: !isProFlavor);
}
