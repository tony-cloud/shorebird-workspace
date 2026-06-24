import 'package:flutter/material.dart';

import 'launch_status.dart';
import 'license.dart';

@pragma('vm:never-inline')
@pragma('vm:entry-point')
String currentLicenseNameForPatch() => currentLicenseType().name;

@pragma('vm:never-inline')
@pragma('vm:entry-point')
bool currentProFeatureEnabledForPatch() => proFeatureEnabled;

@pragma('vm:never-inline')
String readLicenseNameThroughPatchEntry() {
  return Function.apply(currentLicenseNameForPatch, const <Object?>[])
      as String;
}

@pragma('vm:never-inline')
bool readProFeatureEnabledThroughPatchEntry() {
  return Function.apply(currentProFeatureEnabledForPatch, const <Object?>[])
      as bool;
}

void main() {
  final licenseName = readLicenseNameThroughPatchEntry();
  final isProFeatureEnabled = readProFeatureEnabledThroughPatchEntry();
  writeLaunchStatus(
    license: licenseName,
    proFeatureEnabled: isProFeatureEnabled,
  );
  runApp(const LicenseFlavorPatchApp());
}

class LicenseFlavorPatchApp extends StatelessWidget {
  const LicenseFlavorPatchApp({super.key});

  @override
  Widget build(BuildContext context) {
    final licenseName = readLicenseNameThroughPatchEntry();
    final isProFeatureEnabled = readProFeatureEnabledThroughPatchEntry();
    return MaterialApp(
      home: Scaffold(
        appBar: AppBar(title: const Text('License Patch Test')),
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                'license:$licenseName',
                key: const ValueKey('license-status'),
              ),
              Text(
                isProFeatureEnabled ? 'pro-feature:enabled' : 'pro-feature:off',
                key: const ValueKey('feature-status'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
