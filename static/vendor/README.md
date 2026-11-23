# Locally served barcode decoder

- `zxing-browser-0.2.1.min.js`: unmodified UMD browser bundle from `@zxing/browser` **0.2.1**, MIT license (`zxing-browser-LICENSE.txt`).
- Source: https://github.com/zxing-js/browser
- Release archive: https://registry.npmjs.org/@zxing/browser/-/browser-0.2.1.tgz
- Archive SHA-512 (base64): `92pVfVDUXbc15xu9vIEmhNyqIEASMEkDF92CwT6T+7sg2EHXJRktH9CQKoQSXe51UtbNsj+Fm09H/JBWHMs/Sw==`
- Registry's current latest release was checked on 2026-11-11. The package metadata lists the `@zxing/library` ^0.23.0 peer dependency. The browser UMD bundle includes the decoder; its Apache-2.0 license is retained in `zxing-library-LICENSE`.

The page serves this bundle from its own server, not from a runtime CDN. Barcode images stay in the browser; accepted tracking text follows the existing local-server order lookup flow. The application owns the camera and decode timer, using the synchronous `decode(video)` API, so cancellation never depends on a second library-managed camera stream.

When updating, verify release provenance/integrity, dependency licenses, exported `BarcodeFormat` values, `possibleFormats`, `decode(video)`, and recoverable exception `getKind()` behavior. Run application regression tests and test real labels on Android Chrome and iPhone Safari.
