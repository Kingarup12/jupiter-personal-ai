importScripts('./frontend_version.js');

const JUPITER_STATIC_CACHE_PREFIX = 'jupiter-static-';
const JUPITER_LEGACY_CACHE_PREFIX = 'jupiter-cache-';
const VERSION = self.JUPITER_FRONTEND_VERSION || 'unversioned';
const CACHE_NAME = `${JUPITER_STATIC_CACHE_PREFIX}${VERSION}`;
const versioned = (path) => `${path}?v=${encodeURIComponent(VERSION)}`;
const urlsToCache = [
  './index.html',
  './frontend_version.js',
  versioned('./style.css'),
  versioned('./app.js'),
  versioned('./profile_photo.js'),
  versioned('./tts_selection.js'),
  versioned('./live_voice_orb.js'),
  versioned('./live_voice_policy.js'),
  versioned('./math_presentation.js'),
  versioned('./interactive_response.js'),
  versioned('./live_media.js'),
  versioned('./copyable_blocks.js'),
  versioned('./manifest.json')
];

self.addEventListener('install', event => {
  self.skipWaiting(); // Force the waiting service worker to become the active service worker.
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => Promise.allSettled(urlsToCache.map(url => cache.add(url))))
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          const isJupiterStatic = cacheName.startsWith(JUPITER_STATIC_CACHE_PREFIX)
            || cacheName.startsWith(JUPITER_LEGACY_CACHE_PREFIX);
          if (isJupiterStatic && cacheName !== CACHE_NAME) {
            return caches.delete(cacheName);
          }
        })
      );
    }).then(() => self.clients.claim()) // claim clients immediately
  );
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  const sameOrigin = url.origin === self.location.origin;
  const dynamic = sameOrigin && (
    url.pathname.startsWith('/api/')
    || url.pathname === '/health'
    || url.pathname.startsWith('/downloads/')
  );
  if (dynamic) return;

  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request, { cache: 'no-store' })
        .catch(() => caches.match('./index.html'))
    );
    return;
  }

  const isVersionedStatic = sameOrigin && url.searchParams.get('v') === VERSION;
  if (isVersionedStatic) {
    event.respondWith((async () => {
      const cached = await caches.match(event.request);
      if (cached) return cached;
      const response = await fetch(event.request);
      if (response.ok) {
        const cache = await caches.open(CACHE_NAME);
        await cache.put(event.request, response.clone());
      }
      return response;
    })());
    return;
  }

  // Version/bootstrap files revalidate from the network; offline fallback is
  // limited to the current Jupiter static cache and never includes private API data.
  event.respondWith(fetch(event.request, { cache: 'no-cache' }).catch(() => caches.match(event.request)));
});
