autoComplete.js 10.2.10, Tarek Raafat, Apache-2.0.

Upstream: https://github.com/TarekRaafat/autoComplete.js/tree/v10.2.10
Unmodified dist/autoComplete.min.js: 9,367 bytes.
SHA-256: bf0308f29d699f4f9e92a23a807adc9684158c5a9a4b3db0c4950272a8554641

LICENSE is beside the script. Guide's adapter and CSS are separate.

location-index.js builds a name-only index from the public snapshot embedded in HTML.
location-search.js adapts it to the widget; typing never fetches data or waits on debounce.
Native GET matching is mirrored by domain/location_search.py and checked against this index.
