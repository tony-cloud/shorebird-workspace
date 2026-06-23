import 'package:flutter/material.dart';

import 'license.dart';

void main() => runApp(const LicenseFlavorPatchApp());

class LicenseFlavorPatchApp extends StatelessWidget {
  const LicenseFlavorPatchApp({super.key});

  @override
  Widget build(BuildContext context) {
    final license = currentLicenseType();
    return MaterialApp(
      home: Scaffold(
        appBar: AppBar(title: const Text('License Patch Test')),
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                'license:${license.name}',
                key: const ValueKey('license-status'),
              ),
              Text(
                proFeatureEnabled ? 'pro-feature:enabled' : 'pro-feature:off',
                key: const ValueKey('feature-status'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
