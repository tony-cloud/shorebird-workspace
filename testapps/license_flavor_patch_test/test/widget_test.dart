import 'package:flutter_test/flutter_test.dart';

import 'package:license_flavor_patch_test/main.dart';

void main() {
  testWidgets('renders default license status', (tester) async {
    await tester.pumpWidget(const LicenseFlavorPatchApp());

    expect(find.text('license:free'), findsOneWidget);
    expect(find.text('pro-feature:off'), findsOneWidget);
  });
}
