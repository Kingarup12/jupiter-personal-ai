(function publishJupiterFrontendVersion(global) {
  'use strict';

  // Canonical deterministic version for the shared Windows, mobile-web, and
  // Android-hosted Jupiter frontend. Bump this only when shipped shell assets
  // change; persistent account/user storage is deliberately independent.
  global.JUPITER_FRONTEND_VERSION = '222-notifications-r1';
})(typeof self !== 'undefined' ? self : window);
