import 'package:license_flavor_patch_test/license.dart';

void main() {
  final license = currentLicenseType();
  print('license:${license.name}');
  print(proFeatureEnabled ? 'pro-feature:enabled' : 'pro-feature:off');
}
