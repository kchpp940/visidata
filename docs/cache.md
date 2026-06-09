---
eleventyNavigation:
  key: Remote Data Cache
  order: 7
  parent: Getting Started
update: 2024-06-09
version: VisiData 3.4
---

## Remote Data Cache Management

VisiData provides a unified remote data cache system for HTTP, S3, and other remote sources. Downloaded files and their metadata are managed centrally so they can be viewed, refreshed, and cleared from a single interface.

### Cache Sheet

Open the Cache Sheet with `File > Cache > Open cache sheet` or the `open-cache` command.

Each row represents one cached remote resource. The following columns are available:

- **source_type** -- origin protocol (http, s3, etc.)
- **url** -- the original remote URL
- **local_path** -- path to the cached file on disk
- **size** -- size of the cached file
- **mtime** -- modification time of the cached file
- **last_accessed** -- when this cache entry was last used
- **cache_policy** -- the invalidation policy for this entry
- **etag** -- HTTP ETag header (hidden by default)
- **last_modified** -- HTTP Last-Modified or S3 modification time (hidden by default)
- **content_type** -- MIME type of the resource (hidden by default)

### Cache Commands

From the Cache Sheet:

| Key(s) | Command | Description |
|--------|---------|-------------|
| `Enter` | `cache-open-cached` | Open the cached local file (offline-safe) |
| `g Enter` | `cache-open-cached-selected` | Open all selected cached files locally |
| `z Enter` | `cache-reload-source` | Reload the original URL/S3 source |
| `gz Enter` | `cache-reload-sources` | Reload original sources for all selected rows |
| `Ctrl+R` | `cache-refresh` | Re-download the current cache entry |
| `g Ctrl+R` | `cache-refresh-all` | Re-download all selected cache entries |
| `d` | `cache-delete-row` | Delete the current cache entry |
| `g d` | `cache-clear-all` | Clear ALL cache entries and files |
| `y` | `cache-yank-url` | Copy original URL to clipboard |
| `g y` | `cache-yank-urls` | Copy all selected URLs to clipboard |
| `e` | `cache-set-policy` | Edit the cache invalidation policy |

Global commands (available everywhere):

| Command | Description |
|---------|-------------|
| `open-cache` | Open the Cache Sheet |
| `cache-toggle` | Toggle remote caching on/off |
| `cache-toggle-offline` | Toggle offline mode (use cache only) |
| `cache-clear-current` | Clear cache for the current sheet source |
| `cache-refresh-source` | Drop cache for current sheet source and reload |

### Cache Policies

Each cache entry has a policy that determines when it is considered stale:

- **days:N** -- consider the entry stale after N days (default 1)
- **etag** -- use HTTP ETag for conditional validation (never expires locally)
- **last-modified** -- use HTTP Last-Modified for conditional validation (never expires locally)
- **never** -- never consider the entry stale; always use the cache

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `cache_enabled` | True | Enable remote data caching |
| `cache_offline` | False | Offline mode: use cache only, no network requests |
| `cache_default_days` | 1 | Default cache expiry in days for new entries |
| `http_use_cache` | True | Cache HTTP responses locally |
| `s3_use_cache` | True | Cache S3 objects locally |

### How It Works

When an HTTP or S3 URL is opened:

1. The cache manager checks for an existing valid entry
2. If a valid entry exists, the local cached file is used
3. If no cache exists (or the entry is stale per its policy), the resource is downloaded
4. Metadata (ETag, Last-Modified, size, mtime, etc.) is stored in `$XDG_CACHE_HOME/visidata/_cache_index.json`
5. The cached file is stored under `$XDG_CACHE_HOME/visidata/`

HTTP caching supports conditional requests via `If-None-Match` (ETag) and `If-Modified-Since` headers, returning 304 Not Modified responses when the content has not changed.

### Offline Mode

When `cache_offline` is enabled, VisiData will only use locally cached files and will never make network requests. Opening a URL without a cache entry will fail with an error message.
