# WebDAV operations

Everything else on this server is reflected from `ocs_api_viewer`, which describes the **OCS API only** - metadata about files, never their bytes. `collectives_page_get` reports that a page is 2,739 bytes; nothing discovered returns the bytes. Page bodies live at `/remote.php/dav/files/<user>/…`, outside anything discovery can see, so five hand-written `webdav_*` operations cover read, write, list, create folder and delete.

They are registered as catalogue entries rather than as tools of their own, so `nextcloud_find_operations` finds them beside the discovered ones, `nextcloud_describe_operations` returns their schemas, and `nextcloud_call_operation` runs them. The fixed surface stays at four tools, and each operation's warnings arrive from `describe` at the moment of use instead of sitting in every client's context for the whole session. Because nothing discovers them, `find`'s own description lists `webdav` in its app index; searching `delete folder` also ranks `webdav_delete` first without knowing the app name.

Rather than teach the server about Collectives, the gap is closed with generic file operations. A page body is just a file, so the app-agnostic primitive covers it and everything else, and the server keeps its property of having no per-app integration code. Composing a page's path is the caller's job, from fields its own index entry already returns:

```text
{collectivePath}/{filePath}/{fileName}      # filePath is often empty
.Collectives/研究筆記/第一章 概論/背景.md
```

All five run under the caller's own credentials - `nextcloud_call_operation` refuses an uncredentialed caller whichever operation it names - so they reach exactly the files that caller can reach. Paths are confined to that user's files root: `.` and `..` segments are rejected rather than normalised, because Basic auth stops a caller reaching another user's files but would not stop a traversal climbing out of `files/<user>/` into the other DAV endpoints.

`webdav_write_file` replaces the whole file. Read first, send the `etag` back as `if_match`, and a write computed from stale content fails with 412 instead of silently discarding whatever changed in between. It will not create parent folders on its own - use `webdav_create_folder`, or for a Collectives page create it with `collectives_page_create` and write the body afterwards.

Binary goes through `content_base64`, because reads hand binary back base64-encoded and feeding that into the text field would write the base64 itself, then encode it again on the next read - a silent round-trip corruption that reports success at every step.

`webdav_delete` refuses a folder unless `recursive` is set: WebDAV DELETE on a collection always takes everything inside and has no shallow variant, so the resource is inspected first rather than letting a mistyped path remove a subtree. Deletions land in the Nextcloud trash bin.

Three behaviours here were corrected only after testing against a real instance rather than reasoning from the specification:

- A write below a missing folder answers **404**, not the 409 the WebDAV spec implies, so guidance keyed on 409 never appeared.
- `GET` on a folder returns **200** with the HTML placeholder Nextcloud serves for a collection, which made a mistyped path look like a successfully read file. Files always carry an ETag and that page never does, which is how the two are told apart.
- ETags come back with a `-gzip` (or `-br`, `-deflate`, `-zstd`) suffix whenever the response is compressed, while the entity's real validator has none. Handing the suffixed value back as `If-Match` failed with 412 while nothing had changed, and re-reading returned the same suffixed value - an unbreakable loop. It only bites on responses large enough to compress, so small test files never showed it.
